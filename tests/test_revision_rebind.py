"""Explicit future-input repair tests; no real book, API, or model calls.

Receipt-backed stand-ins exercise session orchestration. The production prefix
verifier's real receipt and state checks are covered in test_revision_session.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import chapter_budget
import chapter_commit
import novel_cli
import revision_session


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RevisionRebindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        self.checkpoint = self.root / "checkpoint"
        self.candidates = self.project / "rewrite_staging" / "old"
        self.repairs = self.project / "rewrite_staging" / "repair"
        self.bundle = self.candidates / "配套"
        self.bundle_new = self.repairs / "配套"
        for project in (self.project, self.checkpoint):
            for relative in ("output/第01卷", "plot/runtime", "characters", "world_building"):
                (project / relative).mkdir(parents=True, exist_ok=True)
            _write_json(project / "project_config.json", {"model": "offline-test"})
            (project / "characters/主角.txt").write_text("角色设定不变", encoding="utf-8")
            (project / "world_building/规则.txt").write_text("灰影不能服从新指令", encoding="utf-8")
            (project / "plot/写作进度.md").write_text("活动记录", encoding="utf-8")
        self.candidates.mkdir(parents=True)
        self.repairs.mkdir(parents=True)
        for number in range(1, 5):
            self.chapter_path(self.project, number).write_text(
                f"第{number}章 旧正式\n\n原正式正文。", encoding="utf-8"
            )
            (self.candidates / f"第{number:04d}章.txt").write_text(
                f"第{number}章 候选\n\n旧候选第{number}章正文。", encoding="utf-8"
            )
        self.bundle.mkdir()
        outline = "全局规则：训练不触发系统。\n\n" + "\n\n".join(
            f"第{number}章 候选\n核心事件：旧候选事件{number}。\n"
            f"状态落点：2026-09-{number + 4:02d}，一枚片段。\n伏笔/禁出内容：灰影不响应新指令。"
            for number in range(1, 5)
        ) + "\n"
        content = {
            "第一卷逐章细纲.txt": outline,
            "AI滚动逐章细纲.txt": (
                "滚动全局规则：不越级注册。\n\n第31章 后组\n"
                "核心事件：未发生的后组训练。\n状态落点：2026-12-01。\n"
            ),
            "比赛与成长台账.md": (
                "# 比赛与成长台账\n\n## 系统使用记录\n\n"
                "| 比赛日期 | 实际使用 | 剩余 |\n| --- | --- | --- |\n"
                "|2026-09-05|63次|37次|\n|2026-09-06|完成|已关闭|\n"
                "|2026-09-07|51次|49次|\n|2026-09-08|静默|无新窗口|\n"
            ),
            "当前卷大纲.txt": "第一卷总合同：身体技术不能顿悟，系统规则保持不变。\n",
        }
        for name, text in content.items():
            (self.bundle / name).write_text(text, encoding="utf-8")
            for project in (self.project, self.checkpoint):
                shutil.copy2(self.bundle / name, project / "plot" / name)
        self.budget = self.project / "runtime/chapter_budgets/chapter_0003.json"
        _write_json(self.budget, {"epoch": "unchanged-epoch", "calls": 1, "repairs": 0})
        self.record_committed(1)
        self.record_committed(2)
        self.calls = []
        self.prefix = mock.patch(
            "revision_session.verify_prefix", side_effect=self.verify_prefix
        ).start()
        self.factory = mock.patch(
            "revision_session._make_app", side_effect=self.make_app
        ).start()
        mock.patch("novel_cli.build_doctor_report", return_value={
            "overall_status": "PASS", "formal": {"latest_chapter": 4},
            "project": {"latest_chapter": 4},
        }).start()
        self.addCleanup(mock.patch.stopall)
        self.session = revision_session.RevisionSession.create(
            self.project, self.candidates, checkpoint=self.checkpoint,
            bundle_dir=self.bundle,
        )
        # An actual failure record must survive repair; it is not a new approval.
        self.session.row.update(status="STOPPED", current_chapter=3, error="第3章叙事未通过")
        self.session.row["events"].append({
            "event": "checkpoint_stopped", "chapter": 3, "error": "第3章叙事未通过",
        })
        self.session._save()
        self.prefix.reset_mock()

    @staticmethod
    def chapter_path(project, number):
        return Path(project) / "output/第01卷" / f"第{number:04d}章.txt"

    def receipt_path(self, number):
        return self.checkpoint / "plot/runtime/offline_verified_receipts" / f"{number}.json"

    def record_committed(self, number, text=None):
        if text is None:
            text = (self.candidates / f"第{number:04d}章.txt").read_text(encoding="utf-8")
        path = self.chapter_path(self.checkpoint, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _write_json(self.receipt_path(number), {
            "chapter": number, "chapter_sha256": chapter_commit.chapter_sha256(text),
            "offline_verified": True,
        })

    def verify_prefix(self, project, inputs):
        self.assertEqual(self.checkpoint.resolve(), Path(project).resolve())
        chapters = {}
        tip = 0
        for number in range(1, 5):
            path = self.chapter_path(project, number)
            if not path.exists():
                break
            receipt = self.receipt_path(number)
            if not receipt.exists():
                raise RuntimeError(f"第{number}章缺真实收据")
            row = revision_session.read_json(receipt)
            digest = chapter_commit.chapter_sha256(path.read_text(encoding="utf-8"))
            binding = inputs[str(number)]
            if (not row["offline_verified"] or row["chapter_sha256"] != digest
                    or binding["chapter_sha256"] != digest):
                raise RuntimeError(f"第{number}章证据无效")
            chapters[str(number)] = {
                "chapter_sha256": digest, "receipt_sha256": revision_session.sha256(receipt),
            }
            tip = number
        return {"tip": tip, "chapters": chapters}

    def make_app(self, project, budget_project, progress, lock=None):
        self.assertEqual(self.checkpoint.resolve(), Path(project).resolve())
        self.assertEqual(self.project.resolve(), Path(budget_project).resolve())
        owner = self

        class FakeApp:
            def _restore_project_bindings(self):
                pass

            def _get_volume_ranges(self):
                return [(1, 30, "第一卷")]

            def _commit_manual_chapter(self, number, target, text, *args, **kwargs):
                owner.calls.append(number)
                owner.assertEqual(owner.chapter_path(owner.checkpoint, number), Path(target))
                owner.record_committed(number, text)

        return FakeApp()

    def replacement(self, number=3, text=None):
        path = self.repairs / f"第{number:04d}章.txt"
        # Preserve an explicit CRLF fixture on Windows: default newline
        # translation would turn it into CRCRLF and add real blank lines.
        path.write_text(text or f"第{number}章 修复候选\n\n定点修复后的真实正文。",
                        encoding="utf-8", newline="")
        return path

    def new_bundle(self, edits=None):
        shutil.copytree(self.bundle, self.bundle_new)
        for name, change in (edits or {}).items():
            path = self.bundle_new / name
            path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
        return self.bundle_new

    def reject_without_writes(self, replacements, *, reason="定点修复", bundle_dir=None):
        before = _tree_hashes(self.root)
        row = copy.deepcopy(self.session.row)
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            self.session.rebind(replacements, reason=reason, bundle_dir=bundle_dir)
        self.assertEqual(row, self.session.row)
        self.assertEqual(before, _tree_hashes(self.root))
        self.factory.assert_not_called()

    def test_substantive_future_repair_keeps_formal_prefix_budget_and_failure_history(self):
        replacement = self.replacement()
        source_before = revision_session.source_manifest(self.project)
        prefix_before = _tree_hashes(self.checkpoint / "output")
        receipts_before = _tree_hashes(self.checkpoint / "plot/runtime/offline_verified_receipts")
        budget_before = self.budget.read_bytes()
        old_binding = copy.deepcopy(self.session.row["inputs"]["3"])
        old_proof = copy.deepcopy(self.session.row["completed_chapters"])
        old_events = copy.deepcopy(self.session.row["events"])
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            with redirect_stdout(StringIO()):
                result = self.session.rebind({3: replacement}, reason="补赛后18次复盘并主动关闭")
        self.assertEqual("READY", result["status"])
        self.assertEqual(2, result["verified_through"])
        self.assertEqual(3, result["next_chapter"])
        self.prefix.assert_called_once()
        self.factory.assert_not_called()
        renew.assert_not_called()
        self.assertEqual(source_before, revision_session.source_manifest(self.project))
        self.assertEqual(prefix_before, _tree_hashes(self.checkpoint / "output"))
        self.assertEqual(receipts_before, _tree_hashes(self.checkpoint / "plot/runtime/offline_verified_receipts"))
        self.assertEqual(budget_before, self.budget.read_bytes())
        self.assertEqual(old_proof, self.session.row["completed_chapters"])
        self.assertEqual(old_events, self.session.row["events"][:-1])
        event = self.session.row["events"][-1]
        self.assertEqual("future_inputs_rebound", event["event"])
        self.assertEqual("补赛后18次复盘并主动关闭", event["reason"])
        self.assertEqual(2, event["verified_through"])
        self.assertEqual(old_binding, event["changes"]["3"]["old"])
        self.assertEqual(str(replacement.resolve()), event["changes"]["3"]["new"]["path"])
        self.assertEqual(revision_session.sha256(replacement), event["changes"]["3"]["new"]["sha256"])
        self.assertEqual(old_binding["sha256"], revision_session.sha256(old_binding["path"]))

    def test_reopen_after_repair_only_commits_next_chapter_without_rechecking_models_one_two(self):
        replacement = self.replacement()
        with redirect_stdout(StringIO()):
            self.session.rebind({3: replacement}, reason="真实修改第3章")
            reopened = revision_session.RevisionSession.open(self.project)
            result = reopened.advance(3)
        self.assertEqual(3, result["verified_through"])
        self.assertEqual([3], self.calls)
        self.assertEqual(replacement.read_text(encoding="utf-8"),
                         self.chapter_path(self.checkpoint, 3).read_text(encoding="utf-8"))

    def test_multiple_future_inputs_rebind_as_one_explicit_batch(self):
        repairs = {3: self.replacement(3), 4: self.replacement(4)}
        with redirect_stdout(StringIO()):
            self.session.rebind(repairs, reason="修3章窗口与4章邻接承接")
        self.assertEqual({"3", "4"}, set(self.session.row["events"][-1]["changes"]))
        self.assertEqual(2, self.session.row["completed_through"])
        self.factory.assert_not_called()

    def test_later_input_rebind_cannot_clear_current_chapter_failure(self):
        old_error = self.session.row["error"]
        budget_before = self.budget.read_bytes()
        with redirect_stdout(StringIO()):
            result = self.session.rebind({4: self.replacement(4)}, reason="只修后续第4章，不修第3章")
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(old_error, result["error"])
        self.assertTrue(self.session.row["events"][-1]["stop_retained"])
        self.assertEqual(old_error, self.session.row["events"][-1]["prior_error"])
        self.assertEqual(budget_before, self.budget.read_bytes())
        self.factory.assert_not_called()

    def test_budget_stop_survives_even_substantive_current_input_rebind(self):
        budget_before = self.budget.read_bytes()
        errors = ("CHAPTER_BUDGET_STOPPED：预算耗尽", "MANUAL_STATE_AUDIT_STOPPED：已入场两份",
                  "MANUAL_STATE_AUDIT_WINDOW_INSUFFICIENT：不足120秒")
        for index, error in enumerate(errors):
            folder = self.repairs / f"stop_{index}"
            folder.mkdir()
            replacement = folder / "第0003章.txt"
            replacement.write_text(f"第3章 新候选\n实质修复编号{index}的正文。", encoding="utf-8")
            self.session.row.update(status="STOPPED", current_chapter=3, error=error)
            self.session._save()
            with self.subTest(error=error), redirect_stdout(StringIO()), \
                 mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
                result = self.session.rebind({3: replacement}, reason="修正文但不续预算")
                self.assertEqual("STOPPED", result["status"])
                self.assertEqual(error, result["error"])
                renew.assert_not_called()
                self.assertEqual(budget_before, self.budget.read_bytes())
        self.factory.assert_not_called()

    def test_current_semantic_repair_is_ready_but_prior_failure_remains_auditable(self):
        error = self.session.row["error"]
        with redirect_stdout(StringIO()):
            result = self.session.rebind({3: self.replacement(3)}, reason="实修当前拒绝正文")
        self.assertEqual("READY", result["status"])
        event = self.session.row["events"][-1]
        self.assertFalse(event["stop_retained"])
        self.assertEqual(error, event["prior_error"])
        self.factory.assert_not_called()

    def test_cli_successful_input_only_rebind_is_not_reported_as_retryable_failure(self):
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            code = novel_cli.main([
                "revision", "--project", str(self.project), "--session", str(self.session.path),
                "--replace", f"4={self.replacement(4)}", "--reason", "仅同步后续输入", "--execute",
            ])
        self.assertEqual(0, code)
        self.assertIn('"operation_status": "INPUTS_REBOUND"', stdout.getvalue())
        self.assertIn('"status": "STOPPED"', stdout.getvalue())
        self.assertIn('"advanced": false', stdout.getvalue())
        reopened = revision_session.RevisionSession.open(self.project, self.session.path)
        self.assertEqual("STOPPED", reopened.row["status"])
        self.factory.assert_not_called()

    def test_rebinding_committed_chapter_is_rejected_using_real_prefix(self):
        self.reject_without_writes({2: self.replacement(2)})
        self.prefix.assert_called_once()

    def test_stale_low_metadata_does_not_allow_repair_of_actual_committed_chapter(self):
        self.session.row["completed_through"] = 1
        self.session.row["completed_chapters"].pop("2")
        self.session._save()
        self.reject_without_writes({2: self.replacement(2)})

    def test_actual_receipts_reconcile_lower_metadata_during_future_repair(self):
        self.session.row["completed_through"] = 1
        self.session.row["completed_chapters"].pop("2")
        self.session._save()
        with redirect_stdout(StringIO()):
            result = self.session.rebind({3: self.replacement()}, reason="只修未验3")
        self.assertEqual(2, result["verified_through"])
        self.assertEqual({"1", "2"}, set(self.session.row["completed_chapters"]))
        self.factory.assert_not_called()

    def test_high_metadata_without_receipt_cannot_be_promoted_by_rebind(self):
        self.session.row["completed_through"] = 3
        self.session._save()
        self.reject_without_writes({4: self.replacement(4)})

    def test_missing_actual_prefix_receipt_refuses_rebind(self):
        self.receipt_path(2).unlink()
        self.reject_without_writes({3: self.replacement()})

    def test_whitespace_only_body_change_does_not_buy_another_review(self):
        text = (self.candidates / "第0003章.txt").read_text(encoding="utf-8")
        self.reject_without_writes({3: self.replacement(text=text.replace("\n", "\r\n") + "\n  ")})

    def test_candidate_outside_own_rewrite_staging_is_refused(self):
        external = self.root / "other_book/rewrite_staging/第0003章.txt"
        external.parent.mkdir(parents=True)
        external.write_text("第3章 外书候选\n\n新正文。", encoding="utf-8")
        self.reject_without_writes({3: external})

    def test_chapter_heading_and_filename_must_both_match_binding_number(self):
        for number, title in ((3, "第4章 错误标题"), (4, "第3章 错误文件名")):
            with self.subTest(number=number, title=title):
                self.reject_without_writes({3: self.replacement(number, title + "\n\n新正文。")})

    def test_one_invalid_replacement_rolls_back_the_entire_batch(self):
        repairs = {3: self.replacement(3), 4: self.replacement(4, "第3章 错误标题\n\n变化。")}
        self.reject_without_writes(repairs)

    def test_empty_reason_or_empty_replacement_set_is_rejected_without_models(self):
        replacement = self.replacement()
        for replacements, reason in (({3: replacement}, ""), ({3: replacement}, "  "), ({}, "修复")):
            with self.subTest(reason=reason, chapters=list(replacements)):
                self.reject_without_writes(replacements, reason=reason)

    def test_old_bound_input_drift_is_not_erased_by_rebinding_to_a_new_version(self):
        (self.candidates / "第0003章.txt").write_text("第3章 被原地篡改的旧候选", encoding="utf-8")
        self.reject_without_writes({3: self.replacement()})
        self.prefix.assert_not_called()

    def test_source_fact_drift_is_not_accepted_as_new_baseline(self):
        (self.project / "world_building/规则.txt").write_text("灰影改为服从指令", encoding="utf-8")
        self.reject_without_writes({3: self.replacement()})
        self.prefix.assert_not_called()

    def test_started_or_completed_formal_adoption_cannot_replace_isolation_inputs(self):
        replacement = self.replacement()
        for state in ("RUNNING", "COMPLETED"):
            with self.subTest(state=state):
                self.session.row["formal_adoption"] = state
                self.session._save()
                self.reject_without_writes({3: replacement})

    def test_pending_commit_must_be_recovered_before_any_rebinding(self):
        _write_json(self.checkpoint / "plot/runtime" / chapter_commit.JOURNAL_FILENAME, {
            "schema_version": chapter_commit.SCHEMA_VERSION, "chapter": 3,
            "chapter_sha256": self.session.row["inputs"]["3"]["chapter_sha256"],
            "chapter_path": str(self.chapter_path(self.checkpoint, 3)),
            "plot_dir": str(self.checkpoint / "plot"), "steps": {},
        })
        self.reject_without_writes({3: self.replacement()})

    def test_future_outline_and_same_date_ledger_row_can_change_with_bound_prose(self):
        repaired_bundle = self.new_bundle({
            "第一卷逐章细纲.txt": lambda text: text.replace("旧候选事件3。", "修复事件3，18次后关闭。"),
            "比赛与成长台账.md": lambda text: text.replace("|2026-09-07|51次|49次|", "|2026-09-07|18次|82次放弃|"),
        })
        source_before = revision_session.source_manifest(self.project)
        receipts_before = _tree_hashes(self.checkpoint / "plot/runtime/offline_verified_receipts")
        old_ledger = (self.checkpoint / "plot/比赛与成长台账.md").read_bytes()
        old_bundle = copy.deepcopy(self.session.row["bundle"])
        with redirect_stdout(StringIO()):
            self.session.rebind({3: self.replacement()}, reason="未来3章及同日预算同步", bundle_dir=repaired_bundle)
        self.assertEqual(source_before, revision_session.source_manifest(self.project))
        self.assertEqual(receipts_before, _tree_hashes(self.checkpoint / "plot/runtime/offline_verified_receipts"))
        self.assertEqual((repaired_bundle / "比赛与成长台账.md").read_bytes(),
                         (self.checkpoint / "plot/比赛与成长台账.md").read_bytes())
        event = self.session.row["events"][-1]
        self.assertEqual(old_bundle, event["bundle_before"])
        self.assertEqual(self.session.row["bundle"], event["bundle_after"])
        saved = Path(event["checkpoint_bundle_backups"]["比赛与成长台账.md"])
        self.assertEqual(old_ledger, saved.read_bytes())
        self.factory.assert_not_called()

    def test_incomplete_bundle_cannot_partially_replace_checkpoint(self):
        repaired_bundle = self.new_bundle()
        (repaired_bundle / "当前卷大纲.txt").unlink()
        self.reject_without_writes({3: self.replacement()}, bundle_dir=repaired_bundle)

    def test_duplicate_chapter_heading_in_replacement_bundle_is_ambiguous_and_refused(self):
        repaired_bundle = self.new_bundle({
            "第一卷逐章细纲.txt": lambda text: text.replace(
                "第4章 候选", "第3章 重复合同\n核心事件：另一个第三章答案。\n\n第4章 候选"
            ),
        })
        self.reject_without_writes({3: self.replacement()}, bundle_dir=repaired_bundle)

    def test_error_after_event_persistence_keeps_disk_metadata_and_bundle_consistent(self):
        repaired_bundle = self.new_bundle({
            "比赛与成长台账.md": lambda text: text.replace(
                "|2026-09-07|51次|49次|", "|2026-09-07|18次|82次放弃|"
            ),
        })
        replacement = self.replacement()
        old_row = copy.deepcopy(self.session.row)
        old_metadata = Path(self.session.path).read_bytes()
        old_plot = _tree_hashes(self.checkpoint / "plot")
        event = self.session._event

        def persisted_then_observer_failed(payload):
            event(payload)
            # A closed pipe can fail after _event has durably saved new input
            # bindings. Either rollback or success must retain one disk truth.
            raise BrokenPipeError("输出观察者已关闭")

        with mock.patch.object(self.session, "_event", side_effect=persisted_then_observer_failed):
            with redirect_stdout(StringIO()):
                with self.assertRaises(BrokenPipeError):
                    self.session.rebind({3: replacement}, reason="事务输出异常", bundle_dir=repaired_bundle)
        self.assertEqual(old_row, self.session.row)
        self.assertEqual(old_metadata, Path(self.session.path).read_bytes())
        self.assertEqual(old_plot, _tree_hashes(self.checkpoint / "plot"))
        self.factory.assert_not_called()

    def test_global_outline_preamble_and_global_volume_contract_are_protected(self):
        for name in ("第一卷逐章细纲.txt", "AI滚动逐章细纲.txt", "当前卷大纲.txt"):
            with self.subTest(name=name):
                directory = self.repairs / name.replace(".txt", "")
                shutil.copytree(self.bundle, directory)
                path = directory / name
                path.write_text("全局改为无限次数\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
                self.reject_without_writes({3: self.replacement()}, bundle_dir=directory)

    def test_committed_and_unreplaced_future_outline_sections_cannot_change(self):
        for chapter in (1, 2, 4):
            with self.subTest(chapter=chapter):
                directory = self.repairs / f"bundle_change_{chapter}"
                shutil.copytree(self.bundle, directory)
                path = directory / "第一卷逐章细纲.txt"
                path.write_text(path.read_text(encoding="utf-8").replace(
                    f"旧候选事件{chapter}。", f"越界修改事件{chapter}。"), encoding="utf-8")
                self.reject_without_writes({3: self.replacement()}, bundle_dir=directory)

    def test_ledger_dates_outside_replaced_chapter_or_in_prefix_are_protected(self):
        for day in ("05", "06", "08"):
            with self.subTest(day=day):
                directory = self.repairs / f"bundle_date_{day}"
                shutil.copytree(self.bundle, directory)
                path = directory / "比赛与成长台账.md"
                path.write_text(path.read_text(encoding="utf-8").replace(
                    f"|2026-09-{day}|", f"|2026-09-{day}|改动|"), encoding="utf-8")
                self.reject_without_writes({3: self.replacement()}, bundle_dir=directory)

    def test_ledger_cannot_add_rows_delete_rows_or_change_date(self):
        transformations = (
            lambda text: text + "|2026-09-07|新增行|82次|\n",
            lambda text: text.replace("|2026-09-07|51次|49次|\n", ""),
            lambda text: text.replace("|2026-09-07|", "|2026-09-09|"),
        )
        for index, change in enumerate(transformations):
            with self.subTest(index=index):
                directory = self.repairs / f"bundle_row_{index}"
                shutil.copytree(self.bundle, directory)
                path = directory / "比赛与成长台账.md"
                path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
                self.reject_without_writes({3: self.replacement()}, bundle_dir=directory)

    def test_global_ledger_text_is_not_allowed_as_a_table_row_repair(self):
        repaired_bundle = self.new_bundle({
            "比赛与成长台账.md": lambda text: text.replace("# 比赛与成长台账", "# 全局规则已改变"),
        })
        self.reject_without_writes({3: self.replacement()}, bundle_dir=repaired_bundle)

    def test_old_bundle_and_checkpoint_config_drift_refuse_before_rebinding(self):
        replacement = self.replacement()
        for path in (self.bundle / "当前卷大纲.txt", self.checkpoint / "project_config.json"):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\n ")
                self.reject_without_writes({3: replacement})
                path.write_bytes(original)

    def test_cli_replacement_without_execute_is_read_only(self):
        replacement = self.replacement()
        before = _tree_hashes(self.root)
        with mock.patch.object(revision_session.RevisionSession, "rebind") as rebind:
            with mock.patch.object(revision_session.RevisionSession, "advance") as advance:
                with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
                    code = novel_cli.main([
                        "revision", "--project", str(self.project), "--replace", f"3={replacement}",
                        "--reason", "先看断点", "--through", "3",
                    ])
        self.assertEqual(0, code)
        self.assertIn('"read_only": true', output.getvalue())
        rebind.assert_not_called()
        advance.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(before, _tree_hashes(self.root))

    def test_cli_execute_rebind_without_through_does_not_call_models_or_advance(self):
        replacement = self.replacement()
        with mock.patch.object(revision_session.RevisionSession, "advance") as advance:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main([
                    "revision", "--project", str(self.project), "--replace", f"3={replacement}",
                    "--reason", "只重绑定修复", "--execute",
                ])
        self.assertEqual(0, code)
        advance.assert_not_called()
        self.factory.assert_not_called()
        reopened = revision_session.RevisionSession.open(self.project)
        self.assertEqual(str(replacement.resolve()), reopened.row["inputs"]["3"]["path"])
        self.assertEqual(2, reopened.summary()["verified_through"])

    def test_cli_execute_rebind_then_through_resumes_at_three(self):
        replacement = self.replacement()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = novel_cli.main([
                "revision", "--project", str(self.project), "--replace", f"3={replacement}",
                "--reason", "修复后接续一章", "--through", "3", "--execute",
            ])
        self.assertEqual(0, code)
        self.assertEqual([3], self.calls)

    def test_cli_rebind_and_apply_are_separate_explicit_actions(self):
        replacement = self.replacement()
        before = _tree_hashes(self.root)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = novel_cli.main([
                "revision", "--project", str(self.project), "--replace", f"3={replacement}",
                "--reason", "拒绝混用", "--apply", "--execute",
            ])
        self.assertEqual(2, code)
        self.factory.assert_not_called()
        self.assertEqual(before, _tree_hashes(self.root))


if __name__ == "__main__":
    unittest.main()
