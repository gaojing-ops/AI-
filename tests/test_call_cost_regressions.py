import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import gui_app


class CallCostRegressionTests(unittest.TestCase):
    def app(self, **config):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "model_provider": "deepseek", "model": "deepseek-flash",
            "max_tokens": 1000, "model_usage_ledger_enabled": True, **config,
        }
        app._model_call_lock = threading.Lock()
        app._model_call_count = 0
        app._model_call_limit = 10
        app._stop_event = threading.Event()
        app._ui = lambda callback: None
        app._append_batch_audit = lambda payload: None
        return app

    def test_thinking_fallback_stops_before_second_request_exceeds_cap(self):
        app = self.app(deepseek_review_thinking=True,
                       deepseek_review_thinking_min_tokens=1, max_estimated_cost_cny=1.0)
        spent = [0.75]
        app._project_estimated_cost_cny = lambda: spent[0]
        app._estimate_model_call_reserve_cny = lambda *args: 0.20
        app._record_model_usage = lambda *args, **kwargs: spent.__setitem__(0, spent[0] + 0.10)
        create = mock.Mock(return_value=types.SimpleNamespace(
            usage=None, choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=""))],
        ))
        app.get_client = lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        with self.assertRaisesRegex(RuntimeError, "可能超过止损"):
            app.call_llm_non_stream("system", "user", max_tokens=1000, review=True)
        self.assertEqual(1, create.call_count)
        self.assertEqual(1, app._model_call_count)

    def test_interrupted_stream_reservation_survives_restart_and_enforces_cap(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            gui_app.generator.DIRS, {"logs": root}, clear=True
        ):
            app = self.app()
            stream = mock.Mock()

            def interrupted():
                yield types.SimpleNamespace(usage=None, choices=[
                    types.SimpleNamespace(delta=types.SimpleNamespace(content="partial"))])
                raise TimeoutError("synthetic stream timeout")

            stream.__iter__ = mock.Mock(side_effect=interrupted)
            create = mock.Mock(return_value=stream)
            app.get_client = lambda: types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
            with self.assertRaisesRegex(TimeoutError, "synthetic stream timeout"):
                app._call_generation_text("system", "user", model_name="deepseek-flash",
                                          token_limit=100, task_type="test_interruption")
            stream.close.assert_called_once()
            record = json.loads((Path(root) / "model_usage.jsonl").read_text(encoding="utf-8"))
            self.assertEqual("estimated_reservation", record["usage_kind"])
            self.assertGreater(record["estimated_cost_cny"], 0)
            restarted = self.app(max_estimated_cost_cny=record["estimated_cost_cny"])
            self.assertEqual(record["estimated_cost_cny"], restarted._project_estimated_cost_cny())
            with self.assertRaisesRegex(RuntimeError, "已达到止损"):
                restarted._before_model_call(reserved_cost_cny=0.001)
            self.assertEqual(1, create.call_count)

    def test_known_usage_is_saved_even_when_close_raises(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            gui_app.generator.DIRS, {"logs": root}, clear=True
        ):
            app = self.app()
            usage = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
            stream = mock.MagicMock()
            stream.__iter__.return_value = iter([types.SimpleNamespace(usage=usage, choices=[])])
            stream.close.side_effect = OSError("close failed")
            app.get_client = mock.Mock()
            app.get_client.return_value.chat.completions.create.return_value = stream
            with self.assertRaisesRegex(OSError, "close failed"):
                app._call_generation_text("system", "user", model_name="deepseek-flash",
                                          token_limit=100, task_type="test_close")
            record = json.loads((Path(root) / "model_usage.jsonl").read_text(encoding="utf-8"))
            self.assertEqual("provider", record["usage_kind"])
            self.assertEqual(14, record["total_tokens"])

    def test_request_failure_with_unknown_usage_is_not_accounted_as_free(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            gui_app.generator.DIRS, {"logs": root}, clear=True
        ):
            app = self.app()
            app.get_client = mock.Mock()
            app.get_client.return_value.chat.completions.create.side_effect = TimeoutError("request timeout")
            with self.assertRaisesRegex(TimeoutError, "request timeout"):
                app._call_generation_text("system", "user", model_name="deepseek-flash",
                                          token_limit=100, task_type="test_request")
            summary = json.loads((Path(root) / "model_usage_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(1, summary["calls_with_usage"])
            self.assertGreater(summary["estimated_cost_cny"], 0)
