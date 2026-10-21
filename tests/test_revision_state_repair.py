"""Bounded manual state-repair orchestration; all paid review work is fake."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import chapter_budget
import chapter_commit
import formal_suffix_rewrite
import novel_cli
import project_job_lock
import revision_session
import state_ledger
import state_repair


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _tree_hashes(root):
    root = Path(root)
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _budget_row(project, *, epoch, calls=0, repairs=0):
    """A real schema fixture, never a relaxation of production budget parsing."""
    row = chapter_budget.ChapterBudget(project, 3)._new()
    instant = time.time()
    row.update(epoch=epoch, calls=calls, repairs=repairs,
               started_at=instant if calls else None, last_seen=instant)
    return row


class RevisionStateRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        mock.patch("chapter_budget.time.time", return_value=1_800_000_000.0).start()
        self.project = self.root / "source"
        self.checkpoint = self.root / "checkpoint"
        self.candidates = self.project / "rewrite_staging/candidates"
        self.candidates.mkdir(parents=True)
        for project in (self.project, self.checkpoint):
            for relative in ("output/第01卷", "plot/runtime", "characters", "world_building"):
                (project / relative).mkdir(parents=True, exist_ok=True)
            _write_json(project / "project_config.json", {"model": "offline-state-test"})
            (project / "plot/比赛与成长台账.md").write_text("正式事实保持不变", encoding="utf-8")
            (project / "characters/主角.txt").write_text("角色保持不变", encoding="utf-8")
        for number in range(1, 5):
            self._body(self.project, number).write_text(f"第{number}章 旧正式正文", encoding="utf-8")
            (self.candidates / f"第{number:04d}章.txt").write_text(
                f"第{number}章 候选\n\n周予安选择了一个片段。韩教练完成初筛。", encoding="utf-8"
            )
        for number in (1, 2):
            self._body(self.checkpoint, number).write_bytes(
                (self.candidates / f"第{number:04d}章.txt").read_bytes()
            )
        self.state = state_ledger.initial_state()
        self.state.update(current_chapter=2, applied_chapters={str(number): chapter_commit.chapter_sha256(
            self._body(self.checkpoint, number).read_text(encoding="utf-8")) for number in (1, 2)})
        _write_json(self.checkpoint / "plot/story_state.json", self.state)
        self.fp = "a" * 64
        self.audit_calls = []
        self.commit_calls = []
        self.budget_begins = []
        self.fail_audit = False
        self.fail_service = False
        self.materialize_budget_on_begin = False
        self.invalid_prepared = False
        self.previous = {
            "chapter": 3, "resources": [{"owner": "周予安", "item": "可选复盘片段",
                                          "quantity": 3, "status": "仍可选择三个片段",
                                          "evidence_quote": "周予安选择了一个片段。"}],
            "characters": [{"name": "周予安", "knowledge_add": [], "abilities_add": [],
                             "evidence_quote": "周予安选择了一个片段。"}],
            "events": [{"event_type": "world", "summary": "主角选择片段",
                        "evidence_quote": "周予安选择了一个片段。"}],
        }
        chapter_hash = chapter_commit.chapter_sha256((self.candidates / "第0003章.txt").read_text(encoding="utf-8"))
        self.rejected = {
            "schema_version": 1, "status": "REJECTED", "repair_scope": "state_only",
            "chapter": 3, "chapter_sha256": chapter_hash,
            "previous_raw": json.dumps(self.previous, ensure_ascii=False),
            "issues": ["resources[0]遗漏本章已选择片段的资源变化", "events提取遗漏韩教练初筛"],
            "repair_history": [{"issues": ["此前实际独立审计拒绝"], "snapshot_sha256": "b" * 64}],
        }
        self.patch_file = self.project / "rewrite_staging/state_patch.json"
        self.context = {
            "schema_version": 1, "chapter": 3, "chapter_sha256": chapter_hash,
            "state_sha256": state_repair.digest(self.state),
            "rejection_sha256": state_repair.digest(self.rejected), "context_fingerprint": self.fp,
            "patch": {"updates": {"resources[0]": {"quantity": 2, "status": "已选择一个片段"}},
                      "append": {"events": [{"event_type": "world", "summary": "韩教练完成初筛",
                                              "evidence_quote": "韩教练完成初筛。"}]}},
        }
        _write_json(self.patch_file, self.context["patch"])
        self.prefix_mock = mock.patch("revision_session.verify_prefix", side_effect=self._verify_prefix).start()
        self.factory_mock = mock.patch("revision_session._make_app", side_effect=self._make_app).start()
        mock.patch("novel_cli.build_doctor_report", return_value={"overall_status": "PASS"}).start()
        mock.patch("state_ledger.load_state", side_effect=lambda _plot: copy.deepcopy(self.state)).start()
        self.addCleanup(mock.patch.stopall)
        self.session = revision_session.RevisionSession.create(
            self.project, self.candidates, checkpoint=self.checkpoint
        )
        self.session.row.update(status="STOPPED", current_chapter=3, error="第3章状态被拒绝")
        self.session._save()
        self._store_rejected()
        self.budget_file = self.project / "runtime/chapter_budgets/chapter_0003.json"
        _write_json(self.budget_file, _budget_row(self.project, epoch="1" * 32, calls=6, repairs=1))

    @staticmethod
    def _body(project, number):
        return Path(project) / "output/第01卷" / f"第{number:04d}章.txt"

    def _verify_prefix(self, project, inputs):
        chapters = {}
        tip = 0
        for number in range(1, 5):
            path = self._body(project, number)
            if not path.exists():
                break
            digest = chapter_commit.chapter_sha256(path.read_text(encoding="utf-8"))
            if digest != inputs[str(number)]["chapter_sha256"]:
                raise RuntimeError("真实前缀正文绑定变化")
            chapters[str(number)] = {"chapter_sha256": digest, "receipt_sha256": str(number) * 64}
            tip = number
        return {"tip": tip, "chapters": chapters}

    def _store_rejected(self):
        formal_suffix_rewrite.store_preflight_stage(
            self.checkpoint, chapter=3, context_fingerprint=self.fp,
            stage="structured_state_rejected", payload=self.rejected
        )

    def _make_app(self, project, budget_project, progress, lock=None):
        self.assertEqual(self.checkpoint.resolve(), Path(project).resolve())
        self.assertEqual(self.project.resolve(), Path(budget_project).resolve())
        owner = self

        class FakeApp:
            project_dir = str(owner.checkpoint)

            def _restore_project_bindings(self):
                pass

            def _begin_chapter_model_budget(self, number):
                owner.budget_begins.append(number)
                if owner.materialize_budget_on_begin and not owner.budget_file.exists():
                    _write_json(owner.budget_file, _budget_row(owner.project, epoch="3" * 32))

            def _extract_chapter_outline(self, number):
                return f"第{number}章受控细纲"

            def _formal_suffix_preflight_fingerprint(self, number, text, outline):
                return owner.fp

            def _load_formal_suffix_preflight_stage(self, number, fp, stage):
                return formal_suffix_rewrite.load_preflight_stage(owner.checkpoint, chapter=number,
                    context_fingerprint=fp, stage=stage, strict=True)

            def _store_formal_suffix_preflight_stage(self, number, fp, stage, payload):
                return formal_suffix_rewrite.store_preflight_stage(owner.checkpoint, chapter=number,
                    context_fingerprint=fp, stage=stage, payload=payload)

            def _formal_suffix_state_repair_arguments(self, number, fp):
                rejected = self._load_formal_suffix_preflight_stage(number, fp, "structured_state_rejected")

                def reject(payload):
                    self._store_formal_suffix_preflight_stage(number, fp, "structured_state_rejected", payload)

                return {"repair_context": rejected, "rejection_callback": reject}

            def _prepare_structured_state_delta(self, number, text, outline="", state_override=None,
                                                 repair_context=None, rejection_callback=None,
                                                 manual_patch_context=None):
                for project in (owner.project, owner.checkpoint):
                    owner.assertTrue((project / project_job_lock.LOCK_FILENAME).is_file())
                state = state_override if state_override is not None else owner.state
                candidate = state_repair.manual_candidate(manual_patch_context, repair_context,
                    chapter=number, chapter_sha256=chapter_commit.chapter_sha256(text), state=state)
                owner.audit_calls.append({"chapter": number, "manual": copy.deepcopy(manual_patch_context),
                                          "rejected": copy.deepcopy(repair_context), "candidate": candidate})
                if owner.fail_service:
                    raise state_ledger.StateExtractionError("独立审计服务未完成，保留原拒绝记录")
                if owner.fail_audit:
                    rejected = copy.deepcopy(repair_context)
                    rejected["previous_raw"] = json.dumps(candidate, ensure_ascii=False)
                    rejected["issues"] = ["resources[0]独立审计仍不支持数量"]
                    rejected["repair_history"].append({"issues": rejected["issues"]})
                    if rejection_callback is not None:
                        rejection_callback(rejected)
                    raise state_ledger.StateExtractionError("单次独立状态审计拒绝")
                return {"chapter": number, "chapter_sha256": "c" * 64 if owner.invalid_prepared else chapter_commit.chapter_sha256(text),
                        "already_committed": False, "delta": candidate}

            def _validate_cached_state_preflight(self, payload, number, text, state):
                if (payload["chapter"] != number or payload["chapter_sha256"] != chapter_commit.chapter_sha256(text)
                        or payload.get("already_committed") or not isinstance(payload.get("delta"), dict)):
                    raise RuntimeError("准备态正文绑定无效")
                return copy.deepcopy(payload)

            def _commit_manual_chapter(self, number, target, text, *args, **kwargs):
                owner.commit_calls.append(number)
                owner._body(owner.checkpoint, number).write_text(text, encoding="utf-8")

            def _get_volume_ranges(self):
                return [(1, 30, "第一卷")]

        return FakeApp()

    def _patch(self, mutate):
        context = copy.deepcopy(self.context)
        mutate(context)
        _write_json(self.patch_file, context["patch"])

    def _assert_no_paid_work(self):
        self.assertEqual([], self.audit_calls)
        self.assertEqual([], self.commit_calls)

    def test_manual_repair_runs_once_stores_exact_cache_and_does_not_commit_or_renew(self):
        before = _tree_hashes(self.project / "output")
        checkpoint_before = _tree_hashes(self.checkpoint / "output")
        budget_before = self.budget_file.read_bytes()
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            result = self.session.repair_state(self.patch_file)
        self.assertNotEqual("STOPPED", result["status"])
        self.assertEqual(2, result["verified_through"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(3, self.audit_calls[0]["chapter"])
        self.assertEqual([], self.commit_calls)
        renew.assert_not_called()
        self.assertEqual(budget_before, self.budget_file.read_bytes())
        self.assertEqual(before, _tree_hashes(self.project / "output"))
        self.assertEqual(checkpoint_before, _tree_hashes(self.checkpoint / "output"))
        prepared = formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state", strict=True)
        self.assertEqual(self.audit_calls[0]["candidate"], prepared["delta"])
        rejected = formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state_rejected", strict=True)
        self.assertEqual(self.rejected, rejected)
        for project in (self.project, self.checkpoint):
            self.assertFalse((project / project_job_lock.LOCK_FILENAME).exists())

    def test_rejection_identity_and_scope_must_match_before_audit(self):
        original = copy.deepcopy(self.rejected)
        for updates in ({"chapter": 2}, {"chapter_sha256": "d" * 64}, {"repair_scope": "prose"}):
            with self.subTest(updates=updates):
                self.rejected = {**copy.deepcopy(original), **updates}
                self._store_rejected()
                result = self.session.repair_state(self.patch_file)
                self.assertEqual("STOPPED", result["status"])
                self._assert_no_paid_work()

    def test_only_tip_plus_one_can_receive_manual_state_repair(self):
        for number in (2, 4):
            with self.subTest(chapter=number):
                self._patch(lambda row: row.update(patch={**self.previous, "chapter": number}))
                result = self.session.repair_state(self.patch_file)
                self.assertEqual("STOPPED", result["status"])
                self.assertIn("不能修改chapter", result["error"])
                self._assert_no_paid_work()

    def test_noop_and_unrejected_path_changes_refuse_before_audit(self):
        for patch in ({"updates": {"resources[0]": {"quantity": 3}}},
                      {"updates": {"characters[0]": {"abilities_add": ["虚构提升"]}}}):
            with self.subTest(patch=patch):
                self._patch(lambda row: row.update(patch=patch))
                result = self.session.repair_state(self.patch_file)
                self.assertEqual("STOPPED", result["status"])
                self._assert_no_paid_work()

    def test_missing_exact_rejection_cannot_use_other_fingerprint(self):
        self.fp = "e" * 64
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self._assert_no_paid_work()

    def test_external_or_non_json_patch_path_refuses_before_audit(self):
        for path in (self.root / "external.json", self.project / "rewrite_staging/patch.txt"):
            with self.subTest(path=path):
                _write_json(path, self.context["patch"])
                result = self.session.repair_state(path)
                self.assertEqual("STOPPED", result["status"])
                self._assert_no_paid_work()

    def test_pending_commit_blocks_state_audit_without_recovery(self):
        _write_json(self.checkpoint / "plot/runtime" / chapter_commit.JOURNAL_FILENAME,
                    {"schema_version": chapter_commit.SCHEMA_VERSION, "chapter": 3, "steps": {}})
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("pending提交", result["error"])
        self._assert_no_paid_work()

    def test_changed_source_or_candidate_refuses_before_audit(self):
        (self.candidates / "第0003章.txt").write_text("第3章 新变化", encoding="utf-8")
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self._assert_no_paid_work()

    def test_changed_source_facts_refuse_before_audit(self):
        (self.project / "plot/比赛与成长台账.md").write_text("正式事实发生变化", encoding="utf-8")
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self._assert_no_paid_work()

    def test_already_prepared_exact_state_blocks_duplicate_paid_audit(self):
        first = self.session.repair_state(self.patch_file)
        self.assertEqual("STATE_PREPARED", first["status"])
        second = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", second["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual([], self.commit_calls)

    def test_audit_rejection_retains_history_prefix_and_never_retries(self):
        self.fail_audit = True
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual([], self.commit_calls)
        self.assertEqual(2, result["verified_through"])
        rejected = formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state_rejected", strict=True)
        self.assertEqual(self.rejected["repair_history"], rejected["repair_history"][:-1])
        self.assertIsNone(formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state", strict=True))

    def test_wrong_prepared_body_binding_cannot_form_approved_cache(self):
        self.invalid_prepared = True
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertIsNone(formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state", strict=True))

    def test_cache_promotion_error_still_releases_both_locks(self):
        with mock.patch("formal_suffix_rewrite.promote_isolated_preflight_cache", side_effect=RuntimeError("提升失败")):
            with self.assertRaisesRegex(RuntimeError, "提升失败"):
                self.session.repair_state(self.patch_file)
        for project in (self.project, self.checkpoint):
            self.assertFalse((project / project_job_lock.LOCK_FILENAME).exists())

    def _failed_distinct_manual_patches(self):
        self.fail_audit = True
        for quantity in (2, 1):
            _write_json(self.patch_file, {"updates": {"resources[0]": {
                "quantity": quantity, "status": f"第{3 - quantity}份人工实改补丁"
            }}})
            result = self.session.repair_state(self.patch_file)
            self.assertEqual("STOPPED", result["status"])
            self.assertIn("单次独立状态审计拒绝", result["error"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(2, len(self.session.row["manual_state_patches"]))
        latest = formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state_rejected", strict=True)
        self.assertEqual(1, json.loads(latest["previous_raw"])["resources"][0]["quantity"])
        self.assertEqual(self.rejected["repair_history"], latest["repair_history"][:1])
        _write_json(self.patch_file, {"updates": {"resources[0]": {
            "quantity": 0, "status": "第三份仍然实改但不能新增入场"
        }}})

    def test_third_changed_patch_same_epoch_is_blocked_before_any_paid_or_budget_work(self):
        budget_before = self.budget_file.read_bytes()
        source_before = _tree_hashes(self.project / "output")
        checkpoint_before = _tree_hashes(self.checkpoint / "output")
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            self._failed_distinct_manual_patches()
            begins = list(self.budget_begins)
            result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("MANUAL_STATE_AUDIT_STOPPED", result["error"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(begins, self.budget_begins)
        self.assertEqual([], self.commit_calls)
        renew.assert_not_called()
        self.assertEqual(budget_before, self.budget_file.read_bytes())
        self.assertEqual(source_before, _tree_hashes(self.project / "output"))
        self.assertEqual(checkpoint_before, _tree_hashes(self.checkpoint / "output"))
        self.assertEqual(["1" * 32] * 2,
                         [row["budget_epoch"] for row in self.session.row["manual_state_patches"]])

    def test_legacy_admissions_without_epoch_still_count_in_current_phase(self):
        self._failed_distinct_manual_patches()
        for row in self.session.row["manual_state_patches"]:
            row.pop("budget_epoch")
        self.session._save()
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("MANUAL_STATE_AUDIT_STOPPED", result["error"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(2, len(self.budget_begins))
        self.assertEqual([], self.commit_calls)

    def test_missing_budget_file_uses_stable_unstarted_epoch_and_cannot_reset_cap(self):
        self.budget_file.unlink()
        self._failed_distinct_manual_patches()
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("MANUAL_STATE_AUDIT_STOPPED", result["error"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(["unstarted"] * 2,
                         [row["budget_epoch"] for row in self.session.row["manual_state_patches"]])
        self.assertFalse(self.budget_file.exists())

    def test_first_budget_creation_does_not_lose_first_manual_admission(self):
        self.budget_file.unlink()
        self.materialize_budget_on_begin = True
        self._failed_distinct_manual_patches()
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("MANUAL_STATE_AUDIT_STOPPED", result["error"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(2, len(self.budget_begins))
        self.assertEqual([], self.commit_calls)

    def test_explicit_new_budget_epoch_can_start_another_phase_without_auto_renewal(self):
        self._failed_distinct_manual_patches()
        _write_json(self.budget_file, _budget_row(self.project, epoch="2" * 32))
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("单次独立状态审计拒绝", result["error"])
        self.assertEqual(3, len(self.audit_calls))
        self.assertEqual("2" * 32, self.session.row["manual_state_patches"][-1]["budget_epoch"])
        self.assertEqual([], self.commit_calls)
        renew.assert_not_called()

    def test_different_chapter_does_not_spend_prior_chapter_admission_count(self):
        self._failed_distinct_manual_patches()
        text3 = (self.candidates / "第0003章.txt").read_text(encoding="utf-8")
        self._body(self.checkpoint, 3).write_text(text3, encoding="utf-8")
        self.state.update(current_chapter=3)
        self.state["applied_chapters"]["3"] = chapter_commit.chapter_sha256(text3)
        _write_json(self.checkpoint / "plot/story_state.json", self.state)
        verified = self._verify_prefix(self.checkpoint, self.session.row["inputs"])
        self.session.row.update(completed_through=3, completed_chapters=verified["chapters"])
        self.session._save()
        rejected4 = copy.deepcopy(self.rejected)
        previous4 = copy.deepcopy(self.previous)
        previous4["chapter"] = 4
        rejected4.update(chapter=4, chapter_sha256=chapter_commit.chapter_sha256(
            (self.candidates / "第0004章.txt").read_text(encoding="utf-8")),
            previous_raw=json.dumps(previous4, ensure_ascii=False))
        formal_suffix_rewrite.store_preflight_stage(self.checkpoint, chapter=4,
            context_fingerprint=self.fp, stage="structured_state_rejected", payload=rejected4)
        _write_json(self.patch_file, {"updates": {"resources[0]": {"quantity": 2}}})
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("单次独立状态审计拒绝", result["error"])
        self.assertEqual(3, len(self.audit_calls))
        self.assertEqual(4, self.audit_calls[-1]["chapter"])
        self.assertEqual(4, self.session.row["manual_state_patches"][-1]["manual_context"]["chapter"])
        self.assertEqual([], self.commit_calls)

    def test_same_identity_after_service_failure_cannot_receive_second_paid_audit(self):
        self.fail_service = True
        first = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", first["status"])
        self.assertEqual(1, len(self.audit_calls))
        second = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", second["status"])
        self.assertIn("本人工补丁已审计或已入场", second["error"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(1, len(self.budget_begins))
        self.assertEqual([], self.commit_calls)

    def test_cli_state_patch_without_execute_is_read_only(self):
        before = _tree_hashes(self.root)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = novel_cli.main(["revision", "--project", str(self.project),
                                   "--state-patch", str(self.patch_file)])
        self.assertEqual(0, code)
        self.assertEqual(before, _tree_hashes(self.root))
        self._assert_no_paid_work()

    def test_cli_state_patch_alone_audits_without_advancing(self):
        with mock.patch.object(revision_session.RevisionSession, "advance") as advance:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main(["revision", "--project", str(self.project), "--execute",
                                       "--state-patch", str(self.patch_file)])
        self.assertEqual(0, code)
        advance.assert_not_called()
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual([], self.commit_calls)

    def test_cli_state_patch_then_through_advances_only_after_pass(self):
        with mock.patch.object(revision_session.RevisionSession, "advance",
                               return_value={"status": "READY"}) as advance:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main(["revision", "--project", str(self.project), "--execute",
                                       "--state-patch", str(self.patch_file), "--through", "3"])
        self.assertEqual(0, code)
        advance.assert_called_once_with(3)
        self.assertEqual(1, len(self.audit_calls))

    def test_cli_rejected_state_patch_does_not_advance(self):
        self.fail_audit = True
        with mock.patch.object(revision_session.RevisionSession, "advance") as advance:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main(["revision", "--project", str(self.project), "--execute",
                                       "--state-patch", str(self.patch_file), "--through", "3"])
        self.assertEqual(2, code)
        advance.assert_not_called()
        self.assertEqual(1, len(self.audit_calls))


class ResourceOrderPatchTests(unittest.TestCase):
    GAIN = "系统打开，获得一百次尝试和三个片段。"
    USE = "周予安选择第一片段，用了十八次尝试。"
    LOSE = "窗口关闭，剩余八十二次和两个片段失效。"

    @classmethod
    def _row(cls, action, quote, *, item="复盘尝试", quantity=None, change=None):
        return {"owner": "周予安", "item": item, "action": action,
                "quantity": quantity, "quantity_change": change,
                "status": "", "evidence_quote": quote}

    def _fixture(self, rows=None, issues=None):
        text = f"第3章 测试\n\n{self.GAIN}\n\n{self.USE}\n\n{self.LOSE}"
        rows = rows if rows is not None else [
            self._row("USE", self.USE, change=18), self._row("LOSE", self.LOSE, change=82),
            self._row("GAIN", self.GAIN, quantity=100, change=100),
        ]
        previous = {"chapter": 3, "resources": copy.deepcopy(rows), "characters": [], "events": []}
        rejected = {"schema_version": 1, "status": "REJECTED", "repair_scope": "state_only",
                    "chapter": 3, "chapter_sha256": chapter_commit.chapter_sha256(text),
                    "previous_raw": json.dumps(previous, ensure_ascii=False),
                    "issues": list(issues or ["events提取遗漏韩初筛"]),
                    "repair_history": [{"issues": ["原真实模型拒绝意见"], "snapshot_sha256": "f" * 64}]}
        patch = {"resource_order_by_evidence": True, "updates": {}, "remove": [], "append": {}}
        return rejected, patch, text

    @staticmethod
    def _candidate(rejected, patch):
        previous = json.loads(rejected["previous_raw"])
        permissions = state_repair.plan(previous, rejected["issues"], expected_chapter=3)
        return state_repair.apply(previous, patch, permissions)

    @staticmethod
    def _replay(candidate):
        return state_ledger._apply_delta_to_state(state_ledger.initial_state(), candidate, "1" * 64)

    def test_explicit_order_fix_preserves_original_evidence_and_replays_100_minus_18_minus_82_to_zero(self):
        rejected, patch, text = self._fixture()
        before_rejected, before_patch = copy.deepcopy(rejected), copy.deepcopy(patch)
        original = json.loads(rejected["previous_raw"])
        original_balance = next(iter(self._replay(original)["resources"].values()))["quantity"]
        self.assertEqual(100, original_balance)
        augmented, transformed, diagnostics = revision_session._resource_order_patch(rejected, patch, text)
        candidate = self._candidate(augmented, transformed)
        self.assertEqual(["GAIN", "USE", "LOSE"], [row["action"] for row in candidate["resources"]])
        self.assertEqual(0, next(iter(self._replay(candidate)["resources"].values()))["quantity"])
        self.assertEqual(original["resources"][2], candidate["resources"][0])
        self.assertEqual(original["resources"][0], candidate["resources"][1])
        self.assertEqual(original["resources"][1], candidate["resources"][2])
        self.assertEqual(before_rejected, rejected)
        self.assertEqual(before_patch, patch)
        self.assertEqual(state_repair.digest(rejected), augmented["model_rejection_sha256"])
        self.assertEqual(rejected["repair_history"], augmented["repair_history"])
        self.assertEqual(rejected["issues"], augmented["issues"][:len(rejected["issues"])])
        self.assertEqual(diagnostics, augmented["local_resource_order_diagnostics"]["issues"])
        self.assertTrue(diagnostics)

    def test_missing_noncontinuous_short_or_ambiguous_quotes_refuse_without_mutating_input(self):
        for quote in ("", "系统打开周予安选择第一片段", "系统", self.GAIN):
            with self.subTest(quote=quote):
                rejected, patch, text = self._fixture()
                previous = json.loads(rejected["previous_raw"])
                previous["resources"][0]["evidence_quote"] = quote
                rejected["previous_raw"] = json.dumps(previous, ensure_ascii=False)
                if quote == self.GAIN:
                    text += "\n" + self.GAIN
                before = copy.deepcopy((rejected, patch))
                with self.assertRaises(ValueError):
                    revision_session._resource_order_patch(rejected, patch, text)
                self.assertEqual(before, (rejected, patch))

    def test_without_explicit_flag_original_resource_order_is_unchanged(self):
        rejected, patch, text = self._fixture()
        patch.pop("resource_order_by_evidence")
        before = copy.deepcopy((rejected, patch))
        augmented, transformed, diagnostics = revision_session._resource_order_patch(rejected, patch, text)
        self.assertEqual([], diagnostics)
        self.assertEqual(before, (augmented, transformed))
        self.assertEqual(["USE", "LOSE", "GAIN"],
                         [row["action"] for row in self._candidate(augmented, transformed)["resources"]])
        self.assertEqual(before, (rejected, patch))

    def test_explicit_order_flag_does_not_grant_permission_for_content_edits(self):
        rejected, patch, text = self._fixture()
        patch["updates"] = {"resources[0]": {"quantity": 999}}
        before = copy.deepcopy((rejected, patch))
        with self.assertRaisesRegex(ValueError, "禁止修改未被拒绝条目"):
            revision_session._resource_order_patch(rejected, patch, text)
        self.assertEqual(before, (rejected, patch))

    def test_already_correct_order_does_not_manufacture_local_diagnostics(self):
        rows = [self._row("GAIN", self.GAIN, quantity=100, change=100),
                self._row("USE", self.USE, change=18), self._row("LOSE", self.LOSE, change=82)]
        rejected, patch, text = self._fixture(rows=rows)
        before = copy.deepcopy((rejected, patch))
        augmented, transformed, diagnostics = revision_session._resource_order_patch(rejected, patch, text)
        self.assertEqual([], diagnostics)
        self.assertEqual(rejected, augmented)
        self.assertNotIn("model_rejection_sha256", augmented)
        self.assertNotIn("local_resource_order_diagnostics", augmented)
        self.assertEqual(rows, self._candidate(augmented, transformed)["resources"])
        self.assertEqual(before, (rejected, patch))

    def test_new_selection_use_is_inserted_between_gain3_and_lose2(self):
        item = "可选复盘片段"
        rows = [self._row("GAIN", self.GAIN, item=item, quantity=3, change=3),
                self._row("LOSE", self.LOSE, item=item, change=2)]
        rejected, patch, text = self._fixture(rows=rows, issues=["resources提取遗漏首次选择消耗一个片段"])
        selected = self._row("USE", self.USE, item=item, change=1)
        patch["append"] = {"resources": [selected]}
        augmented, transformed, diagnostics = revision_session._resource_order_patch(rejected, patch, text)
        candidate = self._candidate(augmented, transformed)
        self.assertEqual([rows[0], selected, rows[1]], candidate["resources"])
        self.assertEqual(0, next(iter(self._replay(candidate)["resources"].values()))["quantity"])
        self.assertTrue(diagnostics)

    def test_two_resources_may_share_one_unique_quote_with_stable_order(self):
        fragments = self._row("GAIN", self.GAIN, item="可选复盘片段", quantity=3, change=3)
        attempts = self._row("GAIN", self.GAIN, quantity=100, change=100)
        rows = [self._row("USE", self.USE, change=18), self._row("LOSE", self.LOSE, change=82),
                fragments, attempts]
        rejected, patch, text = self._fixture(rows=rows)
        self.assertEqual(1, text.count(self.GAIN))
        augmented, transformed, diagnostics = revision_session._resource_order_patch(rejected, patch, text)
        candidate = self._candidate(augmented, transformed)
        self.assertEqual([fragments, attempts], candidate["resources"][:2])
        self.assertEqual([2, 3, 0, 1], augmented["local_resource_order_diagnostics"]["stable_order"])
        self.assertTrue(diagnostics)


if __name__ == "__main__":
    unittest.main()
