# -*- coding: utf-8 -*-
"""GUI-independent lifecycle controller for formal batch generation.

The chapter-writing implementation is still supplied by a host adapter during
the first extraction phase.  This module owns the job boundary, durable start
state, worker exception handling, thread lifecycle and project-lock release so
GUI and headless entrypoints cannot drift apart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class BatchRequest:
    """Normalized, immutable bounds for one batch job."""

    count: int
    label: str
    target_end: int
    mode: str
    resume: bool = False

    @classmethod
    def build(
        cls,
        *,
        count: int,
        label: str,
        next_chapter: int,
        target_end: int | None = None,
        mode: str | None = None,
        resume: bool = False,
    ) -> "BatchRequest":
        normalized_count = int(count)
        normalized_next = int(next_chapter)
        if normalized_count < 1:
            raise ValueError("批量章节数必须大于0")
        if normalized_next < 1:
            raise ValueError("下一章节号必须大于0")
        normalized_target = int(
            target_end if target_end is not None else normalized_next + normalized_count - 1
        )
        expected_target = normalized_next + normalized_count - 1
        if normalized_target != expected_target:
            raise ValueError(
                f"批量边界不一致：第{normalized_next}章起写{normalized_count}章，"
                f"终点应为第{expected_target}章，而不是第{normalized_target}章"
            )
        normalized_label = str(label or "批量生成")
        normalized_mode = str(mode or "").strip() or (
            "trial"
            if "试写" in normalized_label
            else "full_book"
            if "一键写完整本" in normalized_label
            else "batch"
        )
        if normalized_mode not in {"batch", "trial", "full_book"}:
            raise ValueError(f"不支持的批量模式：{normalized_mode}")
        return cls(
            count=normalized_count,
            label=normalized_label,
            target_end=normalized_target,
            mode=normalized_mode,
            resume=bool(resume),
        )


class BookRunner:
    """Own one formal batch lifecycle while delegating chapter work to a host.

    The host is intentionally duck-typed.  A Tk GUI and a headless adapter can
    both provide the same narrow operational surface without importing Tk here.
    """

    def __init__(
        self,
        host: Any,
        *,
        thread_factory: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.host = host
        self._thread_factory = thread_factory or threading.Thread
        self._now = now or datetime.now
        self._thread: Any | None = None

    @property
    def thread(self) -> Any | None:
        return self._thread

    @property
    def is_running(self) -> bool:
        return bool(getattr(self.host, "is_batch_running", False))

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _prepare_model_budget(self, request: BatchRequest) -> None:
        host = self.host
        with host._model_call_lock:
            if request.resume or host._model_call_limit <= host._model_call_count:
                per_chapter = max(
                    4,
                    min(
                        20,
                        int(host.config.get("max_model_calls_per_chapter", 16) or 16),
                    ),
                )
                host._model_call_count = 0
                ledger_gap = host._structured_state_gap()
                migration_calls = ledger_gap * max(
                    1,
                    int(host.config.get("state_delta_max_attempts", 2) or 2)
                    * (
                        2
                        if (
                            host.config.get("state_delta_independent_audit", True)
                            or host.config.get("pre_save_continuity_audit_enabled", True)
                        )
                        else 1
                    ),
                )
                host._model_call_limit = request.count * per_chapter + migration_calls
            host._chapter_model_call_count = 0
            host._chapter_model_call_limit = 0
            host._chapter_model_call_number = 0
        host._update_model_usage_label()

    def _snapshot_selected_inputs(self) -> None:
        host = self.host
        host._batch_selected_chars = {
            name for name, var in getattr(host, "chars_vars", {}).items() if var.get()
        }
        host._batch_selected_world = {
            name for name, var in getattr(host, "world_vars", {}).items() if var.get()
        }

    def start(
        self,
        count: int,
        *,
        label: str = "批量生成",
        target_end: int | None = None,
        resume: bool = False,
        mode: str | None = None,
    ) -> bool:
        """Start one bounded worker thread after writing a durable checkpoint."""

        host = self.host
        if self.is_running:
            return False
        _, next_chapter, _, _, _ = host._get_latest_chapter_info()
        request = BatchRequest.build(
            count=count,
            label=label,
            next_chapter=next_chapter,
            target_end=target_end,
            mode=mode,
            resume=resume,
        )
        if not host._begin_project_task(request.label):
            return False

        try:
            self._prepare_model_budget(request)
            self._snapshot_selected_inputs()
            host._stop_event.clear()
            host.is_batch_running = True
            old_state = host._load_batch_state() if request.resume else {}
            host._current_batch_job_id = old_state.get("job_id") or self._now().strftime(
                "batch_%Y%m%d_%H%M%S"
            )
            network_resume_cycles = int(old_state.get("network_resume_cycles") or 0)
            if request.resume and old_state.get("status") == "interrupted":
                network_resume_cycles += 1
            host._write_batch_state(
                job_id=host._current_batch_job_id,
                status="running",
                mode=request.mode,
                target_end=request.target_end,
                planned_count=request.count,
                completed_count=0,
                skipped_chapters=[],
                resume_allowed=request.mode in {"full_book", "trial"},
                network_resume_cycles=network_resume_cycles,
                stage="starting",
                started_at=self._timestamp(),
            )
            host._append_batch_audit(
                {
                    "event": "batch_resume" if request.resume else "batch_start",
                    "label": request.label,
                    "planned_count": request.count,
                    "target_end": request.target_end,
                }
            )
            host._ensure_batch_log_window()
            host._ui_progress_append(
                f" {request.label}启动，共计划 {request.count} 章。\n\n", clear=True
            )
            host.disable_buttons()
            host.btn_stop.config(state="normal")
            self._thread = self._thread_factory(
                target=self._worker_entry,
                args=(request.count,),
                daemon=True,
            )
            self._thread.start()
            return True
        except Exception:
            host.is_batch_running = False
            host._finish_project_task()
            raise

    def _worker_entry(self, total_count: int) -> None:
        """Turn an unexpected worker exception into a durable safe pause."""

        host = self.host
        try:
            host.batch_worker(total_count)
        except Exception as exc:
            message = f"托管线程异常：{str(exc)[:300]}"
            host._append_batch_audit(
                {"event": "batch_unexpected_error", "error": str(exc)[:1200]}
            )
            host._write_batch_state(
                status="paused",
                resume_allowed=False,
                stage="unexpected_error",
                message=message,
                finished_at=self._timestamp(),
            )
            host._ui_progress_append(
                f"\n\n[FAIL] {message}\n已安全暂停，不会跳过当前章。\n"
            )
            host.is_batch_running = False
            host.is_generating = False
            host._ui(lambda: host.enable_buttons(True))
            host._ui(lambda: host.btn_stop.config(state="disabled"))
        finally:
            host._finish_project_task()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the current worker; return False only when timeout expires."""

        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not bool(thread.is_alive())


__all__ = ["BatchRequest", "BookRunner"]
