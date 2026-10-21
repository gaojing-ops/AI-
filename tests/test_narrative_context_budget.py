"""Whole audit sources and one bounded readjudication, no live model calls."""
from contextlib import ExitStack
import json
import unittest
from unittest import mock

import generator
import narrative_guard
from prompt_budget import PromptBudgetError
import tests.test_narrative_guard as audit_helpers
import tests.test_policy_context_budget as gui_helpers


class NarrativeContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = audit_helpers.NarrativeGuardTests()
        self.fixture.setUp()
        self.requirements, self.payload = self.fixture.valid_payload()

    def make_app(self):
        app = gui_helpers.PolicyContextBudgetTests().make_app()
        app.get_review_model_name = lambda: "offline-review-test"
        app.call_llm_review = mock.Mock(return_value=json.dumps(self.payload, ensure_ascii=False))
        return app

    def sources(self, **overrides):
        values = dict(chapter_number=1, chapter_text=self.fixture.chapter,
                      chapter_outline=self.fixture.outline, book_contract="", story_context="",
                      canon_context="", recent_context="")
        values.update(overrides)
        return values

    def guard_context(self, app, *, book="", canon="", recent=""):
        stack = ExitStack()
        stack.enter_context(mock.patch.dict(generator.DIRS, {"plot": ""}, clear=True))
        stack.enter_context(mock.patch("gui_app.story_architect.build_story_context", return_value=""))
        stack.enter_context(mock.patch("gui_app.continuity_guard.build_canon_context", return_value=canon))
        stack.enter_context(mock.patch("gui_app.knowledge_manager.build_generation_context", return_value=recent))
        stack.enter_context(mock.patch("narrative_guard.load_recent_audits", return_value=[]))
        stack.enter_context(mock.patch("narrative_guard.evaluate_rolling_drift", return_value=[]))
        app._commercial_contract_context = mock.Mock(return_value=book)
        return stack

    def test_all_audit_sources_keep_tail_and_input_unchanged(self):
        for field, old_cap in (("book_contract", 7000), ("canon_context", 4500),
                               ("recent_context", 2600), ("chapter_outline", 5000)):
            text = "完整来源。" * (old_cap//5+100) + f"{field}唯一尾句不能省略。"
            values = self.sources(**{field: text})
            before = dict(values)
            with self.subTest(field=field):
                _, user, _ = narrative_guard.build_audit_prompts(**values)
                self.assertIn(text, user)
                self.assertEqual(values, before)

    def test_empty_sources_still_use_fallback(self):
        _, user, _ = narrative_guard.build_audit_prompts(**self.sources())
        self.assertIn("【最近章节事实】\n暂无", user)

    def test_first_audit_overflow_blocks_before_model_for_each_source(self):
        for field in ("book", "canon", "recent", "outline"):
            app = self.make_app()
            text = "不能丢弃的来源" * 2500 + "尾部约束。"
            app.STATE_USER_CHAR_CAP = 10000
            context = {field: text} if field != "outline" else {}
            with self.subTest(field=field), self.guard_context(app, **context):
                with self.assertRaises(PromptBudgetError):
                    app._run_narrative_quality_guard(1, self.fixture.chapter,
                                                    "第1章 测试\n核心事件："+text
                                                    if field == "outline" else self.fixture.outline)
                app.call_llm_review.assert_not_called()
                self.assertEqual("BLOCKED", app._last_prompt_budget["status"])

    def test_good_audit_still_uses_one_call_and_preserves_evidence(self):
        app = self.make_app()
        with self.guard_context(app):
            result = app._run_narrative_quality_guard(1, self.fixture.chapter, self.fixture.outline)
        self.assertEqual("PASS", result["verdict"])
        app.call_llm_review.assert_called_once()
        self.assertIn(self.fixture.chapter, app.call_llm_review.call_args.args[1])

    def test_one_correction_preserves_previous_json_tail_and_receipt_prompt(self):
        app = self.make_app()
        payload = dict(self.payload, diagnostic="上轮JSON证据"*1300 + "JSON独有尾部")
        app.call_llm_review.side_effect = [json.dumps(payload, ensure_ascii=False),
                                         json.dumps(self.payload, ensure_ascii=False)]
        normalized, _ = narrative_guard.validate_audit(self.payload, chapter_text=self.fixture.chapter,
                                                       requirements=self.requirements)
        with self.guard_context(app), \
             mock.patch("narrative_guard.audit_admits_self_error", return_value=True), \
             mock.patch("narrative_guard.validate_audit", side_effect=[(normalized, ["引文错误"]), (normalized, [])]), \
             mock.patch("narrative_guard.build_review_receipt", return_value={}) as receipt:
            app._run_narrative_quality_guard(1, self.fixture.chapter, self.fixture.outline)
        self.assertEqual(2, app.call_llm_review.call_count)
        correction = app.call_llm_review.call_args.args[1]
        self.assertIn(json.dumps(payload, ensure_ascii=False), correction)
        self.assertEqual(correction, receipt.call_args.kwargs["user_prompt"])
        self.assertEqual("narrative_readjudication", app._last_prompt_budget["stage"])

    def test_correction_overflow_stops_after_first_call(self):
        app = self.make_app()
        _, initial, _ = narrative_guard.build_audit_prompts(**self.sources())
        app.STATE_USER_CHAR_CAP = len(initial)+100
        normalized, _ = narrative_guard.validate_audit(self.payload, chapter_text=self.fixture.chapter,
                                                       requirements=self.requirements)
        with self.guard_context(app), \
             mock.patch("narrative_guard.audit_admits_self_error", return_value=True), \
             mock.patch("narrative_guard.validate_audit", return_value=(normalized, ["引文错误"])):
            with self.assertRaises(PromptBudgetError):
                app._run_narrative_quality_guard(1, self.fixture.chapter, self.fixture.outline)
        self.assertEqual(1, app.call_llm_review.call_count)
        self.assertEqual("narrative_readjudication", app._last_prompt_budget["stage"])
        self.assertEqual("BLOCKED", app._last_prompt_budget["status"])

    def test_correction_failure_does_not_start_a_third_call(self):
        app = self.make_app()
        normalized, _ = narrative_guard.validate_audit(self.payload, chapter_text=self.fixture.chapter,
                                                       requirements=self.requirements)
        with self.guard_context(app), \
             mock.patch("narrative_guard.audit_admits_self_error", return_value=True), \
             mock.patch("narrative_guard.validate_audit", return_value=(normalized, ["真实缺口"])):
            with self.assertRaises(narrative_guard.NarrativeGuardError):
                app._run_narrative_quality_guard(1, self.fixture.chapter, self.fixture.outline)
        self.assertEqual(2, app.call_llm_review.call_count)


if __name__ == "__main__":
    unittest.main()
