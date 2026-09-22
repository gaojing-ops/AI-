import tempfile
import unittest
from unittest.mock import patch

import continuity_guard


class DeclaredRealmExtractionTests(unittest.TestCase):
    def setUp(self):
        self.canon = {
            "protagonist": "沈川",
            "realm_order": ["见习调查员", "正式调查员", "高级调查员"],
        }

    def test_enemy_realm_near_protagonist_is_not_a_declaration(self):
        text = "沈川面对高级调查员级别的守卫，没有正面交手。"
        self.assertEqual([], continuity_guard._extract_declared_realms(text, self.canon))

    def test_unscoped_evaluation_is_not_a_protagonist_declaration(self):
        text = "实力评价：正式调查员 -> 高级调查员"
        self.assertEqual([], continuity_guard._extract_declared_realms(text, self.canon))

    def test_explicit_protagonist_realm_is_detected(self):
        text = "沈川已经晋升正式调查员，但新权限尚未稳固。"
        self.assertEqual(["正式调查员"], continuity_guard._extract_declared_realms(text, self.canon))

    def test_protagonist_comparing_two_realms_is_not_promoted(self):
        text = (
            "沈川清楚见习调查员与正式调查员的差距。"
            "前者只能记录，后者已经能独立签发意见。"
        )
        self.assertEqual(
            [], continuity_guard._extract_declared_realms(text, self.canon)
        )

    def test_explicit_report_row_realm_is_detected(self):
        text = "风险表写着沈川的当前状态。境界仍是见习调查员，权限没有升级。"
        self.assertEqual(
            ["见习调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_negative_guard_is_ignored(self):
        text = "沈川不得写回见习调查员。"
        self.assertEqual([], continuity_guard._extract_declared_realms(text, self.canon))

    def test_pronoun_state_after_named_protagonist_is_detected(self):
        text = (
            "沈川站在警戒线外，没有接触设备。\n\n"
            "他目前仍是正式调查员，只负责核对现场记录。"
        )
        self.assertEqual(
            ["正式调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_unscoped_pronoun_state_is_not_detected(self):
        text = "他目前仍是见习调查员，只负责门外值守。"
        self.assertEqual(
            [],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_realm_panel_transition_is_detected(self):
        text = "境界栏从见习调查员改成了正式调查员。"
        self.assertEqual(
            ["正式调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_entered_realm_after_form_is_detected(self):
        text = "沈川没有接管按钮。进入高级调查员后，他仍只能签自己的意见。"
        self.assertEqual(
            ["高级调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_realm_still_stopped_at_form_is_detected(self):
        text = "沈川核对记录。他的境界仍停在正式调查员。"
        self.assertEqual(
            ["正式调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )

    def test_realm_capability_form_is_detected(self):
        text = "沈川留在门外。正式调查员境界能让他看懂权限，却不能替别人签字。"
        self.assertEqual(
            ["正式调查员"],
            continuity_guard._extract_declared_realms(text, self.canon),
        )


class OutlineValidationTests(unittest.TestCase):
    def test_heading_with_title_label_and_colon_is_recognized(self):
        sections = continuity_guard.story_architect.split_outline_sections(
            "第1章 标题：死亡视频\n核心事件：发现未来录像。"
        )
        self.assertIn(1, sections)

    def test_single_extracted_section_does_not_require_repeated_heading(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(
                plot_dir, continuity_guard.story_architect.build_initial_canon("主角")
            )
            result = continuity_guard.validate_outline(
                "核心事件：主角进入回收站。\n章末悬念：收到文明残片。",
                1,
                1,
                plot_dir,
            )
        self.assertFalse(
            any("章节覆盖错误" in issue for issue in result["issues"]),
            result,
        )

    def test_multi_chapter_outline_still_requires_explicit_headings(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(
                plot_dir, continuity_guard.story_architect.build_initial_canon("主角")
            )
            result = continuity_guard.validate_outline("只有一段正文", 1, 2, plot_dir)
        self.assertTrue(any("章节覆盖错误" in issue for issue in result["issues"]))


class RealmRegressionGuardTests(unittest.TestCase):
    def setUp(self):
        self.canon = {
            "protagonist": "沈川",
            "current_chapter": 10,
            "current_realm": "正式调查员",
            "realm_order": ["见习调查员", "正式调查员", "高级调查员"],
        }

    def check_text(self, text, *, outline=False, chapter=11):
        # Isolate rank validation from unrelated outline contract fields.
        with (
            patch.object(continuity_guard, "load_canon", return_value=self.canon),
            patch.object(continuity_guard.story_architect, "load_story_bible", return_value={}),
            patch.object(
                continuity_guard.story_architect,
                "validate_outline_contract",
                return_value={"issues": []},
            ),
        ):
            if outline:
                return continuity_guard.validate_outline(
                    f"第{chapter}章\n{text}", chapter, chapter, "."
                )
            return continuity_guard.validate_chapter(text, chapter, ".")

    def test_chapter_rejects_explicit_project_rank_regression(self):
        for text in (
            "沈川目前是见习调查员。",
            "沈川站在门外。\n\n他目前仍是见习调查员。",
        ):
            with self.subTest(text=text):
                result = self.check_text(text)
                self.assertEqual("FAIL", result["status"], result)
                self.assertTrue(any("主角境界回档" in issue for issue in result["issues"]))

    def test_outline_rejects_explicit_project_rank_regression(self):
        result = self.check_text("沈川目前是见习调查员。", outline=True)
        self.assertEqual("FAIL", result["status"], result)
        self.assertTrue(any("细纲写成见习调查员" in issue for issue in result["issues"]))

    def test_chapter_does_not_reject_non_regression_references(self):
        for text in (
            "沈川面对见习调查员级别的守卫，没有正面交手。",
            "沈川回忆起自己担任见习调查员的日子。",
            "沈川不得写回见习调查员。",
            "沈川清楚见习调查员与正式调查员的差距。",
            "沈川目前是正式调查员。",
            "沈川已经晋升高级调查员。",
        ):
            with self.subTest(text=text):
                self.assertEqual("PASS", self.check_text(text)["status"])

    def test_outline_does_not_reject_non_regression_references(self):
        for text in (
            "沈川面对见习调查员级别的守卫，没有正面交手。",
            "沈川回忆起自己担任见习调查员的日子。",
            "沈川不得写回见习调查员。",
            "沈川清楚见习调查员与正式调查员的差距。",
            "沈川目前是正式调查员。",
            "沈川已经晋升高级调查员。",
        ):
            with self.subTest(text=text):
                self.assertEqual("PASS", self.check_text(text, outline=True)["status"])

    def test_historical_chapter_keeps_existing_rewrite_boundary(self):
        self.assertEqual(
            "PASS", self.check_text("沈川目前是见习调查员。", chapter=9)["status"]
        )

    def test_missing_project_rank_order_does_not_invent_one(self):
        self.canon["realm_order"] = []
        for outline in (False, True):
            with self.subTest(outline=outline):
                self.assertEqual(
                    "PASS", self.check_text("沈川目前是见习调查员。", outline=outline)["status"]
                )

    def test_football_stage_order_is_used_by_both_entry_points(self):
        self.canon.update({
            "protagonist": "周予安",
            "current_realm": "俱乐部梯队正式注册球员",
            "realm_order": [
                "校园U17球员（离开青训三年）",
                "联合训练与冬训候选",
                "俱乐部梯队正式注册球员",
            ],
        })
        for outline in (False, True):
            with self.subTest(outline=outline):
                result = self.check_text(
                    "周予安目前是校园U17球员（离开青训三年）。", outline=outline
                )
                self.assertEqual("FAIL", result["status"], result)


class ContinuityRequirementTests(unittest.TestCase):
    def test_compound_requirement_accepts_reordered_readable_prose(self):
        self.assertTrue(
            continuity_guard._continuity_requirement_present(
                "唐砺翻开记录本，开始正式询问许衡为什么覆盖日志。",
                "许衡正式询问",
            )
        )

    def test_death_window_is_equivalent_to_second_death_time_label(self):
        self.assertTrue(
            continuity_guard._continuity_requirement_present(
                "组织反应把死亡窗口向前锁定了十一分钟。",
                "第二死亡时间。",
            )
        )

    def test_mixed_identifier_requires_identifier_and_chinese_anchor(self):
        self.assertTrue(
            continuity_guard._continuity_requirement_present(
                "B-17编号来自十二年前的封存档案。",
                "B-17封存单",
            )
        )
        self.assertFalse(
            continuity_guard._continuity_requirement_present(
                "另一份普通封存档案已经归档。",
                "B-17封存单",
            )
        )

    def test_unrelated_chapter_still_fails_all_requirements(self):
        required = ["B-17封存单", "许衡正式询问", "第二死亡时间。"]
        self.assertFalse(
            continuity_guard._any_continuity_requirement_present(
                "沈砚回到办公室处理日常报告。",
                required,
            )
        )


class CanonSnapshotTests(unittest.TestCase):
    def test_evidence_delta_overrides_outline_placeholders_for_protagonist(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {
                "current_chapter": 1,
                "protagonist": "沈砚",
                "current_location": "以第1章细纲为准",
                "current_injury": "无",
                "realm_order": [],
                "completed_events": [],
                "recent_chapter_contracts": [],
            })
            continuity_guard.update_after_chapter(
                plot_dir,
                2,
                "第2章\n\n沈砚复勘恢复舱。",
                chapter_outline="状态落点：地点=未知；伤势=无",
                prepared_state_delta={
                    "chapter": 2,
                    "delta": {
                        "chapter": 2,
                        "characters": [{
                            "name": "沈砚",
                            "location": "联赛馆恢复室",
                            "condition": "右臂逆伤未愈，右手握力只剩六成",
                        }],
                    },
                },
            )
            canon = continuity_guard.load_canon(plot_dir)
            self.assertEqual("联赛馆恢复室", canon["current_location"])
            self.assertEqual(
                "右臂逆伤未愈，右手握力只剩六成",
                canon["current_injury"],
            )

    def test_protagonist_state_uses_latest_nonempty_field_across_multiple_items(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {
                "current_chapter": 1,
                "protagonist": "沈砚",
                "current_location": "旧站",
                "current_injury": "右腿旧伤",
                "realm_order": [],
                "completed_events": [],
                "recent_chapter_contracts": [],
            })
            continuity_guard.update_after_chapter(
                plot_dir,
                2,
                "第2章\n\n沈砚在恢复室确认右肩仍有热意，随后读完通知。",
                prepared_state_delta={
                    "chapter": 2,
                    "delta": {
                        "chapter": 2,
                        "characters": [
                            {
                                "name": "沈砚",
                                "location": "恢复室",
                                "condition": "右肩仍有热意",
                            },
                            {
                                "name": "沈砚",
                                "knowledge_add": ["测试日期已确定"],
                                "location": "",
                                "condition": "",
                            },
                        ],
                    },
                },
            )
            canon = continuity_guard.load_canon(plot_dir)
            self.assertEqual("恢复室", canon["current_location"])
            self.assertEqual("右肩仍有热意", canon["current_injury"])

    def test_generic_outline_clear_does_not_erase_proven_injury(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {
                "current_chapter": 2,
                "protagonist": "沈砚",
                "current_injury": "右臂逆伤未愈",
                "realm_order": [],
                "completed_events": [],
                "recent_chapter_contracts": [],
            })
            continuity_guard.update_after_chapter(
                plot_dir,
                3,
                "第3章\n\n沈砚继续调查。",
                chapter_outline="状态落点：伤势=无",
                prepared_state_delta={"chapter": 3, "characters": []},
            )
            canon = continuity_guard.load_canon(plot_dir)
            self.assertEqual("右臂逆伤未愈", canon["current_injury"])

    def test_missing_protagonist_location_does_not_retain_stale_place(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {
                "current_chapter": 1,
                "protagonist": "沈砚",
                "current_location": "家",
                "current_injury": "右肩发热",
                "realm_order": [],
                "completed_events": [],
                "recent_chapter_contracts": [],
            })
            continuity_guard.update_after_chapter(
                plot_dir,
                2,
                "第2章\n\n沈砚留在训练场继续对抗。",
                prepared_state_delta={
                    "chapter": 2,
                    "delta": {
                        "chapter": 2,
                        "characters": [{
                            "name": "沈砚",
                            "location": "",
                            "condition": "右肩发热",
                        }],
                    },
                },
            )
            canon = continuity_guard.load_canon(plot_dir)
            self.assertEqual(
                "未结构化，以最新正式章节正文为准",
                canon["current_location"],
            )
            self.assertEqual("右肩发热", canon["current_injury"])

    def test_replacing_last_chapter_removes_old_irreversible_state(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {
                "current_chapter": 0,
                "protagonist": "林舟",
                "realm_order": ["一阶", "二阶"],
                "current_realm": "一阶",
                "completed_events": [],
                "recent_chapter_contracts": [],
            })
            continuity_guard.save_canon_snapshot(plot_dir)
            continuity_guard.update_after_chapter(
                plot_dir,
                1,
                "第1章\n\n林舟已经晋升二阶。旧站在火海中消失。",
                chapter_outline=(
                    "状态落点：境界=二阶\n"
                    "不可逆变化：旧站永久爆炸"
                ),
                prepared_state_delta={
                    "delta": {"events": [{"summary": "旧站永久爆炸"}]}
                },
            )
            old = continuity_guard.load_canon(plot_dir)
            self.assertEqual("二阶", old["current_realm"])
            self.assertTrue(any("旧站永久爆炸" in row for row in old["completed_events"]))

            continuity_guard.restore_before_chapter(plot_dir, 1)
            continuity_guard.update_after_chapter(
                plot_dir,
                1,
                "第1章\n\n林舟仍是一阶，他及时关闭了旧站电闸。",
                chapter_outline="第1章 重写\n不可逆变化：旧站电闸被关闭",
                prepared_state_delta={
                    "delta": {"events": [{"summary": "旧站电闸被关闭"}]}
                },
            )
            replaced = continuity_guard.load_canon(plot_dir)
            self.assertEqual("一阶", replaced["current_realm"])
            self.assertFalse(
                any("旧站永久爆炸" in row for row in replaced["completed_events"])
            )
            self.assertTrue(
                any("旧站电闸被关闭" in row for row in replaced["completed_events"])
            )

    def test_missing_prior_snapshot_blocks_rollback(self):
        with tempfile.TemporaryDirectory() as plot_dir:
            continuity_guard.save_canon(plot_dir, {"current_chapter": 1})
            with self.assertRaisesRegex(RuntimeError, "不能安全覆盖"):
                continuity_guard.restore_before_chapter(plot_dir, 1)


if __name__ == "__main__":
    unittest.main()
