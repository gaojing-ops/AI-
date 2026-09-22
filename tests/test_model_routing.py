import types
import os
import hashlib
import tempfile
import unittest
from unittest import mock

import gui_app
import codex_sol_adapter


class ModelRoutingTests(unittest.TestCase):
    def test_gui_exposes_deepseek_and_exact_codex_sol_presets(self):
        self.assertEqual(
            ["DeepSeek-V4", "Codex GPT-5.6-sol"],
            list(gui_app.MODEL_PRESETS),
        )
        codex = gui_app.MODEL_PRESETS["Codex GPT-5.6-sol"]
        self.assertEqual(gui_app.PROVIDER_CODEX_SOL, codex["provider"])
        self.assertEqual("gpt-5.6-sol", codex["model"])
        self.assertEqual("gpt-5.6-sol", codex["review_model"])

    def test_missing_provider_migrates_to_deepseek_and_unknown_fails_closed(self):
        self.assertEqual(
            gui_app.PROVIDER_DEEPSEEK,
            gui_app.normalize_model_provider(None),
        )
        with self.assertRaisesRegex(gui_app.ModelProviderConfigError, "未知模型提供方"):
            gui_app.normalize_model_provider("mystery-provider")
        with self.assertRaisesRegex(gui_app.ModelProviderConfigError, "空值"):
            gui_app.normalize_model_provider("")

    def test_codex_model_is_exact_and_ignores_deepseek_model_fields(self):
        preset = gui_app.MODEL_PRESETS["Codex GPT-5.6-sol"]
        config = {
            "model": "deepseek-v4-flash",
            "review_model": "deepseek-v4-pro",
        }
        self.assertEqual(
            "gpt-5.6-sol",
            gui_app.resolve_preset_model(preset, config, review=False),
        )
        self.assertEqual(
            "gpt-5.6-sol",
            gui_app.resolve_preset_model(preset, config, review=True),
        )

    def test_legacy_deepseek_aliases_migrate_to_v4_defaults(self):
        preset = gui_app.MODEL_PRESETS["DeepSeek-V4"]
        config = {
            "model": "deepseek-chat",
            "review_model": "deepseek-reasoner",
        }
        self.assertEqual(
            "deepseek-v4-flash",
            gui_app.resolve_preset_model(preset, config, review=False),
        )
        self.assertEqual(
            "deepseek-v4-pro",
            gui_app.resolve_preset_model(preset, config, review=True),
        )

    def test_explicit_v4_models_are_honored(self):
        preset = gui_app.MODEL_PRESETS["DeepSeek-V4"]
        config = {
            "model": "deepseek-v4-pro",
            "review_model": "deepseek-v4-flash",
        }
        self.assertEqual(
            "deepseek-v4-pro",
            gui_app.resolve_preset_model(preset, config, review=False),
        )
        self.assertEqual(
            "deepseek-v4-flash",
            gui_app.resolve_preset_model(preset, config, review=True),
        )

    def test_review_calls_use_the_review_model(self):
        captured = {}
        dummy = types.SimpleNamespace()
        dummy.get_review_model_name = lambda: "deepseek-v4-pro"

        def call_non_stream(*args, **kwargs):
            captured.update(kwargs)
            return "PASS"

        dummy.call_llm_non_stream = call_non_stream
        result = gui_app.NovelGeneratorGUI.call_llm_review(
            dummy, "system", "user", max_tokens=500
        )
        self.assertEqual("PASS", result)
        self.assertEqual("deepseek-v4-pro", captured["model_name"])
        self.assertTrue(captured["review"])
        self.assertEqual("review_or_audit", captured["task_type"])

    def test_short_structured_review_disables_thinking(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "deepseek_review_thinking": True,
            "deepseek_review_thinking_min_tokens": 6000,
            "deepseek_review_reasoning_effort": "high",
        }
        short = app._deepseek_request_kwargs(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "输出 JSON"}],
            max_tokens=2600,
            temperature=0.0,
            review=True,
        )
        roomy = app._deepseek_request_kwargs(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "深度审稿"}],
            max_tokens=6200,
            temperature=0.0,
            review=True,
        )
        self.assertEqual({"type": "disabled"}, short["extra_body"]["thinking"])
        self.assertEqual(0.0, short["temperature"])
        self.assertEqual({"type": "enabled"}, roomy["extra_body"]["thinking"])
        self.assertNotIn("temperature", roomy)

    def test_empty_thinking_review_retries_once_without_thinking(self):
        calls = []
        responses = iter([
            types.SimpleNamespace(
                usage=None,
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=""))],
            ),
            types.SimpleNamespace(
                usage=None,
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="PASS"))],
            ),
        ])

        def create(**kwargs):
            calls.append(kwargs)
            return next(responses)

        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "max_tokens": 8192,
            "deepseek_review_thinking": True,
            "deepseek_review_thinking_min_tokens": 1,
            "deepseek_review_reasoning_effort": "high",
        }
        app.get_client = lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)
            )
        )
        app._before_model_call = lambda *args, **kwargs: None
        app._record_model_usage = lambda *args, **kwargs: None
        app._append_batch_audit = lambda *args, **kwargs: None

        result = app.call_llm_non_stream(
            "system",
            "user",
            max_tokens=7000,
            model_name="deepseek-v4-pro",
            review=True,
            task_type="test_review",
        )
        self.assertEqual("PASS", result)
        self.assertEqual("enabled", calls[0]["extra_body"]["thinking"]["type"])
        self.assertEqual("disabled", calls[1]["extra_body"]["thinking"]["type"])

    def test_codex_without_deepseek_key_never_constructs_deepseek_client(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "model_provider": gui_app.PROVIDER_CODEX_SOL,
            "api_key": "YOUR_DEEPSEEK_API_KEY_HERE",
            "max_tokens": 8192,
        }
        app._estimate_model_call_reserve_cny = mock.Mock(return_value=0.0)
        app._before_model_call = mock.Mock()
        app._call_codex_text = mock.Mock(return_value="SOL 正文")
        app.get_client = mock.Mock(side_effect=AssertionError("must not touch DeepSeek"))

        result = app.call_llm_non_stream(
            "system", "user", model_name="deepseek-v4-pro", review=True
        )

        self.assertEqual("SOL 正文", result)
        app.get_client.assert_not_called()
        self.assertEqual("gpt-5.6-sol", app.get_review_model_name())
        self.assertEqual(
            "generation", app._call_codex_text.call_args.kwargs["task_type"]
        )

    def test_switching_back_to_deepseek_preserves_its_models(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "model_provider": gui_app.PROVIDER_CODEX_SOL,
            "model": "deepseek-v4-pro",
            "review_model": "deepseek-v4-flash",
        }
        self.assertEqual("gpt-5.6-sol", app.get_model_name())
        self.assertEqual("deepseek-v4-pro", app.config["model"])
        self.assertEqual("deepseek-v4-flash", app.config["review_model"])
        app.config["model_provider"] = gui_app.PROVIDER_DEEPSEEK
        self.assertEqual("deepseek-v4-pro", app.get_model_name())
        self.assertEqual("deepseek-v4-flash", app.get_review_model_name())

    def test_provider_preflight_cache_identity_is_isolated_and_hash_bound(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        with tempfile.TemporaryDirectory() as tmp:
            executable = os.path.join(tmp, "codex.exe")
            with open(executable, "wb") as handle:
                handle.write(b"version-one")
            app.config = {
                "model_provider": gui_app.PROVIDER_CODEX_SOL,
                "api_key": "",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
                "review_model": "deepseek-v4-pro",
            }
            app._resolve_codex_executable = lambda: executable
            codex_key_one = app._provider_preflight_cache_key()
            with open(executable, "wb") as handle:
                handle.write(b"version-two")
            codex_key_two = app._provider_preflight_cache_key()
            app.config["model_provider"] = gui_app.PROVIDER_DEEPSEEK
            app._get_runtime_api_key = lambda: "secret-not-persisted"
            deepseek_key = app._provider_preflight_cache_key()

        self.assertNotEqual(codex_key_one, codex_key_two)
        self.assertNotEqual(codex_key_two, deepseek_key)
        self.assertTrue(codex_key_two.startswith("codex_sol|"))
        self.assertTrue(deepseek_key.startswith("deepseek|"))

    def test_codex_preflight_uses_cli_runtime_and_not_deepseek_client(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "model_provider": gui_app.PROVIDER_CODEX_SOL,
            "codex_timeout_seconds": 900,
            "deepseek_preflight_cache_seconds": 300,
            "api_key": "YOUR_DEEPSEEK_API_KEY_HERE",
        }
        app._provider_preflight_cache = {}
        app.get_client = mock.Mock(side_effect=AssertionError("DeepSeek touched"))
        with tempfile.TemporaryDirectory() as tmp:
            executable = os.path.join(tmp, "codex.exe")
            with open(executable, "wb") as handle:
                handle.write(b"approved-cli")
            app._resolve_codex_executable = lambda: executable
            app._codex_evidence_dir = lambda _task: tmp
            response = {
                "text": "READY",
                "model": "gpt-5.6-sol",
                "provider": gui_app.codex_sol_runtime.PROVIDER,
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                "evidence_path": os.path.join(tmp, "receipt.json"),
                "evidence_sha256": "b" * 64,
                "provenance_basis": "command_bound_official_cli",
            }
            with mock.patch.object(
                gui_app.codex_sol_runtime, "preflight", return_value=response
            ) as preflight:
                result = app._run_provider_preflight(force=True)

        self.assertEqual("PASS", result["status"])
        self.assertEqual("gpt-5.6-sol", result["model"])
        self.assertEqual(
            hashlib.sha256(b"approved-cli").hexdigest(),
            result["executable_sha256"],
        )
        preflight.assert_called_once()
        app.get_client.assert_not_called()

    def test_codex_protocol_is_fatal_but_timeout_remains_retryable(self):
        protocol = codex_sol_adapter.CodexSolProtocolError("bad protocol")
        timeout = codex_sol_adapter.CodexSolTimeoutError("timed out")
        self.assertTrue(
            gui_app.NovelGeneratorGUI._is_codex_fatal_transport_error(protocol)
        )
        self.assertFalse(
            gui_app.NovelGeneratorGUI._is_codex_fatal_transport_error(timeout)
        )
        self.assertTrue(gui_app.NovelGeneratorGUI._is_codex_timeout_error(timeout))


if __name__ == "__main__":
    unittest.main()
