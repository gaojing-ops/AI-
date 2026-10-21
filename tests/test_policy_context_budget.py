"""Offline tail-preservation and no-retry checks for publishing rules."""
import copy
import hashlib
import json
import tempfile
import unittest
from unittest import mock

import generator
import gui_app
import publishing_rules
from prompt_budget import PromptBudgetError


class PolicyContextBudgetTests(unittest.TestCase):
    def make_app(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {"structured_state_enabled": False}
        app.next_chap = 90
        app.project_dir = "."
        app._estimate_token_count = len
        app._append_batch_audit = mock.Mock()
        app._commercial_contract_context = mock.Mock(return_value="")
        app._build_effective_project_rules = mock.Mock(return_value=self.rules())
        app._get_chapter_char_limits = lambda: {
            "min": 3000, "target_min": 3100, "target_max": 3400,
        }
        app._ui_progress_append = mock.Mock()
        app.call_llm_review = mock.Mock(return_value="PASS")
        app.call_llm_non_stream = mock.Mock(return_value="第90章 测试细纲")
        return app

    @staticmethod
    def rules():
        return publishing_rules.build_project_tool_rules({
            "book_title": "测试书",
            "book_brief": "明确的项目方向。" * 700 + "尾部能力规则不许操纵队友。",
            "real_people_policy": "尾部权限规则只能观察不能替人行动。",
        })

    def outline_patches(self, app):
        app._has_configured_model_provider = lambda: True
        app._get_story_volume_name = lambda chapter: "测试卷"
        app._extract_volume_outline_from_master = lambda name: "核心事件"
        app._filter_spoilers_from_text = lambda text, chapter: text
        app._is_codex_fatal_transport_error = mock.Mock(return_value=False)
        return (
            mock.patch("gui_app.generator.read_text_safe", return_value=""),
            mock.patch("gui_app.knowledge_manager.build_generation_context", return_value=""),
            mock.patch("gui_app.continuity_guard.build_canon_context", return_value=""),
            mock.patch("gui_app.story_architect.build_story_context", return_value=""),
            mock.patch("gui_app.continuity_guard.validate_outline", return_value={"status": "PASS"}),
        )

    def test_rule_builder_preserves_long_brief_without_mutating_input(self):
        config = {"book_brief": "说明" * 2000 + "尾部不得操纵灰影。"}
        before = copy.deepcopy(config)
        rules = publishing_rules.build_project_tool_rules(config)
        self.assertEqual(config, before)
        self.assertEqual(config["book_brief"], rules["theme_contract"]["core_direction"])
        self.assertIn(config["book_brief"], publishing_rules.render_rules_document(rules))

    def test_new_rule_fingerprint_invalidates_old_truncated_generation(self):
        config = {"book_title": "测试书", "book_brief": "说明" * 2000}
        legacy = {
            "ruleset_version": publishing_rules.FANQIE_RULESET_VERSION,
            "book_title": config["book_title"], "genre": "",
            "book_brief": config["book_brief"], "real_people_policy": "", "reveal_rules": {},
        }
        old_hash = hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        self.assertNotEqual(old_hash, publishing_rules.project_rules_fingerprint(config))

    def test_quality_prompt_contains_complete_policy_and_tail(self):
        app = self.make_app()
        policy = publishing_rules.render_rules_document(self.rules())
        self.assertGreater(len(policy), 5000)
        with mock.patch.dict(generator.DIRS, {"plot": "", "chars": ""}, clear=True), \
             mock.patch("gui_app.glob.glob", return_value=[]):
            self.assertEqual("PASS", app._run_quality_gate("第90章 测试\n正文"))
        app.call_llm_review.assert_called_once()
        self.assertIn(policy, app.call_llm_review.call_args.args[1])
        self.assertEqual("quality_review", app._last_prompt_budget["stage"])
        self.assertFalse(app._last_prompt_budget["trimmed"])

    def test_quality_overflow_blocks_before_model(self):
        app = self.make_app()
        app.STATE_USER_CHAR_CAP = 100
        with mock.patch.dict(generator.DIRS, {"plot": "", "chars": ""}, clear=True), \
             mock.patch("gui_app.glob.glob", return_value=[]):
            with self.assertRaises(PromptBudgetError):
                app._run_quality_gate("第90章 测试\n正文")
        app.call_llm_review.assert_not_called()
        self.assertEqual("BLOCKED", app._last_prompt_budget["status"])

    def test_outline_prompt_contains_full_policy(self):
        app = self.make_app()
        patches = self.outline_patches(app)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root}, clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4] as validate:
            self.assertEqual("第90章 测试细纲", app._auto_generate_outline_batch(90, 1, ""))
        app.call_llm_non_stream.assert_called_once()
        validate.assert_called_once()
        self.assertIn(publishing_rules.render_rules_document(self.rules()),
                      app.call_llm_non_stream.call_args.args[1])
        self.assertEqual("rolling_outline", app._last_prompt_budget["stage"])

    def test_outline_overflow_propagates_instead_of_becoming_empty_outline(self):
        app = self.make_app()
        app.STATE_USER_CHAR_CAP = 100
        patches = self.outline_patches(app)
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root}, clear=True), \
             patches[0], patches[1], patches[2], patches[3], patches[4] as validate:
            with self.assertRaises(PromptBudgetError):
                app._auto_generate_outline_batch(90, 1, "")
        app.call_llm_non_stream.assert_not_called()
        validate.assert_not_called()
        app._is_codex_fatal_transport_error.assert_not_called()
        app._ui_progress_append.assert_not_called()

    def test_generation_system_builder_and_budget_keep_rule_tail(self):
        app = self.make_app()
        app._load_source_of_truth_context = lambda: ""
        app._build_reveal_budget = lambda chapter: ""
        app.plot_vars = {}
        app.chars_vars = {}
        app.world_vars = {}
        app.is_batch_running = False
        app.latest_filepath = ""
        with mock.patch.dict(generator.DIRS, {"plot": ""}, clear=True), \
             mock.patch("gui_app.generator.read_text_safe", return_value=""), \
             mock.patch("gui_app.rag_engine.SimpleLocalRAG") as rag:
            rag.return_value.search.return_value = []
            system = app.build_system_prompt_gui()
            actual, _, report = app._apply_generation_prompt_budget(system, "写第90章")
        self.assertIn(publishing_rules.render_rules_document(self.rules()), actual)
        self.assertFalse(report["trimmed"])

    def test_rule_module_is_in_resume_code_fingerprint(self):
        app = self.make_app()
        with mock.patch("gui_app.formal_suffix_rewrite.build_preflight_fingerprint") as build:
            app._formal_suffix_code_fingerprint()
        self.assertIn("publishing_rules.py", build.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
