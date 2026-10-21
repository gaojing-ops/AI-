"""Whole fact records and source files with explicit time and budget scope."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generator
import knowledge_manager
from prompt_budget import PromptBudgetError
import tests.test_policy_context_budget as gui_helpers


class FactContextSelectionTests(unittest.TestCase):
    @staticmethod
    def db(count=12):
        rows = [{"chapter": n, "title": f"事实{n}",
                 "tail_hook": f"第{n}条完整事实。"*25 + f"独有尾句{n}。",
                 "style_phrase_counts": {}} for n in range(1, count+1)]
        return {"chapters": {str(x["chapter"]): x for x in rows},
                "recent_chapters": rows[-3:], "latest_chapter": count, "sources": {}}

    def test_full_record_tail_and_explicit_eight_chapter_window(self):
        db = self.db()
        before = copy.deepcopy(db)
        with mock.patch("knowledge_manager.load_fact_db", return_value=db):
            result = knowledge_manager.select_generation_context("", 13, None, False)
        self.assertEqual(list(range(5, 13)), result["metadata"]["selected_recent_chapters"])
        self.assertEqual([1, 2, 3, 4], result["metadata"]["older_outside_window"])
        for n in range(5, 13):
            self.assertIn(db["chapters"][str(n)]["tail_hook"], result["text"])
        self.assertNotIn("独有尾句4。", result["text"])
        self.assertNotIn("独有尾句12。", json.dumps(result["metadata"], ensure_ascii=False))
        self.assertIn("不是章节全文", result["text"])
        self.assertEqual(db, before)

    def test_historical_query_uses_durable_records_not_future_rolling_cache(self):
        db = self.db(15)
        with mock.patch("knowledge_manager.load_fact_db", return_value=db):
            result = knowledge_manager.select_generation_context("", 6, None, False)
        self.assertEqual([1, 2, 3, 4, 5], result["metadata"]["selected_recent_chapters"])
        self.assertEqual(list(range(6, 16)), result["metadata"]["future_chapters_excluded"])
        self.assertNotIn("独有尾句6。", result["text"])
        self.assertNotIn("独有尾句15。", result["text"])

    def test_source_is_full_raw_file_not_stale_cached_excerpt(self):
        db = self.db(1)
        name = knowledge_manager.SOURCE_FILES[0]
        db["sources"][name] = {"excerpt": "过时缓存摘录", "chars": 9000}
        text = "完整原文件事实。"*800 + "原文件末尾限制。"
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/name).write_text(text, encoding="utf-8")
            with mock.patch("knowledge_manager.load_fact_db", return_value=db):
                result = knowledge_manager.select_generation_context(root, 2, None)
        self.assertIn(text, result["text"])
        self.assertNotIn("过时缓存摘录", result["text"])
        row = next(x for x in result["metadata"]["sources"] if x["source_label"] == name)
        self.assertEqual(len(text), row["selected_chars"])

    def test_missing_original_never_falls_back_to_incomplete_snapshot(self):
        db = self.db(1)
        db["sources"][knowledge_manager.SOURCE_FILES[0]] = {"excerpt": "头部[中段已截断]尾部", "chars": 9000}
        with tempfile.TemporaryDirectory() as root, mock.patch("knowledge_manager.load_fact_db", return_value=db):
            with self.assertRaises(PromptBudgetError):
                knowledge_manager.select_generation_context(root, 2, None)

    def test_future_memory_sources_are_excluded_and_named(self):
        db = self.db(12)
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/knowledge_manager.SOURCE_FILES[0]).write_text("未来身份不得泄漏", encoding="utf-8")
            with mock.patch("knowledge_manager.load_fact_db", return_value=db):
                result = knowledge_manager.select_generation_context(root, 6, None)
        self.assertNotIn("未来身份不得泄漏", result["text"])
        self.assertIn("已整份排除", result["text"])
        rows = [r for r in result["metadata"]["sources"] if r["selection"] == "future_memory_source"]
        self.assertEqual(5, len(rows))

    def test_file_header_and_stage_terms_preserve_time_boundary(self):
        db = self.db(1)
        names = knowledge_manager.SOURCE_FILES
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/names[0]).write_text("第20章后\n未来发生的事件", encoding="utf-8")
            (Path(root)/names[1]).write_text("目前只知道未知身份名称，仍不能坐实。", encoding="utf-8")
            with mock.patch("knowledge_manager.load_fact_db", return_value=db), \
                 mock.patch("knowledge_manager.story_architect.blocked_terms_for_chapter", return_value=["未知身份名称"]):
                result = knowledge_manager.select_generation_context(root, 2, None)
        self.assertNotIn("未来发生的事件", result["text"])
        self.assertNotIn("未知身份名称", result["text"])
        self.assertIn("后期未公开内容", result["text"])

    def test_exact_budget_and_required_overflow_never_return_partial_facts(self):
        with mock.patch("knowledge_manager.load_fact_db", return_value=self.db(1)):
            full = knowledge_manager.select_generation_context("", 2, None, False)
            size = len(full["text"])
            self.assertEqual(full["text"], knowledge_manager.build_generation_context("", 2, size, False))
            with self.assertRaises(PromptBudgetError):
                knowledge_manager.build_generation_context("", 2, size-1, False)

    def test_style_counts_only_use_selected_prior_window(self):
        db = self.db(3)
        db["chapters"]["3"]["style_phrase_counts"] = {"猛地": 99}
        with mock.patch("knowledge_manager.load_fact_db", return_value=db):
            prior = knowledge_manager.select_generation_context("", 3, None, False)
            later = knowledge_manager.select_generation_context("", 4, None, False)
        self.assertNotIn("近期高频表达禁用", prior["text"])
        self.assertIn("近期高频表达禁用", later["text"])

    def test_invalid_inputs_and_corrupt_db_fail_closed(self):
        bad_dbs = [{"load_error": "bad"}, {"chapters": []},
                   {"chapters": {"1": {"chapter": 2}}}, {"recent_chapters": [None]}]
        for db in bad_dbs:
            with self.subTest(db=db), mock.patch("knowledge_manager.load_fact_db", return_value=db):
                with self.assertRaises(PromptBudgetError):
                    knowledge_manager.select_generation_context("", 2, None, False)
        with mock.patch("knowledge_manager.load_fact_db", return_value=self.db(1)):
            for cap in (True, 0, -1, "3200", 128001):
                with self.subTest(cap=cap), self.assertRaises(PromptBudgetError):
                    knowledge_manager.select_generation_context("", 2, cap, False)

    def test_unknown_chapter_is_explicitly_next_after_db(self):
        with mock.patch("knowledge_manager.load_fact_db", return_value=self.db(3)):
            result = knowledge_manager.select_generation_context("", 0, None, False)
        self.assertEqual(4, result["metadata"]["before_chapter"])
        self.assertEqual(0, result["metadata"]["requested_chapter"])

    def test_quality_prompt_receives_full_file_or_stops_before_model(self):
        app = gui_helpers.PolicyContextBudgetTests().make_app()
        db = self.db(1)
        text = "完整原始资料"*1500 + "源文件末尾权限不能省略。"
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(generator.DIRS, {"plot": root, "chars": root}, clear=True), \
             mock.patch("knowledge_manager.load_fact_db", return_value=db):
            (Path(root)/knowledge_manager.SOURCE_FILES[0]).write_text(text, encoding="utf-8")
            self.assertEqual("PASS", app._run_quality_gate("第2章 测试\n正文"))
            self.assertIn(text, app.call_llm_review.call_args.args[1])
            app.call_llm_review.reset_mock()
            app.STATE_USER_CHAR_CAP = 100
            with self.assertRaises(PromptBudgetError):
                app._run_quality_gate("第2章 测试\n正文")
            app.call_llm_review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
