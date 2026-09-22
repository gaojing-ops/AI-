import threading
import unittest
from datetime import datetime

import book_runner


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class _Widget:
    def __init__(self):
        self.states = []

    def config(self, **kwargs):
        self.states.append(kwargs.get("state"))


class _ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self._alive = False

    def start(self):
        self._alive = True
        try:
            self.target(*self.args)
        finally:
            self._alive = False

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive


class _Host:
    def __init__(self, *, latest=0, worker_error=None, old_state=None):
        self.latest = latest
        self.worker_error = worker_error
        self.config = {
            "max_model_calls_per_chapter": 8,
            "state_delta_max_attempts": 2,
            "state_delta_independent_audit": True,
        }
        self._model_call_lock = threading.Lock()
        self._model_call_count = 0
        self._model_call_limit = 0
        self._chapter_model_call_count = 7
        self._chapter_model_call_limit = 7
        self._chapter_model_call_number = 7
        self._stop_event = threading.Event()
        self._stop_event.set()
        self.is_batch_running = False
        self.is_generating = False
        self._current_batch_job_id = ""
        self.chars_vars = {"角色甲": _Var(True), "角色乙": _Var(False)}
        self.world_vars = {"世界甲": _Var(True)}
        self.btn_stop = _Widget()
        self.state = dict(old_state or {})
        self.audits = []
        self.progress = []
        self.finish_count = 0
        self.worker_counts = []
        self.buttons_disabled = 0
        self.buttons_enabled = 0

    def _get_latest_chapter_info(self):
        return (1, self.latest + 1, "", self.latest, "")

    def _begin_project_task(self, _label):
        return True

    def _finish_project_task(self):
        self.finish_count += 1

    def _structured_state_gap(self):
        return 0

    def _update_model_usage_label(self):
        return None

    def _load_batch_state(self):
        return dict(self.state)

    def _write_batch_state(self, **updates):
        self.state.update(updates)

    def _append_batch_audit(self, event):
        self.audits.append(dict(event))

    def _ensure_batch_log_window(self):
        return None

    def _ui_progress_append(self, text, clear=False):
        self.progress.append((text, clear))

    def disable_buttons(self):
        self.buttons_disabled += 1

    def enable_buttons(self, *_args):
        self.buttons_enabled += 1

    def _ui(self, callback):
        return callback()

    def batch_worker(self, total_count):
        self.worker_counts.append(total_count)
        if self.worker_error:
            raise self.worker_error
        self.latest += total_count
        self.is_batch_running = False
        self._write_batch_state(
            status="completed",
            current_chapter=self.latest,
            completed_count=total_count,
        )


class BookRunnerTests(unittest.TestCase):
    def _runner(self, host):
        return book_runner.BookRunner(
            host,
            thread_factory=_ImmediateThread,
            now=lambda: datetime(2026, 8, 17, 10, 0, 0),
        )

    def test_request_rejects_mismatched_explicit_target(self):
        with self.assertRaisesRegex(ValueError, "批量边界不一致"):
            book_runner.BatchRequest.build(
                count=3,
                label="批量",
                next_chapter=6,
                target_end=9,
            )

    def test_start_owns_durable_lifecycle_and_exact_bounds(self):
        host = _Host(latest=5)
        runner = self._runner(host)

        self.assertTrue(
            runner.start(3, label="批量生成", target_end=8, mode="batch")
        )

        self.assertEqual([3], host.worker_counts)
        self.assertEqual(8, host.latest)
        self.assertEqual("completed", host.state["status"])
        self.assertEqual(8, host.state["target_end"])
        self.assertEqual(3, host.state["planned_count"])
        self.assertEqual({"角色甲"}, host._batch_selected_chars)
        self.assertEqual({"世界甲"}, host._batch_selected_world)
        self.assertEqual("batch_start", host.audits[0]["event"])
        self.assertEqual(1, host.finish_count)
        self.assertFalse(host._stop_event.is_set())
        self.assertTrue(runner.wait())

    def test_worker_exception_becomes_non_resumable_pause(self):
        host = _Host(latest=5, worker_error=RuntimeError("boom"))
        runner = self._runner(host)

        self.assertTrue(runner.start(1, target_end=6))

        self.assertEqual("paused", host.state["status"])
        self.assertEqual("unexpected_error", host.state["stage"])
        self.assertFalse(host.state["resume_allowed"])
        self.assertEqual("batch_unexpected_error", host.audits[-1]["event"])
        self.assertEqual(1, host.finish_count)

    def test_resume_keeps_job_id_and_increments_network_cycle(self):
        host = _Host(
            latest=5,
            old_state={
                "job_id": "job-fixed",
                "status": "interrupted",
                "network_resume_cycles": 2,
            },
        )
        runner = self._runner(host)

        self.assertTrue(
            runner.start(
                2,
                label="一键写完整本（自动恢复）",
                target_end=7,
                mode="full_book",
                resume=True,
            )
        )

        self.assertEqual("job-fixed", host._current_batch_job_id)
        self.assertEqual(3, host.state["network_resume_cycles"])
        self.assertEqual("batch_resume", host.audits[0]["event"])


if __name__ == "__main__":
    unittest.main()
