import copy
import ast
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import commercial_planner
import gui_app
from gui_app import NovelGeneratorGUI
import source_context
from prompt_budget import PromptBudgetError


class WholeSourceSelectionTests(unittest.TestCase):
    @staticmethod
    def source(label, text, required=False, **kwargs):
        return {"label": label, "text": text, "category": "test",
                "required": required, **kwargs}

    def test_complete_tail_and_metadata_without_prose(self):
        content = "完整设定" * 800 + "尾部独有规则"
        result = source_context.select_whole_sources([self.source("规则", content, True)], 5000)
        self.assertIn(content, result["text"])
        metadata = result["metadata"]
        self.assertTrue(metadata["full_coverage"])
        self.assertEqual(metadata["sources"][0]["selected_chars"], len(content))
        self.assertNotIn("尾部独有规则", json.dumps(metadata, ensure_ascii=False))

    def test_required_overflow_fails_without_partial_prompt(self):
        with self.assertRaises(source_context.SourceBudgetError) as raised:
            source_context.select_whole_sources([self.source("必需", "完整" * 100, True)], 50)
        self.assertEqual(raised.exception.metadata["status"], "BLOCKED")
        self.assertEqual(raised.exception.metadata["used_chars"], 0)
        self.assertEqual(raised.exception.metadata["sources"][0]["selected_chars"], 0)

    def test_optional_is_omitted_whole_and_named(self):
        result = source_context.select_whole_sources([
            self.source("必需", "规则尾部", True),
            self.source("大资料", "绝不出现一半" * 100),
        ], 150)
        self.assertIn("规则尾部", result["text"])
        self.assertNotIn("绝不出现一半", result["text"])
        self.assertIn("预算未纳入完整来源：大资料", result["text"])
        self.assertFalse(result["metadata"]["full_coverage"])
        self.assertEqual(result["metadata"]["omitted_labels"], ["大资料"])

    def test_optional_priority_cannot_displace_later_required_source(self):
        result = source_context.select_whole_sources([
            self.source("可选", "可选全文" * 100),
            self.source("必需", "不可丢的底线", True),
        ], 130)
        self.assertIn("不可丢的底线", result["text"])
        self.assertNotIn("可选全文", result["text"])

    def test_future_source_stays_out_of_prompt_without_mutation(self):
        sources = [self.source("未来", "未来动作", eligible=False, reason="future_action_board")]
        before = copy.deepcopy(sources)
        result = source_context.select_whole_sources(sources, 100)
        self.assertEqual(result["text"], "")
        self.assertEqual(sources, before)
        self.assertEqual(result["metadata"]["sources"][0]["selection"], "future_action_board")

    def test_insufficient_notice_budget_is_not_silently_dropped(self):
        with self.assertRaises(source_context.SourceBudgetError):
            source_context.select_whole_sources([self.source("必须注明省略的资料", "太长" * 100)], 8)

    def test_invalid_budgets_and_sources(self):
        for budget in (True, 0, -1, "5000", 128001):
            with self.subTest(budget=budget), self.assertRaises(source_context.SourceBudgetError):
                source_context.select_whole_sources([], budget)
        for sources in ([None], [self.source("x", None)],
                        [self.source("x", "a"), self.source("x", "b")],
                        [self.source("x", "", True)],
                        [self.source(str(n), "a") for n in range(65)]):
            with self.subTest(sources=sources), self.assertRaises(source_context.SourceBudgetError):
                source_context.select_whole_sources(sources, 5000)

    def test_exact_budget_and_empty_sources(self):
        expected = "【规则】\n整段"
        result = source_context.select_whole_sources([self.source("规则", "整段", True)], len(expected))
        self.assertEqual(result["text"], expected)
        self.assertEqual(result["metadata"]["used_chars"], len(expected))
        self.assertEqual(source_context.select_whole_sources([], 1)["text"], "")

    def test_source_failure_uses_existing_terminal_budget_type(self):
        error = source_context.SourceBudgetError("不可截断", {"status": "BLOCKED"})
        self.assertIsInstance(error, PromptBudgetError)
        self.assertEqual(error.report, error.metadata)


