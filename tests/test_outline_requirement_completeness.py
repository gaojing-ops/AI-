"""Whole R contracts, stable IDs, and terminal budgets; no live models."""
import json
import unittest
from unittest import mock

import narrative_guard
from prompt_budget import PromptBudgetError
import tests.test_narrative_context_budget as audit_helpers


class OutlineRequirementCompletenessTests(unittest.TestCase):
    @staticmethod
    def outline(count=9):
        return "第1章 完整合同\n" + "\n".join(
            f"{label}：完整动作{i}。" for i, label in enumerate(narrative_guard.REQUIREMENT_LABELS[:count], 1)
        )

    def test_all_requirements_beyond_seven_keep_stable_ids(self):
        rows = narrative_guard.extract_outline_requirements(self.outline())
        self.assertEqual([f"R{i}" for i in range(1, 10)], [row["id"] for row in rows])
        self.assertEqual("完整动作9。", rows[-1]["requirement"])
        self.assertEqual(rows, narrative_guard.extract_outline_requirements(self.outline()))

    def test_single_requirement_longer_than_1200_keeps_its_tail(self):
        value = "完整动作与代价。"*300 + "独有尾部必须解除授权前先交接。"
        rows = narrative_guard.extract_outline_requirements("第1章 长合同\n核心事件："+value)
        self.assertEqual(value, rows[0]["requirement"])

    def test_continuations_and_duplicate_labels_are_not_overwritten(self):
        rows = narrative_guard.extract_outline_requirements(
            "第1章 合同\n核心事件：第一动作。\n补充代价不能略。\n"
            "核心事件：第二动作。\n第二动作的权限尾句。\n章末钩子：新信息。"
        )
        self.assertEqual(3, len(rows))
        self.assertEqual("第一动作。\n补充代价不能略。", rows[0]["requirement"])
        self.assertEqual("第二动作。\n第二动作的权限尾句。", rows[1]["requirement"])

    def test_explicit_item_budget_fails_instead_of_returning_subset(self):
        rows = narrative_guard.extract_outline_requirements(self.outline(), max_items=9)
        self.assertEqual(9, len(rows))
        with self.assertRaises(PromptBudgetError) as error:
            narrative_guard.extract_outline_requirements(self.outline(), max_items=7)
        self.assertEqual(9, error.exception.report["required_items"])
        self.assertEqual(0, error.exception.report["selected_items"])
        self.assertNotIn("完整动作", json.dumps(error.exception.report, ensure_ascii=False))

    def test_invalid_item_budgets_and_source_types_fail(self):
        for cap in (True, 0, -1, "7"):
            with self.subTest(cap=cap), self.assertRaises(PromptBudgetError):
                narrative_guard.extract_outline_requirements(self.outline(), max_items=cap)
        for source in (False, [], 1):
            with self.subTest(source=source), self.assertRaises(PromptBudgetError):
                narrative_guard.extract_outline_requirements(source)
        with self.assertRaises(PromptBudgetError):
            narrative_guard.extract_outline_requirements("第1章 巨大合同\n核心事件："+"事实"*65000)

    def test_fallback_fields_and_unstructured_lines_are_not_limited_to_three(self):
        fields = "第1章 合同\n"+"\n".join(f"事项{i}：动作{i}。" for i in range(1, 6))
        self.assertEqual(5, len(narrative_guard.extract_outline_requirements(fields)))
        prose = "第1章 合同\n"+"\n".join(f"完成动作{i}并承担其代价。" for i in range(1, 6))
        self.assertEqual(5, len(narrative_guard.extract_outline_requirements(prose)))
        self.assertEqual([], narrative_guard.extract_outline_requirements(""))

    def test_multiple_chapter_contracts_fail_before_audit(self):
        with self.assertRaises(PromptBudgetError):
            narrative_guard.extract_outline_requirements(self.outline()+"\n第2章 次日\n核心事件：其他动作。")

    def test_missing_ninth_requirement_is_rejected_and_full_evidence_passes(self):
        requirements = narrative_guard.extract_outline_requirements(self.outline())
        chapter = "第1章 完整合同\n"+"".join(f"完整动作{i}。" for i in range(1, 10))
        payload = {
            "verdict": "PASS", "scores": {field: 90 for field in narrative_guard.SCORE_FIELDS},
            "outline_coverage": [{"id": f"R{i}", "status": "MET", "draft_quote": f"完整动作{i}。",
                                  "reason": "存在完整动作证据"} for i in range(1, 10)],
            "mainline_progress": "逐项完成实际动作并形成不可逆变化", "progress_evidence_quote": "完整动作9。",
            "drift_flags": [], "nonsense_flags": [], "fail_reasons": [], "summary": "完整兑现",
        }
        complete, issues = narrative_guard.validate_audit(payload, chapter_text=chapter, requirements=requirements)
        self.assertEqual([], issues)
        self.assertEqual("PASS", complete["verdict"])
        payload["outline_coverage"].pop()
        missing, issues = narrative_guard.validate_audit(payload, chapter_text=chapter, requirements=requirements)
        self.assertTrue(any("遗漏细纲要求 R9" in item for item in issues))
        # The normalized verdict preserves the model's claim; nonempty issues
        # are the program's rejection. Never mistake this claim for acceptance.
        self.assertEqual("PASS", missing["verdict"])
        helper = audit_helpers.NarrativeContextBudgetTests()
        helper.setUp()
        app = helper.make_app()
        app.call_llm_review.return_value = json.dumps(payload, ensure_ascii=False)
        with helper.guard_context(app):
            with self.assertRaisesRegex(narrative_guard.NarrativeGuardError, "遗漏细纲要求 R9"):
                app._run_narrative_quality_guard(1, chapter, self.outline())
        app.call_llm_review.assert_called_once()

    def test_full_requirements_enter_prompt_and_overflow_never_calls_model(self):
        _, user, rows = narrative_guard.build_audit_prompts(
            chapter_number=1, chapter_text="第1章 正文\n具体行动。", chapter_outline=self.outline(),
            book_contract="", story_context="", canon_context="", recent_context="",
        )
        self.assertEqual(9, len(rows))
        self.assertIn('"id": "R9"', user)
        helper = audit_helpers.NarrativeContextBudgetTests()
        helper.setUp()
        app = helper.make_app()
        app.STATE_USER_CHAR_CAP = 100
        with helper.guard_context(app):
            with self.assertRaises(PromptBudgetError):
                app._run_narrative_quality_guard(1, helper.fixture.chapter, self.outline())
        app.call_llm_review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
