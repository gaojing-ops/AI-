# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commercial_reviewer


class CommercialReviewerTests(unittest.TestCase):
    def test_missing_revision_debt_is_read_only_initial_state(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "revision_debt.json"
            data = commercial_reviewer.load_revision_debt(path)
            self.assertEqual(data["items"], [])
            self.assertEqual(data["review_history"], [])
            self.assertFalse(path.exists())

    def test_invalid_revision_debt_is_rejected_without_overwrite(self):
        valid_item = {
            "id": "debt_test", "action": "兑现约定", "status": "OPEN",
            "opened_chapter": 3, "due_by": 13,
        }
        valid = {"schema_version": 1, "items": [valid_item], "review_history": []}
        invalid = [
            b"{truncated", b"\xff", b"[]", b"null",
            json.dumps({**valid, "schema_version": 2}).encode(),
            json.dumps({"schema_version": 1}).encode(),
            json.dumps({**valid, "items": {}}).encode(),
            json.dumps({**valid, "review_history": [None]}).encode(),
        ]
        for field, value in (("status", "UNKNOWN"), ("due_by", "tomorrow"),
                             ("opened_chapter", -1), ("id", ""), ("action", None)):
            invalid.append(json.dumps({
                **valid, "items": [{**valid_item, field: value}],
            }).encode())
        invalid.append(json.dumps({**valid, "items": [valid_item, valid_item]}).encode())
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "revision_debt.json"
            for raw in invalid:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaisesRegex(RuntimeError, "修订债务"):
                        commercial_reviewer.load_revision_debt(path)
                    with self.assertRaisesRegex(RuntimeError, "修订债务"):
                        commercial_reviewer.update_revision_debt(
                            str(path), 10, {"review_id": "r10", "next_actions": ["新任务"]}
                        )
                    self.assertEqual(path.read_bytes(), raw)

    def test_revision_debt_permission_error_is_not_an_empty_ledger(self):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "修订债务"):
                commercial_reviewer.load_revision_debt("revision_debt.json")

    def test_revision_debt_retention_never_discards_open_items(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "revision_debt.json"
            items = [{
                "id": f"debt_{i}", "action": f"任务{i}",
                "status": "OPEN" if i == 0 else "VERIFIED",
                "opened_chapter": 1, "due_by": 10,
            } for i in range(201)]
            path.write_text(json.dumps({
                "schema_version": 1, "items": items, "review_history": [],
            }), encoding="utf-8")
            data = commercial_reviewer.update_revision_debt(
                str(path), 30, {"review_id": "r30", "next_actions": []}
            )
            self.assertIn("debt_0", [item["id"] for item in data["items"]])
            self.assertEqual(data["items"][0]["status"], "OPEN")

    def test_runs_only_at_milestones_and_volume_ends(self):
        config = {
            "commercial_review_enabled": True,
            "commercial_review_milestones": [3, 10, 30],
            "commercial_review_volume_end": True,
        }
        volume_ranges = [(1, 140, "第一卷"), (141, 280, "第二卷")]
        for chapter in (3, 10, 30, 140, 280):
            self.assertTrue(commercial_reviewer.should_run(chapter, config, volume_ranges))
        for chapter in (1, 9, 31, 141):
            self.assertFalse(commercial_reviewer.should_run(chapter, config, volume_ranges))
        config["commercial_review_enabled"] = False
        self.assertFalse(commercial_reviewer.should_run(30, config, volume_ranges))

    def test_optional_interval_reviews_every_ten_chapters(self):
        config = {
            "commercial_review_enabled": True,
            "commercial_review_milestones": [3],
            "commercial_review_interval": 10,
            "commercial_review_volume_end": False,
        }
        for chapter in (3, 10, 20, 40, 300):
            self.assertTrue(commercial_reviewer.should_run(chapter, config, []))
        for chapter in (1, 9, 11, 31, 299):
            self.assertFalse(commercial_reviewer.should_run(chapter, config, []))

    def test_golden_three_requires_explicit_pass_and_continue(self):
        config = {
            "golden_three_gate_chapter": 3,
            "golden_three_require_explicit_continue": True,
            "commercial_review_pause_on_fail": True,
        }
        self.assertFalse(
            commercial_reviewer.should_pause_after_review(
                3, "PASS", "CONTINUE", config
            )
        )
        for status, action in (
            ("WARN", "ADJUST"),
            ("PASS", "ADJUST"),
            ("FAIL", "PAUSE"),
        ):
            self.assertTrue(
                commercial_reviewer.should_pause_after_review(
                    3, status, action, config
                )
            )
        self.assertFalse(
            commercial_reviewer.should_pause_after_review(
                10, "WARN", "ADJUST", config
            )
        )

    def test_chapter_30_and_volume_ends_are_hard_gates(self):
        config = {
            "commercial_hard_gate_chapters": [3, 30],
            "commercial_hard_gate_require_explicit_continue": True,
            "commercial_review_volume_end": True,
            "commercial_review_pause_on_fail": True,
        }
        ranges = [(1, 60, "第一卷"), (61, 120, "第二卷")]
        self.assertEqual(
            commercial_reviewer.hard_gate_chapters(config, ranges),
            [3, 30, 60, 120],
        )
        for chapter in (30, 60):
            self.assertTrue(
                commercial_reviewer.should_pause_after_review(
                    chapter,
                    "WARN",
                    "ADJUST",
                    config,
                    volume_ranges=ranges,
                )
            )
            self.assertFalse(
                commercial_reviewer.should_pause_after_review(
                    chapter,
                    "PASS",
                    "CONTINUE",
                    config,
                    volume_ranges=ranges,
                )
            )
        self.assertFalse(
            commercial_reviewer.should_pause_after_review(
                10,
                "WARN",
                "ADJUST",
                config,
                volume_ranges=ranges,
            )
        )

    def test_old_revision_debt_blocks_a_hard_gate_until_verified(self):
        config = {
            "commercial_hard_gate_chapters": [3, 30],
            "commercial_review_volume_end": True,
        }
        ranges = [(1, 60, "第一卷")]
        debt = {
            "items": [
                {
                    "status": "OPEN",
                    "opened_chapter": 10,
                    "action": "第20章前兑现主卖点",
                }
            ]
        }
        partial = {"debt_status": "PARTIAL"}
        resolved = {"debt_status": "RESOLVED"}
        self.assertTrue(
            commercial_reviewer.hard_gate_debt_blocked(
                30, partial, debt, config, ranges
            )
        )
        self.assertFalse(
            commercial_reviewer.hard_gate_debt_blocked(
                30, resolved, debt, config, ranges
            )
        )
        self.assertFalse(
            commercial_reviewer.hard_gate_debt_blocked(
                10, partial, debt, config, ranges
            )
        )

    def test_future_revision_debt_does_not_block_current_hard_gate(self):
        config = {
            "commercial_hard_gate_chapters": [60],
            "commercial_review_volume_end": True,
        }
        ranges = [(31, 60, "第二卷")]
        debt = {
            "items": [
                {
                    "status": "OPEN",
                    "opened_chapter": 60,
                    "due_by": 70,
                    "action": "第61至70章兑现系统限制",
                }
            ]
        }
        self.assertFalse(
            commercial_reviewer.hard_gate_debt_blocked(
                60, {"debt_status": "PARTIAL"}, debt, config, ranges
            )
        )

    def test_overdue_revision_debt_blocks_later_hard_gate(self):
        config = {
            "commercial_hard_gate_chapters": [60],
            "commercial_review_volume_end": True,
        }
        ranges = [(31, 60, "第二卷")]
        debt = {
            "items": [
                {
                    "status": "OPEN",
                    "opened_chapter": 50,
                    "due_by": 55,
                    "action": "第55章前兑现",
                }
            ]
        }
        self.assertTrue(
            commercial_reviewer.hard_gate_debt_blocked(
                60, {"debt_status": "PARTIAL"}, debt, config, ranges
            )
        )

    def test_hard_gate_prompt_does_not_treat_future_debt_as_overdue(self):
        _system, user = commercial_reviewer.build_prompts(
            60,
            [(60, "===== 第60章全文 =====\n正文")],
            commercial_context="未来债务",
            hard_gate=True,
        )
        self.assertIn("未来债务不阻断本次 PASS/CONTINUE", user)
        self.assertIn("当前闸门没有到期欠账", user)

    def test_unavailable_stage_review_pauses_by_default(self):
        self.assertTrue(
            commercial_reviewer.should_pause_after_review(
                3,
                "UNAVAILABLE",
                "UNAVAILABLE",
                {},
                review_unavailable=True,
            )
        )
        self.assertFalse(
            commercial_reviewer.should_pause_after_review(
                3,
                "UNAVAILABLE",
                "UNAVAILABLE",
                {"commercial_review_pause_on_unavailable": False},
                review_unavailable=True,
            )
        )

    def test_collects_only_official_chapters_across_volume_folders(self):
        with tempfile.TemporaryDirectory() as root:
            volume_1 = os.path.join(root, "第一卷")
            volume_2 = os.path.join(root, "第二卷")
            os.makedirs(volume_1)
            os.makedirs(volume_2)
            with open(os.path.join(volume_1, "第2章.txt"), "w", encoding="utf-8") as f:
                f.write("第二章正式稿")
            with open(os.path.join(volume_2, "第3章.txt"), "w", encoding="utf-8") as f:
                f.write("磁盘旧稿")
            with open(os.path.join(volume_2, "第3章_待审.txt"), "w", encoding="utf-8") as f:
                f.write("不应读入的草稿")
            with open(os.path.join(volume_2, "随手笔记.txt"), "w", encoding="utf-8") as f:
                f.write("不应读入的笔记")

            rows = commercial_reviewer.collect_recent_chapters(
                root, 3, current_content="第三章内存正式稿", lookback=3, max_chars=24000
            )

            self.assertEqual([number for number, _ in rows], [2, 3])
            combined = "\n".join(block for _, block in rows)
            self.assertIn("第二章正式稿", combined)
            self.assertIn("第三章内存正式稿", combined)
            self.assertNotIn("磁盘旧稿", combined)
            self.assertNotIn("不应读入", combined)

    def test_parses_structured_result_and_falls_back_safely(self):
        parsed = commercial_reviewer.parse_review(
            "OVERALL: FAIL\nCURRENT: PASS\nACTION: PAUSE\nSUMMARY: 卖点兑现太慢\n"
            "DEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n- 已核验失败原因"
        )
        self.assertEqual(parsed["status"], "FAIL")
        self.assertEqual(parsed["current"], "PASS")
        self.assertEqual(parsed["action"], "PAUSE")
        self.assertEqual(parsed["summary"], "卖点兑现太慢")

        malformed = commercial_reviewer.parse_review("暂时无法给出结构化结果")
        self.assertEqual(malformed["status"], "FAIL")
        self.assertEqual(malformed["current"], "FAIL")
        self.assertEqual(malformed["action"], "PAUSE")
        self.assertTrue(malformed["malformed"])

    def test_parses_strict_json_result_without_weakening_terminal_checks(self):
        parsed = commercial_reviewer.parse_review(json.dumps({
            "OVERALL": "PASS",
            "CURRENT": "PASS",
            "ACTION": "CONTINUE",
            "SUMMARY": "黄金三章可以继续",
            "STRENGTHS": ["开篇钩子明确"],
            "RISKS": [],
            "NEXT_10_CHAPTERS": ["兑现职业代价"],
            "DEBT_STATUS": "RESOLVED",
            "DEBT_EVIDENCE": ["debt_audio_boundary：第3章已解决"],
        }, ensure_ascii=False))
        self.assertFalse(parsed["malformed"])
        self.assertEqual("PASS", parsed["status"])
        self.assertEqual("CONTINUE", parsed["action"])
        self.assertEqual(["兑现职业代价"], parsed["next_actions"])

        duplicate = commercial_reviewer.parse_review(
            '{"OVERALL":"PASS","OVERALL":"FAIL","CURRENT":"PASS",'
            '"ACTION":"CONTINUE","SUMMARY":"x","DEBT_STATUS":"RESOLVED",'
            '"DEBT_EVIDENCE":["verified"]}'
        )
        self.assertTrue(duplicate["malformed"])
        self.assertEqual("FAIL", duplicate["status"])

    def test_terminal_labels_must_be_unique_and_entire_lines(self):
        valid_tail = (
            "CURRENT: PASS\nACTION: CONTINUE\nSUMMARY: ready\n"
            "DEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n- verified"
        )
        for prefix in (
            "OVERALL: PASS\nOVERALL: UNKNOWN\n",
            "OVERALL: PASS junk\n",
            "OVERALL: PASS\nCURRENT: PASS\nCURRENT: UNKNOWN\n"
            "ACTION: CONTINUE\nSUMMARY: ready\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- verified\n",
        ):
            with self.subTest(prefix=prefix):
                raw = prefix + valid_tail if not prefix.startswith(
                    "OVERALL: PASS\nCURRENT:"
                ) else prefix
                parsed = commercial_reviewer.parse_review(raw)
                self.assertTrue(parsed["malformed"])
                self.assertEqual("FAIL", parsed["status"])
                self.assertEqual("PAUSE", parsed["action"])

    def test_hard_gate_must_cover_every_chapter_from_one_through_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output", "第一卷")
            os.makedirs(out_dir)
            for chapter in (1, 3):
                with open(os.path.join(out_dir, f"第{chapter}章.txt"), "w", encoding="utf-8") as f:
                    f.write(f"第{chapter}章正文")
            with self.assertRaisesRegex(RuntimeError, "缺少第2章"):
                commercial_reviewer.run_review(
                    output_dir=os.path.join(root, "output"),
                    report_dir=os.path.join(root, "review_reports"),
                    chapter_num=3,
                    current_content="第3章正文",
                    config={
                        "commercial_hard_gate_chapters": [3],
                        "commercial_review_lookback": 30,
                    },
                    volume_ranges=[],
                    story_context="故事契约",
                    canon_context="当前正史",
                    llm_call=lambda _system, _user: "不应调用",
                    model_name="deepseek-v4-pro",
                )

    def test_run_review_writes_markdown_and_latest_pointer(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output", "第一卷")
            report_dir = os.path.join(root, "review_reports")
            os.makedirs(out_dir)
            with open(os.path.join(out_dir, "第1章.txt"), "w", encoding="utf-8") as f:
                f.write("第一章正文")

            result = commercial_reviewer.run_review(
                output_dir=os.path.join(root, "output"),
                report_dir=report_dir,
                chapter_num=1,
                current_content="第一章正文",
                config={"commercial_review_lookback": 30},
                volume_ranges=[(1, 50, "第一卷")],
                story_context="故事契约",
                canon_context="当前正史",
                llm_call=lambda _system, _user: (
                    "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
                    "SUMMARY: 可以继续\nSTRENGTHS:\n- 开篇明确\nRISKS:\n- 无\n"
                    "NEXT_10_CHAPTERS:\n- 推进主线\nDEBT_STATUS: RESOLVED\n"
                    "DEBT_EVIDENCE:\n- 已核验当前正文"
                ),
                model_name="deepseek-v4-pro",
            )

            self.assertTrue(os.path.exists(result["report_path"]))
            latest_path = os.path.join(report_dir, "latest_commercial_review.json")
            self.assertTrue(os.path.exists(latest_path))
            with open(latest_path, "r", encoding="utf-8") as f:
                latest = json.load(f)
            self.assertEqual(latest["status"], "PASS")
            self.assertEqual(latest["model"], "deepseek-v4-pro")
            self.assertEqual(latest["reviewed_chapters"], [1])

    def test_draft_review_is_not_persisted_until_chapter_commit(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            report_dir = os.path.join(root, "review_reports")
            os.makedirs(out_dir)
            result = commercial_reviewer.run_review(
                output_dir=out_dir,
                report_dir=report_dir,
                chapter_num=3,
                current_content="第三章尚未保存的候选正文",
                config={
                    "commercial_review_lookback": 30,
                    "commercial_hard_gate_chapters": [30],
                    "golden_three_require_explicit_continue": False,
                },
                volume_ranges=[(1, 50, "第一卷")],
                story_context="故事契约",
                canon_context="当前正史",
                llm_call=lambda _system, _user: (
                    "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
                    "SUMMARY: 可以继续\nNEXT_10_CHAPTERS:\n- 推进主线\n"
                    "DEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n- 已核验当前正文"
                ),
                model_name="deepseek-v4-pro",
                persist=False,
            )
            self.assertFalse(os.path.exists(report_dir))
            self.assertEqual(result["report_path"], "")

            result["gate_blocked"] = True
            result["gate_block_reason"] = "上一阶段修订债务尚未核销"
            commercial_reviewer.persist_review(
                report_dir, 3, result, model_name="deepseek-v4-pro"
            )
            with open(
                os.path.join(report_dir, "latest_commercial_review.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                latest = json.load(handle)
            self.assertTrue(latest["gate_blocked"])
            self.assertEqual(latest["gate_block_reason"], "上一阶段修订债务尚未核销")
            first_path = result["report_path"]
            commercial_reviewer.persist_review(
                report_dir, 3, result, model_name="deepseek-v4-pro"
            )
            self.assertEqual(result["report_path"], first_path)
            reports = [name for name in os.listdir(report_dir) if name.endswith(".md")]
            self.assertEqual(len(reports), 1)

    def test_manual_or_missing_review_receipt_cannot_be_persisted(self):
        base = {
            "status": "PASS",
            "current": "PASS",
            "action": "CONTINUE",
            "summary": "可以继续",
            "raw": (
                "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\nSUMMARY: 可以继续\n"
                "DEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n- 已核验当前正文"
            ),
            "debt_status": "RESOLVED",
            "debt_evidence": ["已核验当前正文"],
            "reviewed_chapters": [1, 2, 3],
            "expected_chapters": [1, 2, 3],
            "malformed": False,
        }
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "调用收据"):
                commercial_reviewer.persist_review(
                    root, 3, dict(base), model_name="deepseek-v4-pro"
                )

            manual = dict(base)
            manual["review_receipt"] = {
                "version": commercial_reviewer.REVIEW_RECEIPT_VERSION,
                "origin": "manual-review",
            }
            with self.assertRaisesRegex(RuntimeError, "人工回填"):
                commercial_reviewer.persist_review(
                    root, 3, manual, model_name="manual-review"
                )

    def test_actions_are_parsed_and_written_for_future_generation(self):
        parsed = commercial_reviewer.parse_review(
            "OVERALL: WARN\nCURRENT: PASS\nACTION: ADJUST\nSUMMARY: 兑现偏慢\n"
            "NEXT_10_CHAPTERS:\n- 第四章完成第一次资源兑换\n- 第六章改变盟友关系\n"
            "DEBT_STATUS: PARTIAL\nDEBT_EVIDENCE:\n- 首次建立"
        )
        self.assertEqual(len(parsed["next_actions"]), 2)
        self.assertEqual(parsed["debt_status"], "PARTIAL")
        self.assertEqual(parsed["debt_evidence"], ["首次建立"])
        with tempfile.TemporaryDirectory() as root:
            path = commercial_reviewer.save_action_plan(
                os.path.join(root, "商业审稿行动单.md"), 3, parsed
            )
            with open(path, "r", encoding="utf-8") as f:
                saved = f.read()
        self.assertIn("第四章完成第一次资源兑换", saved)
        self.assertIn("不得推翻正史", saved)
        self.assertIn("级别：REVISION", saved)

    def test_legacy_review_verdicts_map_to_stable_severity(self):
        self.assertEqual(
            "ADVISORY",
            commercial_reviewer.classify_review_severity(
                {"status": "PASS", "action": "CONTINUE"}
            ),
        )
        self.assertEqual(
            "REVISION",
            commercial_reviewer.classify_review_severity(
                {"status": "WARN", "action": "ADJUST"}
            ),
        )
        self.assertEqual(
            "BLOCKER",
            commercial_reviewer.classify_review_severity(
                {"status": "FAIL", "action": "PAUSE"}, hard_gate=True
            ),
        )

    def test_revision_debt_stays_open_until_a_later_review_verifies_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            first = {
                "status": "WARN",
                "action": "ADJUST",
                "debt_status": "PARTIAL",
                "debt_evidence": ["首次建立"],
                "next_actions": ["让主角在十章内完成第一次资源兑换"],
                "reviewed_chapters": [1, 2, 3],
            }
            debt = commercial_reviewer.update_revision_debt(path, 3, first, horizon=10)
            self.assertEqual(debt["items"][0]["status"], "OPEN")
            self.assertEqual(debt["items"][0]["due_by"], 13)
            rendered = commercial_reviewer.render_revision_debt(
                debt, current_chapter=14
            )
            self.assertIn("已逾期", rendered)

            debt_id = debt["items"][0]["id"]

            verified = {
                "status": "PASS",
                "action": "CONTINUE",
                "debt_status": "RESOLVED",
                "debt_evidence": [f"{debt_id}|VERIFIED|第8章已经完成资源兑换并产生代价"],
                "next_actions": [],
                "reviewed_chapters": list(range(1, 11)),
            }
            closed = commercial_reviewer.update_revision_debt(
                path, 10, verified, horizon=10
            )
            self.assertEqual(closed["items"][0]["status"], "VERIFIED")
            self.assertEqual(closed["items"][0]["closed_chapter"], 10)
            self.assertEqual(
                commercial_reviewer.render_revision_debt(closed, current_chapter=11),
                "",
            )

    def test_repeated_revision_action_does_not_move_its_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            action = "压缩程序复述并增加人物主动选择"
            opened = commercial_reviewer.update_revision_debt(
                path,
                40,
                {
                    "review_id": "review_40",
                    "status": "WARN",
                    "action": "ADJUST",
                    "next_actions": [action],
                },
                horizon=10,
            )
            repeated = commercial_reviewer.update_revision_debt(
                path,
                50,
                {
                    "review_id": "review_50",
                    "status": "WARN",
                    "action": "ADJUST",
                    "next_actions": [action],
                },
                horizon=10,
            )
        self.assertEqual(50, opened["items"][0]["due_by"])
        self.assertEqual(50, repeated["items"][0]["due_by"])
        self.assertEqual(2, repeated["items"][0]["repeat_count"])
        self.assertEqual(50, repeated["items"][0]["last_seen_chapter"])

    def test_paraphrased_future_action_in_same_window_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            first_action = (
                "第61—63章安排一次因灰影失败的复盘，明确系统只提供样本、不提供答案。"
            )
            second_action = (
                "第61-63章加入灰影导致复盘失败的样本，避免把系统写成直接给答案。"
            )
            commercial_reviewer.update_revision_debt(
                path,
                60,
                {"review_id": "r60a", "next_actions": [first_action]},
                horizon=10,
            )
            debt = commercial_reviewer.update_revision_debt(
                path,
                60,
                {"review_id": "r60b", "next_actions": [second_action]},
                horizon=10,
            )

        open_items = [item for item in debt["items"] if item["status"] == "OPEN"]
        self.assertEqual(1, len(open_items))
        self.assertEqual(2, open_items[0]["repeat_count"])
        self.assertEqual([second_action], open_items[0]["alternate_actions"])

    def test_each_revision_debt_requires_evidence_with_its_own_id(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            opened = commercial_reviewer.update_revision_debt(
                path,
                3,
                {
                    "status": "WARN",
                    "action": "ADJUST",
                    "debt_status": "PARTIAL",
                    "debt_evidence": ["首次建立"],
                    "next_actions": ["兑现主卖点", "让盟友关系发生变化"],
                    "reviewed_chapters": [1, 2, 3],
                },
                horizon=10,
            )
            first_id, second_id = [item["id"] for item in opened["items"]]
            checked = commercial_reviewer.update_revision_debt(
                path,
                10,
                {
                    "status": "PASS",
                    "action": "CONTINUE",
                    "debt_status": "RESOLVED",
                    "debt_evidence": [f"{first_id}|VERIFIED|第8章已有正文证据"],
                    "next_actions": [],
                    "reviewed_chapters": list(range(1, 11)),
                },
                horizon=10,
            )
            by_id = {item["id"]: item for item in checked["items"]}
            self.assertEqual("VERIFIED", by_id[first_id]["status"])
            self.assertEqual("OPEN", by_id[second_id]["status"])

    def test_negative_or_legacy_debt_evidence_cannot_close_an_open_item(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            opened = commercial_reviewer.update_revision_debt(
                path,
                60,
                {
                    "debt_status": "PARTIAL",
                    "debt_evidence": ["首次建立"],
                    "next_actions": ["第61章验证失败样本"],
                },
                horizon=10,
            )
            debt_id = opened["items"][0]["id"]
            checked = commercial_reviewer.update_revision_debt(
                path,
                60,
                {
                    "debt_status": "RESOLVED",
                    "debt_evidence": [
                        f"{debt_id}|OPEN|未到核销区间",
                        f"{debt_id}：尚未兑现",
                    ],
                    "next_actions": [],
                },
                horizon=10,
            )
            self.assertEqual("OPEN", checked["items"][0]["status"])

    def test_empty_explicit_debt_evidence_cannot_close_an_open_item(self):
        for proof in ('', '   ', '\t\n'):
            with self.subTest(proof=proof), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, 'revision_debt.json')
                opened = commercial_reviewer.update_revision_debt(
                    path, 84, {'next_actions': ['核对限定的比赛结果']})
                debt_id = opened['items'][0]['id']
                checked = commercial_reviewer.update_revision_debt(
                    path, 85, {
                        'debt_status': 'RESOLVED',
                        'debt_evidence': [f'{debt_id}|VERIFIED|{proof}'],
                        'next_actions': [],
                    })
                self.assertEqual('OPEN', checked['items'][0]['status'])
                self.assertNotIn('closed_chapter', checked['items'][0])

    def test_review_before_debt_opened_chapter_cannot_close_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            opened = commercial_reviewer.update_revision_debt(
                path,
                60,
                {
                    "debt_status": "PARTIAL",
                    "debt_evidence": ["首次建立"],
                    "next_actions": ["第61章验证失败样本"],
                },
                horizon=10,
            )
            debt_id = opened["items"][0]["id"]
            checked = commercial_reviewer.update_revision_debt(
                path,
                50,
                {
                    "debt_status": "RESOLVED",
                    "debt_evidence": [
                        f"{debt_id}|VERIFIED|错误地引用未来章节债务"
                    ],
                    "next_actions": [],
                },
                horizon=10,
            )
            self.assertEqual("OPEN", checked["items"][0]["status"])

    def test_thirty_chapter_hard_gate_uses_lossless_map_reduce(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 全区间可继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 首次建立"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output", "第一卷")
            os.makedirs(out_dir)
            for chapter in range(1, 31):
                with open(
                    os.path.join(out_dir, f"第{chapter}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n" + "调查推进。" * 450)
            calls = []
            result = commercial_reviewer.run_review(
                output_dir=os.path.join(root, "output"),
                report_dir=os.path.join(root, "reports"),
                chapter_num=30,
                current_content="第30章 正文\n\n" + "调查推进。" * 450,
                config={
                    "commercial_review_enabled": True,
                    "commercial_hard_gate_chapters": [30],
                    "commercial_review_lookback": 30,
                    "commercial_review_fulltext_chars": 24000,
                    "golden_three_require_explicit_continue": False,
                    "commercial_review_volume_end": False,
                },
                volume_ranges=[],
                story_context="故事契约",
                canon_context="正史",
                llm_call=lambda system, user: calls.append((system, user)) or passing,
                model_name="deepseek-v4-pro",
                persist=False,
            )
            self.assertEqual(result["reviewed_chapters"], list(range(1, 31)))
            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual(
                result["review_receipt"]["version"],
                commercial_reviewer.MULTI_REVIEW_RECEIPT_VERSION,
            )
            commercial_reviewer.verify_review_receipt(
                result, model_name="deepseek-v4-pro"
            )
            wrong_reduce = json.loads(json.dumps(result, ensure_ascii=False))
            wrong_reduce["review_receipt"]["stages"][-1][
                "reviewed_chapters"
            ] = []
            wrong_reduce["review_receipt"]["receipt_sha256"] = (
                commercial_reviewer._receipt_digest(
                    wrong_reduce["review_receipt"]
                )
            )
            with self.assertRaisesRegex(RuntimeError, "reduce|覆盖"):
                commercial_reviewer.verify_review_receipt(
                    wrong_reduce, model_name="deepseek-v4-pro"
                )
            tampered = json.loads(json.dumps(result, ensure_ascii=False))
            tampered["map_reviews"][0]["status"] = "FAIL"
            with self.assertRaisesRegex(RuntimeError, "分块商业审稿结构字段"):
                commercial_reviewer.verify_review_receipt(
                    tampered, model_name="deepseek-v4-pro"
                )

    def test_hard_gate_retries_one_malformed_map_response(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 全区间可继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已核验正文"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            os.makedirs(out_dir)
            for chapter in range(1, 31):
                with open(
                    os.path.join(out_dir, f"第{chapter}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n" + "调查推进。" * 450)
            calls = {"main": 0, "retry": 0}

            def main_review(_system, _user):
                calls["main"] += 1
                return "格式错误" if calls["main"] == 2 else passing

            def retry_review(_system, _user):
                calls["retry"] += 1
                return passing

            result = commercial_reviewer.run_review(
                output_dir=out_dir,
                report_dir=os.path.join(root, "reports"),
                chapter_num=30,
                current_content="第30章 正文\n\n" + "调查推进。" * 450,
                config={
                    "commercial_review_enabled": True,
                    "commercial_hard_gate_chapters": [30],
                    "commercial_review_lookback": 30,
                    "commercial_review_fulltext_chars": 24000,
                    "golden_three_require_explicit_continue": False,
                    "commercial_review_volume_end": False,
                },
                volume_ranges=[],
                story_context="故事契约",
                canon_context="正史",
                llm_call=main_review,
                llm_retry=retry_review,
                model_name="deepseek-v4-pro",
                persist=False,
            )
            self.assertEqual(1, calls["retry"])
            self.assertEqual("PASS", result["status"])
            commercial_reviewer.verify_review_receipt(
                result, model_name="deepseek-v4-pro"
            )

    def test_hard_gate_map_prompt_does_not_fail_for_unseen_chunks(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 本分块无硬伤\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 跨分块债务交由最终汇总核验"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            os.makedirs(out_dir)
            for chapter in range(1, 31):
                with open(
                    os.path.join(out_dir, f"第{chapter}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n" + "冲突推进。" * 450)
            prompts = []

            def scoped_review(system, user):
                prompts.append((system, user))
                return passing

            result = commercial_reviewer.run_review(
                output_dir=out_dir,
                report_dir=os.path.join(root, "reports"),
                chapter_num=30,
                current_content="第30章 正文\n\n" + "冲突推进。" * 450,
                config={
                    "commercial_review_enabled": True,
                    "commercial_hard_gate_chapters": [30],
                    "commercial_review_lookback": 30,
                    "commercial_review_fulltext_chars": 24000,
                    "golden_three_require_explicit_continue": False,
                    "commercial_review_volume_end": False,
                },
                volume_ranges=[],
                story_context="故事契约",
                canon_context="正史",
                llm_call=scoped_review,
                model_name="deepseek-v4-pro",
                persist=False,
            )
            self.assertEqual("PASS", result["status"])
            map_prompts = prompts[:-1]
            self.assertGreaterEqual(len(map_prompts), 2)
            for _system, user in map_prompts:
                self.assertIn("硬闸门证据分块", user)
                self.assertIn("不得因为没有提供其他分块章节", user)
                self.assertIn("最终裁决由后续汇总完成", user)

    def test_volume_end_fifty_chapters_ignores_thirty_chapter_lookback(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 全卷可继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 首次建立"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output", "第一卷")
            os.makedirs(out_dir)
            for chapter in range(1, 51):
                with open(
                    os.path.join(out_dir, f"第{chapter}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n" + "线索落地。" * 180)
            result = commercial_reviewer.run_review(
                output_dir=os.path.join(root, "output"),
                report_dir=os.path.join(root, "reports"),
                chapter_num=50,
                current_content="第50章 正文\n\n" + "线索落地。" * 180,
                config={
                    "commercial_review_enabled": True,
                    "commercial_review_lookback": 30,
                    "commercial_review_fulltext_chars": 24000,
                    "commercial_hard_gate_chapters": [],
                    "golden_three_require_explicit_continue": False,
                    "commercial_review_volume_end": True,
                },
                volume_ranges=[(1, 50, "第一卷")],
                story_context="故事契约",
                canon_context="正史",
                llm_call=lambda _system, _user: passing,
                model_name="deepseek-v4-pro",
                persist=False,
            )
            self.assertEqual(result["reviewed_chapters"], list(range(1, 51)))

    def test_map_warn_cannot_be_overridden_by_reduce_pass(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 可继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已核验当前正文"
        )
        warning = (
            "OVERALL: WARN\nCURRENT: PASS\nACTION: ADJUST\n"
            "SUMMARY: 分块仍有问题\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已定位分块问题"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            os.makedirs(out_dir)
            for chapter in range(1, 31):
                with open(
                    os.path.join(out_dir, f"第{chapter}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n" + "冲突推进。" * 450)
            call_count = {"value": 0}

            def fake_review(_system, _user):
                call_count["value"] += 1
                return warning if call_count["value"] == 1 else passing

            with self.assertRaisesRegex(RuntimeError, "覆盖分块失败"):
                commercial_reviewer.run_review(
                    output_dir=out_dir,
                    report_dir=os.path.join(root, "reports"),
                    chapter_num=30,
                    current_content="第30章 正文\n\n" + "冲突推进。" * 450,
                    config={
                        "commercial_review_enabled": True,
                        "commercial_hard_gate_chapters": [30],
                        "commercial_review_lookback": 30,
                        "commercial_review_fulltext_chars": 24000,
                        "golden_three_require_explicit_continue": False,
                        "commercial_review_volume_end": False,
                    },
                    volume_ranges=[],
                    story_context="故事契约",
                    canon_context="正史",
                    llm_call=fake_review,
                    model_name="deepseek-v4-pro",
                    persist=False,
                )

    def test_receipt_rejects_raw_or_structured_result_tampering(self):
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 可继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已核验当前正文"
        )
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            os.makedirs(out_dir)
            result = commercial_reviewer.run_review(
                output_dir=out_dir,
                report_dir=os.path.join(root, "reports"),
                chapter_num=1,
                current_content="第1章 正文\n\n线索落地。",
                config={
                    "commercial_review_enabled": True,
                    "commercial_review_milestones": [1],
                    "commercial_hard_gate_chapters": [],
                    "golden_three_require_explicit_continue": False,
                    "commercial_review_volume_end": False,
                },
                volume_ranges=[],
                story_context="故事契约",
                canon_context="正史",
                llm_call=lambda _system, _user: passing,
                model_name="deepseek-v4-pro",
                persist=False,
            )
            altered = dict(result)
            altered["status"] = "FAIL"
            with self.assertRaisesRegex(RuntimeError, "结构字段"):
                commercial_reviewer.verify_review_receipt(
                    altered, model_name="deepseek-v4-pro"
                )
            altered = dict(result)
            altered["raw"] = passing + "\nRISKS:\n- 人工追加"
            with self.assertRaisesRegex(RuntimeError, "正文与调用收据"):
                commercial_reviewer.verify_review_receipt(
                    altered, model_name="deepseek-v4-pro"
                )

    def test_revision_debt_history_is_idempotent_by_review_id(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "revision_debt.json")
            result = {
                "review_id": "review_same",
                "status": "WARN",
                "action": "ADJUST",
                "debt_status": "PARTIAL",
                "next_actions": ["十章内兑现主卖点"],
                "reviewed_chapters": [1, 2, 3],
            }
            commercial_reviewer.update_revision_debt(path, 3, result, horizon=10)
            debt = commercial_reviewer.update_revision_debt(path, 3, result, horizon=10)
            self.assertEqual(len(debt["review_history"]), 1)
            self.assertEqual(len(debt["items"]), 1)


if __name__ == "__main__":
    unittest.main()
