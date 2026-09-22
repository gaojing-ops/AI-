import inspect
import unittest

import repair_engine


class FailureClassificationTests(unittest.TestCase):
    def test_continuity_failure_wins_over_truth_wording(self):
        reason = "正史连续性语义审计未通过：本章提前坐实最终真相"
        self.assertEqual("continuity_hard", repair_engine.classify_failure(reason))

    def test_plain_truth_failure_remains_truth_reveal(self):
        self.assertEqual("truth_reveal", repair_engine.classify_failure("真相节奏越界"))

    def test_word_count_expression_is_unambiguous(self):
        self.assertEqual("word_count", repair_engine.classify_failure("低于3200字硬下限"))

    def test_markdown_residue_is_format(self):
        self.assertEqual("format", repair_engine.classify_failure("检测到markdown残留"))

    def test_quality_gate_warn_is_not_mislabeled_pass(self):
        self.assertEqual("WARN", repair_engine.parse_gate_status("WARN\n钩子偏弱"))
        self.assertEqual("PASS", repair_engine.parse_gate_status("PASS\n通过"))
        self.assertEqual("WARN", repair_engine.parse_gate_status("没有结构化结论"))

    def test_all_semantic_and_prose_failures_get_a_rewrite_attempt(self):
        chapter = "第1章 尸检台上的三秒\n\n" + "正文" * 1600
        reasons = (
            "质检未通过：细纲状态落点相反",
            "主线与可读性硬审未通过",
            "跨章检查未通过",
            "设定总校发现人设不一致",
            "正文出现重复段落",
            "正文出现创作过程元话语",
            "字数过长，超过目标上限",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertTrue(repair_engine.should_rewrite(reason, chapter))

    def test_semantic_and_prose_failures_can_never_become_official(self):
        chapter = "第1章 测试\n\n" + "正文" * 1800
        reasons = (
            "主线与可读性硬审未通过",
            "正文出现重复段落",
            "正文出现创作过程元话语",
            "字数过长，超过目标上限",
            "跨章检查未通过",
            "设定总校发现人设不一致",
            "细纲核心事件未完成",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                ok, message = repair_engine.can_continue_with_review(
                    reason,
                    chapter,
                    char_limits={"min": 3200, "max": 4200},
                )
                self.assertFalse(ok)
                self.assertIn("不能降级写入正式上下文", message)
                decision = repair_engine.decide_after_retries(
                    reason,
                    chapter,
                    char_limits={"min": 3200, "max": 4200},
                )
                self.assertEqual("pause", decision["action"])
                self.assertEqual("待审暂停", decision["status"])

    def test_project_hard_minimum_cannot_be_downgraded(self):
        chapter = "第1章 测试\n\n" + "正文" * 1515
        limits = {"min": 3200, "rewrite_floor": 2800}
        self.assertTrue(
            repair_engine.should_rewrite("字数不足（3030字），低于3200字硬下限", chapter, limits)
        )
        ok, message = repair_engine.can_continue_with_review(
            "字数不足", chapter, char_limits=limits, min_salvage_chars=2400
        )
        self.assertFalse(ok)
        self.assertTrue(
            "项目硬下限3200字" in message or "不能降级写入正式上下文" in message
        )

    def test_repair_note_preserves_later_checklist_items(self):
        reason = "A" * 700 + "末项：类比词超过上限"
        note = repair_engine.format_repair_note(reason, 2)
        self.assertIn("末项：类比词超过上限", note)

    def test_generic_repair_module_contains_no_book_specific_facts(self):
        source = inspect.getsource(repair_engine)
        for forbidden in ("沈砚", "周正", "蒋白", "邵岑", "马会", "早十一分钟", "三十一分钟"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_outline_repair_uses_generic_contract_rules(self):
        reason = "细纲要求 R1 未完成：关键角色应根据证据确认明确时间差。"
        rules = repair_engine.build_targeted_rules(reason)
        joined = "\n".join(rules)
        self.assertIn("逐条覆盖本章细纲", joined)
        self.assertNotIn("沈砚", joined)
        self.assertNotIn("十一分钟", joined)

    def test_repetition_meta_and_overlength_prompts_do_not_echo_failed_draft(self):
        failed = "独一无二的失败稿密文，不得再次喂给模型。"
        cases = (
            "正文存在完全重复的长段落",
            "正文出现创作过程元话语",
            "字数过长，超过目标上限",
        )
        for reason in cases:
            with self.subTest(reason=reason):
                prompt = repair_engine.build_repair_prompt(
                    "基础写作合同",
                    [reason],
                    failed_content=failed,
                    chapter_outline="核心事件：重新完成本章",
                    prev_content="上一章正文",
                    char_limits={"target_min": 3200, "target_max": 4000},
                    chap_num=2,
                )
                self.assertNotIn(failed, prompt)
                self.assertNotIn("上一版失败草稿", prompt)
                self.assertIn("本章细纲核对", prompt)


if __name__ == "__main__":
    unittest.main()