class CommercialWholeContextTests(unittest.TestCase):
    @staticmethod
    def app(blueprint=None, debt=False):
        return SimpleNamespace(config={"revision_debt_enabled": debt}, latest_chap=89,
                               _load_commercial_blueprint=lambda: blueprint or {},
                               _append_batch_audit=mock.Mock())

    def test_action_board_beyond_old_2600_keeps_tail(self):
        text = "# 第87章后商业行动单\n" + "完整行动" * 750 + "尾部约束必须保留"
        app = self.app()
        with mock.patch("gui_app.generator.read_text_safe", return_value=text):
            result = NovelGeneratorGUI._commercial_contract_context(app, 5000)
        self.assertIn(text, result)
        self.assertTrue(app._last_commercial_context_selection["full_coverage"])
        self.assertNotIn("尾部约束必须保留", json.dumps(app._append_batch_audit.call_args.args[0], ensure_ascii=False))

    def test_blueprint_preserves_tail_instead_of_slicing(self):
        blueprint = {"synopsis": "长简介" * 1600, "must_avoid": ["尾部禁止万能系统"]}
        full = commercial_planner.render_blueprint(blueprint, max_chars=None)
        app = self.app(blueprint)
        with mock.patch("gui_app.generator.read_text_safe", return_value=""):
            result = NovelGeneratorGUI._commercial_contract_context(app, len(full) + 100)
        self.assertIn(full, result)
        self.assertIn("尾部禁止万能系统", result)

    def test_combined_required_sources_overflow_is_reported_once(self):
        app = self.app({"synopsis": "蓝图" * 200})
        with mock.patch("gui_app.generator.read_text_safe", return_value="行动" * 200):
            with self.assertRaises(source_context.SourceBudgetError):
                NovelGeneratorGUI._commercial_contract_context(app, 500)
        self.assertEqual(app._last_commercial_context_selection["status"], "BLOCKED")
        self.assertEqual(app._append_batch_audit.call_count, 1)

    def test_debts_can_use_outer_budget_without_2600_inner_cap(self):
        ledger = {"items": [{"id": f"debt_{n}", "status": "OPEN", "opened_chapter": 1,
                             "due_by": 10, "action": "完整行动要求" * 25}
                            for n in range(17)]}
        app = self.app(debt=True)
        with mock.patch("gui_app.generator.read_text_safe", return_value=""), \
             mock.patch("gui_app.commercial_reviewer.load_revision_debt", return_value=ledger):
            result = NovelGeneratorGUI._commercial_contract_context(app, 5000)
        self.assertIn(ledger["items"][-1]["action"], result)
        self.assertTrue(app._last_commercial_context_selection["full_coverage"])
        self.assertEqual(app._last_commercial_context_selection["debt_selection"]["selected_count"], 17)

    def test_partial_debt_list_never_claims_full_source_coverage(self):
        ledger = {"items": [{"id": f"debt_{n}", "status": "OPEN", "opened_chapter": 1,
                             "due_by": 10, "action": "必须完整的很长行动要求" * 200}
                            for n in range(17)]}
        app = self.app(debt=True)
        with mock.patch("gui_app.generator.read_text_safe", return_value=""), \
             mock.patch("gui_app.commercial_reviewer.load_revision_debt", return_value=ledger):
            with self.assertRaises(source_context.SourceBudgetError):
                NovelGeneratorGUI._commercial_contract_context(app, 5000)
        self.assertEqual(app._last_commercial_context_selection["status"], "BLOCKED")
        self.assertFalse(app._last_commercial_context_selection["full_coverage"])
        self.assertEqual(app._append_batch_audit.call_count, 1)

    def test_bounded_blueprint_renderer_rejects_half_output(self):
        with self.assertRaisesRegex(commercial_planner.BlueprintBudgetError, "未截断") as raised:
            commercial_planner.render_blueprint({"synopsis": "规则" * 200}, max_chars=30)
        self.assertIsInstance(raised.exception, PromptBudgetError)

    def test_renderer_is_part_of_resume_code_fingerprint(self):
        app = NovelGeneratorGUI.__new__(NovelGeneratorGUI)
        with mock.patch.object(gui_app.formal_suffix_rewrite, "build_preflight_fingerprint") as fingerprint:
            app._formal_suffix_code_fingerprint()
        self.assertNotEqual("missing", fingerprint.call_args.args[0]["commercial_planner.py"])

    def test_existing_worker_budget_branch_stops_after_one_source_error(self):
        # Keep multiline prompt literals untouched; their zero-indent content
        # makes dedent unsuitable for extracting a class method.
        tree = ast.parse("class WorkerBudgetHarness:\n" + inspect.getsource(NovelGeneratorGUI.batch_worker))
        branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                    and "isinstance(e" in ast.unparse(node.test)
                    and "prompt_budget.PromptBudgetError" in ast.unparse(node.test)]
        self.assertEqual(len(branches), 1)
        harness = ast.parse("for retry in range(3):\n    attempts.append(retry)\n")
        harness.body[0].body.append(copy.deepcopy(branches[0]))
        ast.fix_missing_locations(harness)
        for error in (source_context.SourceBudgetError("来源不足"),
                      commercial_planner.BlueprintBudgetError("蓝图不足")):
            pause = mock.Mock()
            ui = mock.Mock()
            scope = {"e": error, "err_msg": str(error), "chapter_content": "已有候选",
                     "_pause_without_retry": pause, "max_retries": 3, "retry_count": 0,
                     "attempts": [], "self": SimpleNamespace(_ui_progress_append=ui),
                     "prompt_budget": gui_app.prompt_budget, "chapter_budget": gui_app.chapter_budget}
            exec(compile(harness, "worker_budget_branch", "exec"), scope)
            self.assertEqual(scope["attempts"], [0])
            self.assertEqual(scope["retry_count"], 3)
            pause.assert_called_once_with(str(error), "已有候选")
            ui.assert_called_once()

    def test_future_board_does_not_use_budget_or_leak(self):
        app = self.app()
        app.latest_chap = 2
        with mock.patch("gui_app.generator.read_text_safe", return_value="# 第87章后商业行动单\n未来" * 100):
            result = NovelGeneratorGUI._commercial_contract_context(app, 500)
        self.assertEqual(result, "")
        self.assertEqual(app._last_commercial_context_selection["eligible_count"], 0)


if __name__ == "__main__":
    unittest.main()
