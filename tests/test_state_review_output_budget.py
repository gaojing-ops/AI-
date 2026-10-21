import unittest
from unittest.mock import Mock

import gui_app
import state_ledger


class StateOutputBudgetTests(unittest.TestCase):
    def test_phase_budgets_and_invalid_configuration(self):
        self.assertEqual(8192, state_ledger.state_review_output_limit("状态提取"))
        for phase in ("局部状态修复", "独立证据审计"):
            self.assertEqual(3600, state_ledger.state_review_output_limit(phase))
        for value in (True, "8192", 3599, 16385):
            with self.subTest(value=value), self.assertRaises(state_ledger.StateExtractionError):
                state_ledger.state_review_output_limit("状态提取", value)

    def test_real_gui_consumer_does_not_use_short_budget_for_full_extraction(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {}
        app._usage_completion_tokens = 0
        app._assemble_prompt_budget = lambda *a, **kw: ("rules", "task", {})
        app._formal_suffix_emit_progress = Mock()
        app.call_llm_review = Mock(return_value='{"chapter":6}')
        app._call_state_review("rules", "task", chapter=6, attempt=1, phase="状态提取", temp=0.05)
        self.assertEqual(8192, app.call_llm_review.call_args.kwargs["max_tokens"])
        app._call_state_review("rules", "task", chapter=6, attempt=1, phase="独立证据审计", temp=0.05)
        self.assertEqual(3600, app.call_llm_review.call_args.kwargs["max_tokens"])

    def test_only_exhausted_parse_failure_is_classified_as_truncation(self):
        error = "状态增量 JSON 解析失败：unterminated string"
        self.assertTrue(state_ledger.exhausted_json_output(error, {"completion_tokens": 8192, "output_limit": 8192}))
        self.assertFalse(state_ledger.exhausted_json_output(error, {"completion_tokens": 8100, "output_limit": 8192}))
        self.assertFalse(state_ledger.exhausted_json_output("characters证据错误", {"completion_tokens": 8192, "output_limit": 8192}))
        self.assertFalse(state_ledger.exhausted_json_output(error, {}))

    def test_full_state_has_output_room_without_hidden_reasoning_or_policy_leak(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {"deepseek_review_thinking": True}
        app._usage_completion_tokens = 0
        app._assemble_prompt_budget = lambda *a, **kw: ("rules", "task", {})
        app._formal_suffix_emit_progress = Mock()
        options = []

        def call(*args, **kwargs):
            options.append(app._deepseek_request_kwargs(model="deepseek-v4-pro", messages=[],
                                                        review=True, max_tokens=kwargs["max_tokens"]))
            return '{"chapter":6}'

        app.call_llm_review = call
        app._call_state_review("rules", "task", chapter=6, attempt=1, phase="状态提取", temp=0.05)
        self.assertEqual("disabled", options[0]["extra_body"]["thinking"]["type"])
        normal = app._deepseek_request_kwargs(model="deepseek-v4-pro", messages=[], review=True, max_tokens=8192)
        self.assertEqual("enabled", normal["extra_body"]["thinking"]["type"])
        self.assertIsNone(app._state_review_thinking_override)

    def test_state_thinking_scope_is_restored_on_exception(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {}
        app._usage_completion_tokens = 0
        app._state_review_thinking_override = True
        app._assemble_prompt_budget = lambda *a, **kw: ("rules", "task", {})
        app._formal_suffix_emit_progress = Mock()
        app.call_llm_review = Mock(side_effect=RuntimeError("test failure"))
        with self.assertRaises(RuntimeError):
            app._call_state_review("rules", "task", chapter=6, attempt=1, phase="状态提取", temp=0.05)
        self.assertIs(app._state_review_thinking_override, True)
