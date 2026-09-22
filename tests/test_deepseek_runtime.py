import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

import deepseek_runtime
import generator
from gui_app import NovelGeneratorGUI


class _BalanceResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class DeepSeekRuntimeTests(unittest.TestCase):
    def test_generation_explicitly_disables_thinking_and_keeps_temperature(self):
        payload = deepseek_runtime.build_chat_kwargs(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "写正文"}],
            max_tokens=1000,
            thinking=False,
            temperature=0.8,
            stream=True,
        )
        self.assertEqual({"type": "disabled"}, payload["extra_body"]["thinking"])
        self.assertEqual(0.8, payload["temperature"])
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual({"include_usage": True}, payload["stream_options"])

    def test_review_enables_thinking_and_never_sends_ignored_temperature(self):
        payload = deepseek_runtime.build_chat_kwargs(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "审稿"}],
            max_tokens=1000,
            thinking=True,
            temperature=0.15,
            reasoning_effort="max",
        )
        self.assertEqual({"type": "enabled"}, payload["extra_body"]["thinking"])
        self.assertEqual("max", payload["reasoning_effort"])
        self.assertNotIn("temperature", payload)

    def test_usage_normalization_and_cost_estimate(self):
        usage = types.SimpleNamespace(
            prompt_tokens=300,
            prompt_cache_hit_tokens=200,
            prompt_cache_miss_tokens=100,
            completion_tokens=50,
            total_tokens=350,
            completion_tokens_details=types.SimpleNamespace(reasoning_tokens=20),
        )
        normalized = deepseek_runtime.normalize_usage(usage)
        self.assertEqual(20, normalized["reasoning_tokens"])
        self.assertEqual(350, normalized["total_tokens"])
        expected = (200 * 0.02 + 100 * 1 + 50 * 2) / 1_000_000
        self.assertAlmostEqual(
            expected,
            deepseek_runtime.estimate_cost_cny("deepseek-v4-flash", normalized),
        )

    def test_preflight_checks_models_and_balance_without_chat_call(self):
        seen = {}
        client = types.SimpleNamespace(
            models=types.SimpleNamespace(
                list=lambda: types.SimpleNamespace(
                    data=[
                        types.SimpleNamespace(id="deepseek-v4-flash"),
                        types.SimpleNamespace(id="deepseek-v4-pro"),
                    ]
                )
            )
        )

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _BalanceResponse({"is_available": True, "balance_infos": []})

        result = deepseek_runtime.preflight(
            client=client,
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            required_models=["deepseek-v4-flash", "deepseek-v4-pro"],
            urlopen=urlopen,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual("https://api.deepseek.com/user/balance", seen["url"])

    def test_preflight_fails_closed_on_missing_model_or_unavailable_balance(self):
        client = types.SimpleNamespace(
            models=types.SimpleNamespace(
                list=lambda: types.SimpleNamespace(
                    data=[types.SimpleNamespace(id="deepseek-v4-flash")]
                )
            )
        )
        with self.assertRaisesRegex(
            deepseek_runtime.DeepSeekPreflightError, "deepseek-v4-pro"
        ):
            deepseek_runtime.preflight(
                client=client,
                api_key="test-key",
                base_url="https://api.deepseek.com",
                required_models=["deepseek-v4-flash", "deepseek-v4-pro"],
                urlopen=lambda *args, **kwargs: _BalanceResponse(
                    {"is_available": True}
                ),
            )

        with self.assertRaisesRegex(
            deepseek_runtime.DeepSeekPreflightError, "余额不足"
        ):
            deepseek_runtime.preflight(
                client=types.SimpleNamespace(
                    models=types.SimpleNamespace(
                        list=lambda: types.SimpleNamespace(
                            data=[types.SimpleNamespace(id="deepseek-v4-flash")]
                        )
                    )
                ),
                api_key="test-key",
                base_url="https://api.deepseek.com",
                required_models=["deepseek-v4-flash"],
                urlopen=lambda *args, **kwargs: _BalanceResponse(
                    {"is_available": False}
                ),
            )

    def test_preflight_retries_transient_model_and_balance_failures(self):
        calls = {"models": 0, "balance": 0}

        def list_models():
            calls["models"] += 1
            if calls["models"] < 3:
                raise ConnectionError("temporary model endpoint failure")
            return types.SimpleNamespace(
                data=[types.SimpleNamespace(id="deepseek-v4-flash")]
            )

        def urlopen(*args, **kwargs):
            calls["balance"] += 1
            if calls["balance"] < 2:
                raise ConnectionError("temporary balance endpoint failure")
            return _BalanceResponse({"is_available": True})

        result = deepseek_runtime.preflight(
            client=types.SimpleNamespace(
                models=types.SimpleNamespace(list=list_models)
            ),
            api_key="test-key",
            base_url="https://api.deepseek.com",
            required_models=["deepseek-v4-flash"],
            urlopen=urlopen,
            retry_delay_seconds=0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual("PASS", result["status"])
        self.assertEqual(3, calls["models"])
        self.assertEqual(2, calls["balance"])

    def test_public_dns_resolver_rejects_fake_ip_and_accepts_public_answer(self):
        responses = iter([
            _BalanceResponse(
                {
                    "Answer": [
                        {"type": 1, "data": "198.18.0.210"},
                    ]
                }
            ),
            _BalanceResponse(
                {
                    "Answer": [
                        {"type": 5, "data": "example.cloudfront.net."},
                        {"type": 1, "data": "3.173.21.63"},
                    ]
                }
            ),
        ])

        resolved = deepseek_runtime.resolve_public_ipv4(
            "api.deepseek.com",
            urlopen=lambda *args, **kwargs: next(responses),
        )
        self.assertEqual("3.173.21.63", resolved)

    def test_transient_connection_error_detection(self):
        self.assertTrue(
            deepseek_runtime.is_transient_connection_error(
                RuntimeError("模型列表读取失败：Connection error.")
            )
        )
        self.assertFalse(
            deepseek_runtime.is_transient_connection_error(
                RuntimeError("当前账号不可用模型：deepseek-v4-pro")
            )
        )

    def test_gui_usage_ledger_persists_tokens_and_estimated_cost(self):
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            app = NovelGeneratorGUI.__new__(NovelGeneratorGUI)
            app.config = {"model_usage_ledger_enabled": True}
            app._model_call_lock = threading.Lock()
            app._usage_prompt_tokens = 0
            app._usage_completion_tokens = 0
            app._usage_reasoning_tokens = 0
            app._usage_estimated_cost_cny = 0.0
            app._current_batch_job_id = "fake-job"
            app._chapter_model_call_number = 2
            app._append_batch_audit = lambda *args, **kwargs: None
            app._ui = lambda *args, **kwargs: None
            usage = {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 40,
                "completion_tokens": 20,
                "total_tokens": 120,
            }
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                app._record_model_usage(
                    usage,
                    "deepseek-v4-pro",
                    True,
                    "fake_audit",
                )
            summary_path = os.path.join(logs, "model_usage_summary.json")
            with open(summary_path, "r", encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(120, summary["total_tokens"])
            self.assertEqual(1, summary["calls_with_usage"])
            self.assertGreater(summary["estimated_cost_cny"], 0)

if __name__ == "__main__":
    unittest.main()
