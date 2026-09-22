# -*- coding: utf-8 -*-
"""Permanent non-visual adapter for the production novel pipeline.

This is a compatibility bridge while chapter generation is incrementally
extracted from ``gui_app.py``.  It never creates a Tk window and provides only
the inert UI surface still referenced by legacy chapter-writing methods.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import cross_chapter_scanner
import formal_suffix_rewrite
import generator
import gui_app
import project_job_lock


def describe_preflight_progress(row, now=None):
    now = time.monotonic() if now is None else now
    chapter = int(row.get("chapter") or 0)
    detail = f"第{chapter}章" if chapter else "尾段"
    detail += " " + str(row.get("stage") or "")
    if row.get("phase"):
        detail += f" / 第{row.get('attempt')}轮 {row['phase']}"
    elapsed = max(0, int(now - row.get("observed_at", now)))
    detail += f"（{row.get('event', '等待')}，距该事件{elapsed}秒）"
    if "elapsed_seconds" in row:
        detail += f"；调用耗时{row['elapsed_seconds']}秒"
    if str(row.get("event", "")).endswith("started"):
        detail += "；尚未收到完成事件，心跳不代表模型有新输出"
    return detail


class StaticVar:
    def __init__(self, value=True):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class DummyWidget:
    def config(self, **_kwargs):
        return None

    configure = config

    def delete(self, *_args, **_kwargs):
        return None

    def insert(self, *_args, **_kwargs):
        return None

    def see(self, *_args, **_kwargs):
        return None

    def get(self, *_args, **_kwargs):
        return ""

    def set(self, *_args, **_kwargs):
        return None


class HeadlessRoot:
    def after(self, _delay_ms, callback=None, *args):
        if callback is not None:
            return callback(*args)
        return None


class HeadlessNovelGenerator(gui_app.NovelGeneratorGUI):
    """Run the formal production pipeline without creating desktop widgets."""

    _DUMMY_SUFFIXES = ("_lbl", "_label", "_text", "_entry", "_combo")

    def __init__(self, project_dir: str | Path, *, echo_progress: bool = True):
        project_path = Path(project_dir).resolve()
        if not project_path.is_dir():
            raise FileNotFoundError(f"项目目录不存在：{project_path}")
        if not (project_path / "project_config.json").is_file():
            raise FileNotFoundError(f"项目缺少 project_config.json：{project_path}")

        self._echo_progress = bool(echo_progress)
        self.root = HeadlessRoot()
        self.config = generator.load_config()
        self.is_generating = False
        self.last_saved_text = ""
        self.current_req = ""
        self.generated_content = ""
        self.project_dir = str(project_path)
        self.current_vol = 1
        self.next_chap = 1
        self.filepath = ""
        self.latest_chap = 0
        self.latest_filepath = ""
        self._stop_event = threading.Event()
        self.is_batch_running = False
        self.batch_log_win = None
        self.batch_log_text = None
        self._tool_rules_cache = None
        self._reveal_rules_cache = None
        self._project_config_error = ""
        self._current_batch_job_id = ""
        self._commercial_review_running = False
        self._initialization_running = False
        self._tool_task_running = False
        self._active_task_name = ""
        self._project_job_lock = None
        self._busy_sensitive_widgets = []
        self._active_preset_name = gui_app.PROVIDER_PRESET_NAMES[
            gui_app.PROVIDER_DEEPSEEK
        ]
        self._batch_selected_chars = set()
        self._batch_selected_world = set()
        self._closing_requested = False
        self._closed = False
        self._model_call_count = 0
        self._model_call_limit = 0
        self._chapter_model_call_count = 0
        self._chapter_model_call_limit = 0
        self._chapter_model_call_number = 0
        self._usage_prompt_tokens = 0
        self._usage_completion_tokens = 0
        self._usage_reasoning_tokens = 0
        self._usage_estimated_cost_cny = 0.0
        self._model_usage_persistence_fault = ""
        self._model_usage_unpersisted_cost_cny = 0.0
        self._deepseek_preflight_cache = {}
        self._provider_preflight_cache = {}
        self._deepseek_direct_mode = False
        self._model_call_lock = threading.Lock()
        self._commercial_review_call_active = False
        self._commercial_review_stage_remaining = 0
        self._commercial_review_fallback_call_active = False
        self._commercial_review_fallback_eligible = 0
        self._reader_staging_validation_cache = {}

        dummy = DummyWidget()
        for name in (
            "result_text",
            "progress_text",
            "btn_new",
            "btn_continue",
            "btn_batch",
            "btn_full_book",
            "btn_stop",
            "info_lbl",
            "book_summary_lbl",
            "folder_lbl",
            "word_count_lbl",
            "usage_lbl",
        ):
            setattr(self, name, dummy)

        gui_app.apply_project_dir(self.project_dir)
        self._reset_project_scoped_caches()
        self._load_project_config(self.project_dir)
        self._load_tool_rules(create_if_missing=False)
        generator.reload_project_tool_rules()
        cross_chapter_scanner.reload_project_tool_rules(self.project_dir)

        self.refresh_status()
        self.chars_map = generator.list_files_in_dir(generator.DIRS["chars"])
        self.world_map = generator.list_files_in_dir(generator.DIRS["world"])
        self.plot_map = generator.list_files_in_dir(generator.DIRS["plot"])
        self.chars_vars = {name: StaticVar(True) for name in self.chars_map}
        self.world_vars = {name: StaticVar(True) for name in self.world_map}
        self.plot_vars = {name: StaticVar(False) for name in self.plot_map}

    def __getattr__(self, name):
        if name.startswith("btn_") or name.endswith(self._DUMMY_SUFFIXES):
            widget = DummyWidget()
            setattr(self, name, widget)
            return widget
        raise AttributeError(name)

    def _ui(self, callback):
        return callback()

    def _ui_progress_append(self, text, clear=False):
        if self._echo_progress:
            prefix = "[CLEAR] " if clear else ""
            print(prefix + str(text).rstrip(), flush=True)

    def _set_busy_widgets(self, _busy):
        return None

    def _refresh_readiness_status(self):
        return None

    def _update_model_usage_label(self):
        return None

    def disable_buttons(self):
        return None

    def enable_buttons(self, *_args, **_kwargs):
        return None

    def update_word_count(self):
        return None

    def refresh_reader_files_silent(self):
        return None

    def refresh_status(self):
        (
            self.current_vol,
            self.next_chap,
            self.filepath,
            self.latest_chap,
            self.latest_filepath,
        ) = generator.get_latest_chapter_info()

    def _refresh_live_progress_label(self):
        return None

    def _restore_project_bindings(self):
        gui_app.apply_project_dir(self.project_dir)
        generator.reload_project_tool_rules()
        cross_chapter_scanner.reload_project_tool_rules(self.project_dir)

    def rewrite_formal_suffix_from_drafts(
        self,
        start_chapter,
        end_chapter,
        draft_paths=None,
        progress_callback=None,
        micro_edit=False,
    ):
        """Preflight a full suffix in isolation, then promote it once."""

        if not bool(
            self.config.get("formal_suffix_isolated_preflight_enabled", True)
        ):
            return super().rewrite_formal_suffix_from_drafts(
                start_chapter,
                end_chapter,
                draft_paths=draft_paths,
                progress_callback=progress_callback,
                micro_edit=micro_edit,
            )

        start = int(start_chapter)
        end = int(end_chapter)
        real_project = Path(self.project_dir).resolve()
        candidates = dict(draft_paths or {})
        if not candidates:
            for chapter in range(start, end + 1):
                volume = next(
                    (
                        index
                        for index, (first, last, _name) in enumerate(
                            self._get_volume_ranges(), start=1
                        )
                        if first <= chapter <= last
                    ),
                    self.current_vol,
                )
                candidates[chapter] = (
                    real_project
                    / "drafts"
                    / f"第{volume:02d}卷"
                    / f"第{chapter:04d}章.txt"
                )

        micro_budget = 0
        if micro_edit:
            resolved = formal_suffix_rewrite.resolve_sparse_micro_edit_candidates(
                real_project,
                candidates,
                start_chapter=start,
                end_chapter=end,
            )
            report = formal_suffix_rewrite.validate_micro_edit_candidates(
                real_project,
                resolved,
                max_changed_chars=int(
                    self.config.get("formal_micro_edit_max_changed_chars", 800) or 800
                ),
                max_changed_ratio=float(
                    self.config.get("formal_micro_edit_max_changed_ratio", 0.08) or 0.08
                ),
            )
            changed_count = len(report.get("changed_chapters") or [])
            configured_cap = int(
                self.config.get("formal_micro_edit_model_call_limit", 40) or 40
            )
            micro_budget = min(configured_cap, max(8, changed_count * 6 + 8))

        latest_progress = {"event": "等待隔离预审", "chapter": 0, "stage": "",
                           "observed_at": time.monotonic()}
        heartbeat_stop = threading.Event()

        def progress_description():
            return describe_preflight_progress(dict(latest_progress))

        def heartbeat():
            while not heartbeat_stop.wait(30):
                self._ui_progress_append(
                    f"[正式微调] 等待状态：{progress_description()}"
                )

        def notify(event, **payload):
            latest_progress.clear()
            latest_progress.update({"event": event, **payload})
            latest_progress["observed_at"] = time.monotonic()
            if event in {
                "isolated_preflight_started",
                "isolated_preflight_completed",
                "isolated_preflight_failed",
                "preflight_model_started",
                "preflight_model_completed",
                "state_model_call_started",
                "state_model_call_completed",
                "state_model_call_failed",
            }:
                self._ui_progress_append(
                    f"[正式微调] {progress_description()}"
                )
            if callable(progress_callback):
                try:
                    progress_callback({"event": event, **payload})
                except Exception:
                    pass

        previous_lock = getattr(self, "_project_job_lock", None)
        previous_model_limit = self._model_call_limit
        previous_model_count = self._model_call_count
        owned_lock = None
        if previous_lock is None:
            owned_lock = project_job_lock.ProjectJobLock(real_project)
            locked, reason = owned_lock.acquire(
                f"隔离预审并提交第{start}-{end}章"
            )
            if not locked:
                raise RuntimeError(reason)
            self._project_job_lock = owned_lock

        heartbeat_thread = None
        if self._echo_progress:
            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="formal-micro-edit-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

        try:
            if micro_budget:
                self._model_call_count = 0
                self._model_call_limit = micro_budget
                self._ui_progress_append(
                    f"[正式微调] 本次模型调用硬上限：每阶段 {micro_budget} 次"
                )
            notify(
                "isolated_preflight_started",
                start_chapter=start,
                end_chapter=end,
            )
            with tempfile.TemporaryDirectory(
                prefix="nsp_",
                dir=str(Path(real_project.anchor)),
            ) as temp_root:
                isolated_project = Path(temp_root) / "p"
                isolated_project, isolated_candidates = (
                    formal_suffix_rewrite.create_isolated_preflight_project(
                        real_project,
                        isolated_project,
                        candidates,
                    )
                )
                staged_app = HeadlessNovelGenerator(
                    isolated_project,
                    echo_progress=False,
                )
                if micro_budget:
                    staged_app._model_call_count = 0
                    staged_app._model_call_limit = micro_budget

                def staged_progress(row):
                    notify(
                        str((row or {}).get("event") or "isolated_preflight_progress"),
                        phase="isolated_preflight",
                        **{
                            key: value
                            for key, value in dict(row or {}).items()
                            if key != "event"
                        },
                    )

                staged_result = None
                staged_failure = None
                try:
                    staged_result = (
                        gui_app.NovelGeneratorGUI.rewrite_formal_suffix_from_drafts(
                            staged_app,
                            start,
                            end,
                            draft_paths=isolated_candidates,
                            progress_callback=staged_progress,
                            micro_edit=micro_edit,
                        )
                    )
                except Exception as exc:
                    staged_failure = exc
                finally:
                    promoted = formal_suffix_rewrite.promote_isolated_preflight_cache(
                        isolated_project,
                        real_project,
                    )
                if staged_failure is not None:
                    notify(
                        "isolated_preflight_failed",
                        start_chapter=start,
                        end_chapter=end,
                        promoted_cache_records=promoted,
                        error=str(staged_failure)[:1000],
                    )
                    raise staged_failure
                if str(staged_result.get("status") or "") != "COMPLETED":
                    raise RuntimeError("隔离预审未完整通过，拒绝触碰正式项目")

            self._restore_project_bindings()
            notify(
                "isolated_preflight_completed",
                start_chapter=start,
                end_chapter=end,
                promoted_cache_records=promoted,
            )
            result = gui_app.NovelGeneratorGUI.rewrite_formal_suffix_from_drafts(
                self,
                start,
                end,
                draft_paths=candidates,
                progress_callback=progress_callback,
                micro_edit=micro_edit,
            )
            result["isolated_preflight"] = {
                "status": "COMPLETED",
                "promoted_cache_records": promoted,
            }
            return result
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1)
            self._model_call_limit = previous_model_limit
            self._model_call_count = previous_model_count
            self._restore_project_bindings()
            if owned_lock is not None:
                self._project_job_lock = previous_lock
                owned_lock.release()


__all__ = ["HeadlessNovelGenerator"]
