"""No hidden event window, no partial hard facts, no live model calls."""
import copy
import json
import tempfile
import unittest
from unittest import mock

import continuity_guard
import generator
import source_context
from prompt_budget import PromptBudgetError
import tests.test_policy_context_budget as gui_helpers


class CanonContextBudgetTests(unittest.TestCase):
    @staticmethod
    def canon():
        return {
            "current_chapter": 89, "protagonist": "沈砚", "current_injury": "禁止上场",
            "identity_facts": ["身份硬事实"*350 + "身份尾部不得替人签字"],
            "consumed_items": [{"label": "唯一封条", "chapter": 7}],
            "forbidden_false_facts": ["错误事实约束"*350 + "禁区尾部不得解除伤势"],
            "completed_events": [f"事件{i}已完成不得重演" for i in range(1, 60)],
        }

    def test_all_completed_events_and_hard_fact_tails_are_preserved(self):
        canon = self.canon()
        before = copy.deepcopy(canon)
        with mock.patch("continuity_guard.load_canon", return_value=canon):
            text = continuity_guard.build_canon_context("", max_chars=None)
        self.assertGreater(len(text), 4500)
        for event in canon["completed_events"]:
            self.assertIn(event, text)
        self.assertIn("身份尾部不得替人签字", text)
        self.assertIn("禁区尾部不得解除伤势", text)
        self.assertIn("唯一封条（第7章耗尽）", text)
        self.assertEqual(canon, before)

    def test_budget_exact_boundary_and_fail_closed(self):
        with mock.patch("continuity_guard.load_canon", return_value=self.canon()):
            text = continuity_guard.build_canon_context("", max_chars=None)
            self.assertEqual(text, continuity_guard.build_canon_context("", max_chars=len(text)))
            with self.assertRaises(PromptBudgetError) as error:
                continuity_guard.build_canon_context("", max_chars=len(text)-1)
        self.assertEqual(error.exception.report["selected_chars"], 0)
        self.assertEqual(error.exception.report["source_chars"], len(text))
        self.assertNotIn("禁止上场", json.dumps(error.exception.report, ensure_ascii=False))

    def test_invalid_caps_and_missing_canon(self):
        with mock.patch("continuity_guard.load_canon", return_value=self.canon()):
            for cap in (True, 0, -1, "2200", 128001):
                with self.subTest(cap=cap), self.assertRaises(PromptBudgetError):
                    continuity_guard.build_canon_context("", max_chars=cap)
        with mock.patch("continuity_guard.load_canon", return_value={}):
            self.assertEqual("", continuity_guard.build_canon_context(""))

    def test_common_source_helper_keeps_complete_text(self):
        text = "全部事实"*700 + "独有尾部"
        self.assertEqual(text, source_context.require_complete_source(text, None, "正史"))
        self.assertEqual(text, source_context.require_complete_source(text, len(text), "正史"))
        self.assertEqual("", source_context.require_complete_source("", 1, "正史"))
        for bad_text, bad_label in ((None, "正史"), (text, ""), (text, None)):
            with self.subTest(text=bad_text, label=bad_label), self.assertRaises(PromptBudgetError):
                source_context.require_complete_source(bad_text, 5000, bad_label)

    def test_quality_review_injects_whole_actual_canon_or_stops(self):
        app = gui_helpers.PolicyContextBudgetTests().make_app()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root, "chars": root}, clear=True), \
             mock.patch("continuity_guard.load_canon", return_value=self.canon()), \
             mock.patch("gui_app.knowledge_manager.build_generation_context", return_value=""), \
             mock.patch("gui_app.story_architect.build_story_context", return_value=""):
            expected = continuity_guard.build_canon_context(root, max_chars=None)
            self.assertEqual("PASS", app._run_quality_gate("第90章 测试\n正文"))
            self.assertIn(expected, app.call_llm_review.call_args.args[1])
            app.call_llm_review.reset_mock()
            app.STATE_USER_CHAR_CAP = 100
            with self.assertRaises(PromptBudgetError):
                app._run_quality_gate("第90章 测试\n正文")
            app.call_llm_review.assert_not_called()

    def test_outline_injects_whole_actual_canon(self):
        helper = gui_helpers.PolicyContextBudgetTests()
        app = helper.make_app()
        patches = helper.outline_patches(app)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root}, clear=True), \
             patches[0], patches[1], patches[3], patches[4], \
             mock.patch("continuity_guard.load_canon", return_value=self.canon()):
            expected = continuity_guard.build_canon_context(root, max_chars=None)
            app._auto_generate_outline_batch(90, 1, "")
            self.assertIn(expected, app.call_llm_non_stream.call_args.args[1])
            self.assertEqual(1, app.call_llm_non_stream.call_count)

    def test_generation_budget_retains_all_events_or_stops(self):
        app = gui_helpers.PolicyContextBudgetTests().make_app()
        with mock.patch("continuity_guard.load_canon", return_value=self.canon()):
            text = continuity_guard.build_canon_context("", max_chars=None)
        system, _, report = app._apply_generation_prompt_budget(text, "写下一章")
        self.assertEqual(text, system)
        self.assertFalse(report["trimmed"])
        app.GENERATION_SYSTEM_CHAR_CAP = 100
        with self.assertRaises(PromptBudgetError):
            app._apply_generation_prompt_budget(text, "写下一章")

    def test_semantic_arc_keeps_full_canon_and_budget_error_cannot_be_warned_away(self):
        app = gui_helpers.PolicyContextBudgetTests().make_app()
        app.config["semantic_arc_audit_fail_closed"] = False
        app.call_llm_review.return_value = "FINAL: PASS\nCURRENT: PASS\nREASON: 连续"
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root, "out": root}, clear=True), \
             mock.patch("continuity_guard.load_canon", return_value=self.canon()), \
             mock.patch("gui_app.story_architect.build_story_context", return_value=""):
            expected = continuity_guard.build_canon_context(root, max_chars=None)
            self.assertEqual("PASS", app._run_semantic_arc_audit("第90章 测试\n正文", 90)["status"])
            self.assertIn(expected, app.call_llm_review.call_args.args[1])
            app.call_llm_review.reset_mock()
            app.STATE_USER_CHAR_CAP = 100
            with self.assertRaises(PromptBudgetError):
                app._run_semantic_arc_audit("第90章 测试\n正文", 90)
            app.call_llm_review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
