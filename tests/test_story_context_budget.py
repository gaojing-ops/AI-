"""Full filtered contracts, full character cards, terminal offline budgets."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generator
import gui_app
import narrative_guard
import story_architect
from prompt_budget import PromptBudgetError
import tests.test_policy_context_budget as policy_helpers


class StoryContextBudgetTests(unittest.TestCase):
    @staticmethod
    def bible():
        return {
            "premise": "当前成长路径" * 800,
            "system_rules": ["不得操纵灰影", "能力代价不能省略"],
            "volume_contracts": [{"start": 1, "end": 90, "goal": "当前任务",
                                  "avoid": ["末尾规则禁止循环"]},
                                 {"start": 91, "end": 180, "goal": "未来终局秘密"}],
            "arc_contracts": [{"start": 85, "end": 90, "environment_constraints": ["只能旁观"],
                               "avoid": ["末尾阶段不得解除伤势限制"]}],
            "hard_gates": [{"before": 100, "terms": ["未来身份秘密"]}],
        }

    def make_app(self):
        return policy_helpers.PolicyContextBudgetTests().make_app()

    def test_full_story_contract_preserves_tail_but_excludes_future_arc(self):
        bible = self.bible()
        before = copy.deepcopy(bible)
        with mock.patch("story_architect.load_story_bible", return_value=bible):
            text = story_architect.build_story_context("", 90, max_chars=None)
        self.assertGreater(len(text), 4500)
        self.assertIn("末尾阶段不得解除伤势限制", text)
        self.assertIn("不得操纵灰影", text)
        self.assertNotIn("未来终局秘密", text)
        self.assertEqual(bible, before)

    def test_story_budget_is_exact_and_never_returns_half_contract(self):
        with mock.patch("story_architect.load_story_bible", return_value=self.bible()):
            text = story_architect.build_story_context("", 90, max_chars=None)
            self.assertEqual(text, story_architect.build_story_context("", 90, max_chars=len(text)))
            with self.assertRaises(PromptBudgetError) as error:
                story_architect.build_story_context("", 90, max_chars=len(text)-1)
        self.assertEqual(error.exception.report["source_chars"], len(text))
        self.assertEqual(error.exception.report["selected_chars"], 0)
        self.assertNotIn("末尾阶段不得解除伤势限制", json.dumps(error.exception.report, ensure_ascii=False))

    def test_invalid_story_budget_fails_explicitly(self):
        with mock.patch("story_architect.load_story_bible", return_value=self.bible()):
            for cap in (True, 0, -1, "5000", 128001):
                with self.subTest(cap=cap), self.assertRaises(PromptBudgetError):
                    story_architect.build_story_context("", 90, max_chars=cap)

    def test_full_visible_truth_keeps_tail_and_redacts_author_answers(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"唯一真相设定表.md"
            path.write_text("|主题|客观答案|当前认知|进度|禁出|\n"
                            "|现场|绝不泄露的客观答案|" + "已知线索"*800
                            + "未来身份秘密|未来身份秘密|禁出|\n", encoding="utf-8")
            with mock.patch("story_architect.load_story_bible", return_value=self.bible()):
                text = story_architect.build_stage_truth_context(root, 90, max_chars=None)
                self.assertIn("未列入角色当前认知的客观因果", text)
                self.assertNotIn("绝不泄露的客观答案", text)
                self.assertNotIn("未来身份秘密", text)
                with self.assertRaises(PromptBudgetError):
                    story_architect.build_stage_truth_context(root, 90, max_chars=2800)

    def test_quality_review_keeps_character_tail_and_uses_outer_budget(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            card = "人物经历。"*400 + "角色卡尾部权限仅观察，不准上场。"
            path = Path(root)/"主角.txt"
            path.write_text(card, encoding="utf-8")
            app.config["main_character_file"] = path.name
            with mock.patch.dict(generator.DIRS, {"plot": "", "chars": root}, clear=True):
                self.assertEqual("PASS", app._run_quality_gate("第90章 测试\n正文"))
                self.assertIn(card, app.call_llm_review.call_args.args[1])
                app.call_llm_review.reset_mock()
                app.STATE_USER_CHAR_CAP = 100
                with self.assertRaises(PromptBudgetError):
                    app._run_quality_gate("第90章 测试\n正文")
                app.call_llm_review.assert_not_called()

    def test_generation_truth_loader_keeps_whole_current_contract(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root}, clear=True), \
             mock.patch("story_architect.load_story_bible", return_value=self.bible()):
            text = app._load_source_of_truth_context()
        self.assertIn("末尾阶段不得解除伤势限制", text)
        self.assertNotIn("未来终局秘密", text)

    def test_narrative_prompt_does_not_recut_story_contract_at_4500(self):
        story = "阶段规则"*1400 + "合同末尾不可越权。"
        _, user, _ = narrative_guard.build_audit_prompts(
            chapter_number=90, chapter_text="第90章 测试\n正文", chapter_outline="核心事件：训练。",
            book_contract="", story_context=story, canon_context="", recent_context="",
        )
        self.assertIn(story, user)

    def test_narrative_overflow_blocks_before_call(self):
        app = self.make_app()
        app.STATE_USER_CHAR_CAP = 100
        with mock.patch.dict(generator.DIRS, {"plot": ""}, clear=True), \
             mock.patch("narrative_guard.deterministic_issues", return_value=[]), \
             mock.patch("narrative_guard.build_audit_prompts", return_value=("审核", "完整证据"*100, ["R1"])), \
             mock.patch("story_architect.build_story_context", return_value=""), \
             mock.patch("gui_app.continuity_guard.build_canon_context", return_value=""), \
             mock.patch("gui_app.knowledge_manager.build_generation_context", return_value=""):
            with self.assertRaises(PromptBudgetError):
                app._run_narrative_quality_guard(90, "第90章 测试\n正文", "核心事件：训练。")
        app.call_llm_review.assert_not_called()
        self.assertEqual("narrative_review", app._last_prompt_budget["stage"])

    def test_story_renderer_is_in_resume_code_fingerprint(self):
        app = self.make_app()
        with mock.patch("gui_app.formal_suffix_rewrite.build_preflight_fingerprint") as build:
            app._formal_suffix_code_fingerprint()
        self.assertIn("story_architect.py", build.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
