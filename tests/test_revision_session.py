"""Offline checkpoint-session tests: never invoke a model or edit a real book."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import chapter_budget
import chapter_commit
import chapter_validator
import narrative_guard
import novel_cli
import project_job_lock
import revision_session
import state_ledger


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RevisionSessionTests(unittest.TestCase):
    """Mock only model/receipt verification, retaining real session persistence."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "source"
        self.checkpoint = self.root / "checkpoint"
        self.candidates = self.project / "rewrite_staging" / "candidates"
        for project in (self.project, self.checkpoint):
            for relative in ("output/第01卷", "plot/runtime", "characters", "world_building"):
                (project / relative).mkdir(parents=True, exist_ok=True)
            _write_json(project / "project_config.json", {"model": "offline-test"})
            (project / "plot" / "比赛与成长台账.md").write_text("事实：替补登场。", encoding="utf-8")
            (project / "plot" / "写作进度.md").write_text("活动进度", encoding="utf-8")
            (project / "characters" / "主角.txt").write_text("角色设定", encoding="utf-8")
            (project / "world_building" / "规则.txt").write_text("系统规则", encoding="utf-8")
        self.candidates.mkdir(parents=True)
        for number in range(1, 4):
            self._chapter_path(self.project, number).write_text(
                f"第{number}章 旧正式正文", encoding="utf-8"
            )
            (self.candidates / f"第{number:04d}章.txt").write_text(
                f"第{number}章 新候选正文", encoding="utf-8"
            )
        self._record_committed(1)
        self.calls = []
        self.recoveries = []
        self.fail_at = None
        self.interrupt_after_commit = None
        self.doctor = mock.patch("novel_cli.build_doctor_report", return_value={
            "overall_status": "PASS", "formal": {"latest_chapter": 3},
            "project": {"latest_chapter": 3},
        }).start()
        self.addCleanup(mock.patch.stopall)
        self.prefix = mock.patch("revision_session.verify_prefix", side_effect=self._verify_prefix).start()
        self.factory = mock.patch("revision_session._make_app", side_effect=self._make_app).start()

    @staticmethod
    def _chapter_path(project: Path, number: int) -> Path:
        return project / "output" / "第01卷" / f"第{number:04d}章.txt"

    @staticmethod
    def _receipt_path(project: Path, number: int) -> Path:
        return project / "plot" / "runtime" / "offline_verified_receipts" / f"{number}.json"

    def _record_committed(self, number: int):
        text = (self.candidates / f"第{number:04d}章.txt").read_text(encoding="utf-8")
        path = self._chapter_path(self.checkpoint, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _write_json(self._receipt_path(self.checkpoint, number), {
            "chapter": number,
            "chapter_sha256": chapter_commit.chapter_sha256(text),
            "offline_verified": True,
        })

    def _verify_prefix(self, project, inputs):
        """A receipt-backed stand-in; cursor metadata alone is never evidence."""
        project = Path(project)
        chapters = {}
        tip = 0
        for number in sorted(int(key) for key in inputs):
            target = self._chapter_path(project, number)
            if not target.exists():
                break
            receipt = self._receipt_path(project, number)
            if not receipt.exists():
                raise RuntimeError(f"第{number}章缺少真实提交收据")
            row = json.loads(receipt.read_text(encoding="utf-8"))
            digest = chapter_commit.chapter_sha256(target.read_text(encoding="utf-8"))
            if not row.get("offline_verified") or row.get("chapter_sha256") != digest:
                raise RuntimeError(f"第{number}章提交收据无效")
            input_row = inputs.get(str(number), inputs.get(number))
            candidate = Path(input_row["path"] if isinstance(input_row, dict) else input_row)
            if candidate.read_text(encoding="utf-8").strip() != target.read_text(encoding="utf-8").strip():
                raise RuntimeError(f"第{number}章已提交正文不是本次输入")
            chapters[str(number)] = {
                "chapter_sha256": digest,
                "receipt_sha256": revision_session.sha256(receipt),
            }
            tip = number
        return {"tip": tip, "chapters": chapters}

    def _make_app(self, project, budget_project, progress, lock=None):
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
                owner.assertEqual(owner._chapter_path(owner.checkpoint, number).resolve(), Path(target).resolve())
                if owner.fail_at == number:
                    raise RuntimeError(f"第{number}章明确门禁失败")
                owner._record_committed(number)
                if owner.interrupt_after_commit == number:
                    raise KeyboardInterrupt("提交已落盘，metadata尚未更新")

            def _recover_pending_chapter_commit(self):
                pending_path = owner.checkpoint / "plot" / "runtime" / chapter_commit.JOURNAL_FILENAME
                payload = json.loads(pending_path.read_text(encoding="utf-8"))
                number = payload["chapter"]
                owner.recoveries.append(number)
                owner._record_committed(number)
                pending_path.unlink()

        return FakeApp()

    def _create(self):
        return revision_session.RevisionSession.create(
            self.project, self.candidates, checkpoint=self.checkpoint
        )

    def _pending(self, *, number=2, path=None, digest=None):
        text = (self.candidates / f"第{number:04d}章.txt").read_text(encoding="utf-8")
        _write_json(self.checkpoint / "plot/runtime" / chapter_commit.JOURNAL_FILENAME, {
            "schema_version": chapter_commit.SCHEMA_VERSION,
            "chapter": number,
            "chapter_sha256": digest or chapter_commit.chapter_sha256(text),
            "chapter_path": str(path or self._chapter_path(self.checkpoint, number)),
            "plot_dir": str(self.checkpoint / "plot"),
            "steps": {},
        })

    def test_imports_existing_verified_chapter_without_model_or_commit(self):
        source_before = _tree_hashes(self.project / "output")
        session = self._create()
        summary = session.summary()
        self.assertEqual(1, summary["verified_through"])
        self.assertEqual(2, summary["next_chapter"])
        self.assertEqual(3, summary["formal_end"])
        self.assertEqual(self.project / "runtime/revision_sessions/current.json", Path(session.path))
        self.assertEqual([], self.calls)
        self.factory.assert_not_called()
        self.assertEqual(source_before, _tree_hashes(self.project / "output"))

    def test_rejects_source_equal_ancestor_or_nested_checkpoint_before_import(self):
        for checkpoint in (self.project, self.root, self.project / "plot/nested-checkpoint"):
            with self.subTest(checkpoint=checkpoint):
                before = _tree_hashes(self.root)
                self.prefix.reset_mock()
                with self.assertRaises(RuntimeError):
                    revision_session.RevisionSession.create(
                        self.project, self.candidates, checkpoint=checkpoint
                    )
                self.prefix.assert_not_called()
                self.factory.assert_not_called()
                self.assertFalse((self.project / "runtime/revision_sessions/current.json").exists())
                self.assertEqual(before, _tree_hashes(self.root))

    def test_failure_keeps_prior_success_and_stops_at_exact_failed_chapter(self):
        session = self._create()
        self.fail_at = 3
        result = session.advance(3)
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(2, result["verified_through"])
        self.assertEqual(3, result["next_chapter"])
        self.assertIn("第3章", result["error"])
        self.assertEqual([2, 3], self.calls)
        self.assertTrue(self._receipt_path(self.checkpoint, 2).exists())
        self.assertFalse(self._chapter_path(self.checkpoint, 3).exists())

    def test_restart_does_not_recommit_verified_prefix(self):
        session = self._create()
        self.fail_at = 3
        session.advance(3)
        self.calls.clear()
        self.fail_at = None
        reopened = revision_session.RevisionSession.open(self.project)
        result = reopened.advance(3)
        self.assertEqual(3, result["verified_through"])
        self.assertEqual([3], self.calls)

    def test_receipt_reconciles_interrupt_between_commit_and_metadata_write(self):
        session = self._create()
        self.interrupt_after_commit = 2
        with self.assertRaises(KeyboardInterrupt):
            session.advance(2)
        self.assertTrue(self._receipt_path(self.checkpoint, 2).exists())
        self.interrupt_after_commit = None
        self.calls.clear()
        reopened = revision_session.RevisionSession.open(self.project)
        result = reopened.advance(2)
        self.assertEqual(2, result["verified_through"])
        self.assertEqual([], self.calls)

    def test_metadata_cursor_cannot_claim_unsubmitted_chapters(self):
        session = self._create()
        row = revision_session.read_json(session.path)
        row["completed_through"] = 3
        _write_json(Path(session.path), row)
        reopened = revision_session.RevisionSession.open(self.project)
        result = reopened.advance(3)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("断点计数", result["error"])
        self.assertEqual([], self.calls)

    def test_changed_candidate_refuses_before_creating_model_app(self):
        session = self._create()
        (self.candidates / "第0002章.txt").write_text("已改的候选正文", encoding="utf-8")
        result = session.advance(2)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("输入变化", result["error"])
        self.factory.assert_not_called()

    def test_changed_fact_ledger_refuses_before_creating_model_app(self):
        session = self._create()
        (self.project / "plot/比赛与成长台账.md").write_text("事实：首发登场。", encoding="utf-8")
        result = session.advance(2)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("事实资产变化", result["error"])
        self.factory.assert_not_called()

    def test_source_manifest_ignores_only_progress_not_fact_documents(self):
        before = revision_session.source_manifest(self.project)
        (self.project / "plot/写作进度.md").write_text("本轮活动进度更新", encoding="utf-8")
        self.assertEqual(before, revision_session.source_manifest(self.project))
        (self.project / "plot/比赛与成长台账.md").write_text("台账事实变化", encoding="utf-8")
        self.assertNotEqual(before, revision_session.source_manifest(self.project))

    def test_changed_progress_is_allowed_and_does_not_restart_prefix(self):
        session = self._create()
        (self.project / "plot/写作进度.md").write_text("已完成第1断点", encoding="utf-8")
        result = session.advance(2)
        self.assertEqual(2, result["verified_through"])
        self.assertEqual([2], self.calls)

    def test_does_not_renew_budget_without_explicit_authorization(self):
        session = self._create()
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            session.advance(2)
        renew.assert_not_called()

    def test_cache_promotion_failure_always_releases_both_real_project_locks(self):
        session = self._create()
        with mock.patch("formal_suffix_rewrite.promote_isolated_preflight_cache",
                        side_effect=RuntimeError("缓存提升磁盘失败")):
            with self.assertRaisesRegex(RuntimeError, "缓存提升"):
                session.advance(2)
        for project in (self.project, self.checkpoint):
            with self.subTest(project=project):
                self.assertFalse((project / project_job_lock.LOCK_FILENAME).exists())
                lock = project_job_lock.ProjectJobLock(project)
                try:
                    acquired, reason = lock.acquire("离线验证异常后锁已释放")
                    self.assertTrue(acquired, reason)
                finally:
                    lock.release()

    def test_summary_is_read_only(self):
        session = self._create()
        before = _tree_hashes(self.root)
        session.summary()
        session.summary()
        self.assertEqual(before, _tree_hashes(self.root))
        self.factory.assert_not_called()

    def test_external_pending_target_is_rejected_without_recovery(self):
        session = self._create()
        external = self.root / "external/第0002章.txt"
        self._pending(path=external)
        result = session.advance(2)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("路径不属于", result["error"])
        self.assertEqual([], self.calls)
        self.assertEqual([], self.recoveries)
        self.assertFalse(external.exists())

    def test_pending_different_prose_is_rejected_without_recovery(self):
        session = self._create()
        self._pending(digest="a" * 64)
        result = session.advance(2)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("正文或目标路径", result["error"])
        self.assertEqual([], self.calls)
        self.assertEqual([], self.recoveries)

    def test_external_pending_plot_is_rejected_without_recovery(self):
        session = self._create()
        self._pending()
        pending_path = self.checkpoint / "plot/runtime" / chapter_commit.JOURNAL_FILENAME
        row = revision_session.read_json(pending_path)
        row["plot_dir"] = str(self.root / "external-plot")
        _write_json(pending_path, row)
        result = session.advance(2)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("待恢复", result["error"])
        self.assertEqual([], self.calls)
        self.assertEqual([], self.recoveries)

    def test_matching_pending_recovers_without_regeneration(self):
        session = self._create()
        self._pending()
        result = session.advance(2)
        self.assertEqual(2, result["verified_through"])
        self.assertEqual([2], self.recoveries)
        self.assertEqual([], self.calls)

    def test_missing_committed_receipt_cannot_be_skipped_on_restart(self):
        session = self._create()
        session.advance(2)
        self._receipt_path(self.checkpoint, 2).unlink()
        self.calls.clear()
        result = revision_session.RevisionSession.open(self.project).advance(3)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("收据", result["error"])
        self.assertEqual([], self.calls)

    def test_forged_committed_receipt_cannot_be_skipped_on_restart(self):
        session = self._create()
        session.advance(2)
        _write_json(self._receipt_path(self.checkpoint, 2), {
            "chapter": 2, "chapter_sha256": "b" * 64, "offline_verified": True,
        })
        self.calls.clear()
        result = revision_session.RevisionSession.open(self.project).advance(3)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("收据", result["error"])
        self.assertEqual([], self.calls)

    def test_cli_without_execute_does_not_create_or_advance_session(self):
        before = _tree_hashes(self.root)
        with mock.patch.object(revision_session.RevisionSession, "create") as create:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main([
                    "revision", "--project", str(self.project),
                    "--candidates-dir", str(self.candidates),
                    "--checkpoint", str(self.checkpoint), "--through", "3",
                ])
        self.assertEqual(0, code)
        create.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(before, _tree_hashes(self.root))

    def test_cli_first_execute_without_candidates_refuses_without_metadata(self):
        before = _tree_hashes(self.root)
        with mock.patch.object(revision_session.RevisionSession, "create") as create:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                code = novel_cli.main([
                    "revision", "--project", str(self.project), "--execute",
                    "--checkpoint", str(self.checkpoint), "--through", "2",
                ])
        self.assertEqual(2, code)
        self.assertIn("--candidates-dir", errors.getvalue())
        create.assert_not_called()
        self.factory.assert_not_called()
        self.assertFalse((self.project / "runtime/revision_sessions/current.json").exists())
        self.assertEqual(before, _tree_hashes(self.root))

    def test_cli_existing_session_default_reads_summary_without_advancing(self):
        self._create()
        before = _tree_hashes(self.root)
        with mock.patch.object(revision_session.RevisionSession, "advance") as advance:
            with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
                code = novel_cli.main(["revision", "--project", str(self.project), "--through", "3"])
        self.assertEqual(0, code)
        self.assertIn("verified_through", output.getvalue())
        advance.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(before, _tree_hashes(self.root))


class RevisionPrefixEvidenceTests(unittest.TestCase):
    """Real model-free verifiers with synthetic test-only audit receipts.

    These fixtures deliberately create no network/model invocation. They test
    checksum, persistence, state-chain, and gate integrity, not literary merit.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "checkpoint"
        self.plot = self.project / "plot"
        self.plot.mkdir(parents=True)
        self.text = "第1章 旧站\n\n林舟在旧站醒来，确认门锁完好，随后把现场编号写进记录簿。"
        self.body = self.project / "output/第01卷/第0001章.txt"
        self.body.parent.mkdir(parents=True)
        self.body.write_text(self.text, encoding="utf-8")
        _write_json(self.project / "project_config.json", {
            "release_mode": "strict", "review_model": "offline-evidence-test",
            "narrative_guard_min_score": 78, "chapter_char_min": 10,
            "chapter_char_target_max": 120,
        })
        _write_json(self.plot / "semantic_invariants.json", {
            "identity_bindings": [{"id": "test_identity", "entity": "林舟身份",
                                   "claim_patterns": ["林舟身份[：:](?P<value>主角|反派)"],
                                   "allowed_values": ["主角"]}], "exclusive_fact_groups": [],
            "forbidden_patterns": [], "numeric_rules": [],
        })
        limits = {"min": 10, "max": 120}
        validation = chapter_validator.validate_candidate(self.project, 1, self.text, limits)
        self.assertTrue(validation["passed"], validation)
        delta, issues = state_ledger.validate_delta({
            "chapter": 1, "events": [{"event_type": "world", "summary": "林舟醒来记录现场",
                                      "evidence_quote": "林舟在旧站醒来"}],
        }, 1, self.text, state_ledger.initial_state())
        self.assertEqual([], issues)
        state_ledger.commit_validated_delta(self.plot, delta, self.text)
        decision = {
            "verdict": "PASS", "scores": {field: 90 for field in narrative_guard.SCORE_FIELDS},
            "outline_requirements": [], "outline_coverage": [],
            "mainline_progress": "林舟醒来记录现场", "progress_evidence_quote": "林舟在旧站醒来",
            "drift_flags": [], "nonsense_flags": [], "fail_reasons": [], "summary": "测试证据",
        }
        raw = json.dumps(decision, ensure_ascii=False)
        audit, issues = narrative_guard.validate_audit(decision, chapter_text=self.text, requirements=[])
        self.assertEqual([], issues)
        audit.update(chapter=1, chapter_sha256=chapter_commit.chapter_sha256(self.text),
                     review_model="offline-evidence-test", review_raw_response=raw)
        audit["review_receipt"] = narrative_guard.build_review_receipt(
            chapter_text=self.text, system_prompt="离线测试", user_prompt="离线测试第1章",
            raw_response=raw, review_model="offline-evidence-test", decision_payload=audit,
        )
        persisted = narrative_guard.persist_audit(self.plot, 1, self.text, audit,
                                                  model_name="offline-evidence-test", min_score=78)
        self.audit_path = Path(persisted)
        persisted_audit = revision_session.read_json(self.audit_path)
        chapter_commit.begin(self.plot, chapter=1, chapter_path=str(self.body), chapter_text=self.text,
                             prepared_state_delta=delta, narrative_audit_result=audit,
                             validation_receipt=chapter_validator.build_validation_receipt(validation),
                             char_limits=limits, chapter_status="正式可用")
        for step in revision_session.STEPS:
            chapter_commit.mark_step(self.plot, step, audit_id=persisted_audit["audit_id"])
        self.receipt = Path(chapter_commit.finalize(self.plot))
        self.receipt_row = revision_session.read_json(self.receipt)
        self.inputs = {"1": {"path": str(self.body), "sha256": revision_session.sha256(self.body),
                             "chapter_sha256": chapter_commit.chapter_sha256(self.text)}}

    def test_full_real_verifier_accepts_exact_complete_test_evidence_without_writes(self):
        before = _tree_hashes(self.project)
        result = revision_session.verify_prefix(self.project, self.inputs)
        self.assertEqual(1, result["tip"])
        self.assertEqual(revision_session.sha256(self.receipt), result["chapters"]["1"]["receipt_sha256"])
        self.assertEqual(before, _tree_hashes(self.project))

    def test_explicit_old_model_binding_keeps_exact_receipt_not_new_model(self):
        config_path = self.project / "project_config.json"
        config = revision_session.read_json(config_path)
        config.update(review_model="deepseek-v4-pro", revision_prefix_review_bindings={"1": {
            "review_model": "offline-evidence-test", "chapter_sha256": self.inputs["1"]["chapter_sha256"],
            "receipt_sha256": revision_session.sha256(self.receipt)}})
        _write_json(config_path, config)
        before = _tree_hashes(self.project)
        self.assertEqual(1, revision_session.verify_prefix(self.project, self.inputs)["tip"])
        self.assertEqual(before, _tree_hashes(self.project))
        config["revision_prefix_review_bindings"]["1"]["review_model"] = "deepseek-v4-pro"
        _write_json(config_path, config)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)

    def test_binding_cannot_cover_future_chapter_or_wrong_receipt(self):
        for key, receipt in [("2", revision_session.sha256(self.receipt)), ("1", "0" * 64)]:
            with self.subTest(key=key):
                config_path = self.project / "project_config.json"
                config = revision_session.read_json(config_path)
                config["revision_prefix_review_bindings"] = {key: {
                    "review_model": "offline-evidence-test", "chapter_sha256": self.inputs["1"]["chapter_sha256"],
                    "receipt_sha256": receipt}}
                _write_json(config_path, config)
                with self.assertRaises(RuntimeError):
                    revision_session.verify_prefix(self.project, self.inputs)

    def _provider_session(self):
        source = self.project.parent / "original"
        source.mkdir()
        _write_json(source / "project_config.json", {"model_provider": "codex_sol", "review_model": "offline-evidence-test"})
        config_path = self.project / "project_config.json"
        config = revision_session.read_json(config_path)
        config["model_provider"] = "codex_sol"
        _write_json(config_path, config)
        proof = revision_session.verify_prefix(self.project, self.inputs)
        row = {"schema_version": 1, "source_project": str(source), "checkpoint_project": str(self.project),
               "inputs": self.inputs, "formal_end": 2, "source_manifest": revision_session.source_manifest(source),
               "bundle": {}, "checkpoint_config_sha256": revision_session.sha256(config_path),
               "completed_through": 1, "completed_chapters": proof["chapters"], "status": "STOPPED",
               "phase": "isolated_preflight", "current_chapter": 2, "error": "CHAPTER_BUDGET_STOPPED：old",
               "formal_adoption": "NOT_ATTEMPTED", "next_chapter_commit": "NOT_ATTEMPTED", "events": []}
        session = revision_session.RevisionSession(source, source / "runtime/revision_sessions/current.json", row)
        session._save()
        return session

    def test_provider_transition_preserves_source_budget_stop_and_real_old_receipt(self):
        session = self._provider_session()
        budget = chapter_budget.ChapterBudget(session.project, 2, calls=6)
        budget.transact("call")
        before_budget = _tree_hashes(session.project / "runtime/chapter_budgets")
        old_source = (session.project / "project_config.json").read_bytes()
        old_receipt = self.receipt.read_bytes()
        result = session.configure_isolated_provider("deepseek", reason="explicit test authorization")
        self.assertEqual("ISOLATED_PROVIDER_CONFIGURED", result["operation_status"])
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(old_source, (session.project / "project_config.json").read_bytes())
        self.assertEqual(old_receipt, self.receipt.read_bytes())
        self.assertEqual(before_budget, _tree_hashes(session.project / "runtime/chapter_budgets"))
        self.assertEqual(1, revision_session.verify_prefix(self.project, self.inputs)["tip"])
        self.assertEqual("deepseek-v4-pro", revision_session.read_json(self.project / "project_config.json")["review_model"])

    def test_provider_transition_rollback_restores_exact_config_and_session(self):
        session = self._provider_session()
        before_config = (self.project / "project_config.json").read_bytes()
        before_session = revision_session.read_json(session.path)
        with mock.patch.object(session, "_event", side_effect=RuntimeError("observer failed")):
            with self.assertRaisesRegex(RuntimeError, "observer failed"):
                session.configure_isolated_provider("deepseek", reason="explicit test authorization")
        self.assertEqual(before_config, (self.project / "project_config.json").read_bytes())
        self.assertEqual(before_session, revision_session.read_json(session.path))

    def test_provider_transition_requires_reason_and_rejects_replay(self):
        session = self._provider_session()
        with self.assertRaises(ValueError):
            session.configure_isolated_provider("deepseek", reason="")
        session.configure_isolated_provider("deepseek", reason="explicit test authorization")
        with self.assertRaisesRegex(RuntimeError, "已相同"):
            session.configure_isolated_provider("deepseek", reason="repeat")

    def test_full_real_verifier_rejects_missing_receipt_without_writes(self):
        self.receipt.unlink()
        before = _tree_hashes(self.project)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)
        self.assertEqual(before, _tree_hashes(self.project))

    def test_full_real_verifier_rejects_each_unfinished_commit_step(self):
        for step in revision_session.STEPS:
            with self.subTest(step=step):
                row = json.loads(json.dumps(self.receipt_row))
                row["steps"][step] = False
                _write_json(self.receipt, row)
                with self.assertRaises(RuntimeError):
                    revision_session.verify_prefix(self.project, self.inputs)

    def test_full_real_verifier_rejects_legacy_commit_schema(self):
        row = json.loads(json.dumps(self.receipt_row))
        row["schema_version"] = 2
        _write_json(self.receipt, row)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)

    def test_full_real_verifier_rejects_forged_validation_receipt(self):
        row = json.loads(json.dumps(self.receipt_row))
        row["validation_receipt"]["chinese_chars"] += 1
        _write_json(self.receipt, row)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)

    def test_full_real_verifier_rejects_changed_model_raw_response(self):
        row = json.loads(json.dumps(self.receipt_row))
        row["narrative_audit_result"]["review_raw_response"] += "已改"
        _write_json(self.receipt, row)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)

    def test_full_real_verifier_requires_persisted_narrative_audit(self):
        self.audit_path.unlink()
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)

    def test_corrupt_materialized_state_is_not_silently_repaired(self):
        state_path = self.plot / "story_state.json"
        row = revision_session.read_json(state_path)
        row["resources"] = {"invented": {"quantity": 8}}
        _write_json(state_path, row)
        before = _tree_hashes(self.project)
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)
        self.assertEqual(before, _tree_hashes(self.project))

    def test_missing_materialized_state_is_not_silently_rebuilt(self):
        (self.plot / "story_state.json").unlink()
        before = _tree_hashes(self.project)
        with self.assertRaises((RuntimeError, OSError)):
            revision_session.verify_prefix(self.project, self.inputs)
        self.assertEqual(before, _tree_hashes(self.project))

    def test_full_real_verifier_rejects_input_of_different_prose(self):
        self.inputs["1"]["chapter_sha256"] = "c" * 64
        with self.assertRaises(RuntimeError):
            revision_session.verify_prefix(self.project, self.inputs)


if __name__ == "__main__":
    unittest.main()
