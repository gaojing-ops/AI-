"""Explicit one-time service recovery, never a semantic-review retry loop."""

from __future__ import annotations

import copy
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

import chapter_budget
import formal_suffix_rewrite
import novel_cli
import revision_session
import state_ledger
import test_revision_state_repair as fixtures


class RevisionStateResumeTests(unittest.TestCase):
    NOW = 1_800_000_000.0
    SERVICE_ERROR = ('独立证据审计调用未完成：'
                     '{"exception_type":"CodexSolTimeoutError","failure_hint":"connection"}')
    _body = staticmethod(fixtures.RevisionStateRepairTests._body)
    _verify_prefix = fixtures.RevisionStateRepairTests._verify_prefix
    _store_rejected = fixtures.RevisionStateRepairTests._store_rejected

    def setUp(self):
        fixtures.RevisionStateRepairTests.setUp(self)
        mock.patch("chapter_budget.time.time", return_value=self.NOW).start()
        self._set_epoch("1" * 32)

    def _make_app(self, project, budget_project, progress, lock=None):
        app = fixtures.RevisionStateRepairTests._make_app(self, project, budget_project, progress, lock)
        original = app._prepare_structured_state_delta

        def prepare(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except state_ledger.StateExtractionError:
                if self.fail_service:
                    raise state_ledger.StateExtractionError(self.SERVICE_ERROR) from None
                raise

        app._prepare_structured_state_delta = prepare
        return app

    def _set_epoch(self, epoch, *, remaining=1199.0):
        budget = chapter_budget.ChapterBudget(self.project, 3)
        row = budget._new()
        row.update(epoch=epoch, calls=6, repairs=1, started_at=self.NOW - (1200.0 - remaining),
                   last_seen=self.NOW, stop_reason="")
        fixtures._write_json(self.budget_file, row)

    def _unavailable(self):
        self.fail_service = True
        before = copy.deepcopy(self.rejected)
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("独立证据审计调用未完成", result["error"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual([], self.commit_calls)
        cached = formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state_rejected", strict=True)
        self.assertEqual(before, cached)
        self.assertEqual("UNAVAILABLE", self.session.row["manual_state_patches"][0]["outcome"])
        self.fail_service = False
        return copy.deepcopy(self.session.row["manual_state_patches"][0])

    def _resume(self, reason="显式恢复已证实未完成的服务调用"):
        return self.session.repair_state(self.patch_file, resume_unavailable=True, reason=reason)

    def test_unavailable_is_recorded_without_semantic_rejection_or_prepared_state(self):
        admission = self._unavailable()
        self.assertEqual("1" * 32, admission["budget_epoch"])
        self.assertEqual(self.rejected, admission["original_rejection"])
        self.assertIsNone(formal_suffix_rewrite.load_preflight_stage(self.checkpoint, chapter=3,
            context_fingerprint=self.fp, stage="structured_state", strict=True))

    def test_same_identity_default_still_refuses_even_with_a_new_epoch(self):
        self._unavailable()
        self._set_epoch("2" * 32)
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(1, len(self.session.row["manual_state_patches"]))

    def test_explicit_resume_requires_nonempty_reason(self):
        self._unavailable()
        self._set_epoch("2" * 32)
        result = self._resume(reason="   ")
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(1, len(self.session.row["manual_state_patches"]))

    def test_explicit_resume_cannot_reuse_same_budget_epoch(self):
        self._unavailable()
        result = self._resume()
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(1, len(self.budget_begins))

    def test_one_explicit_resume_uses_new_epoch_without_rewriting_old_context_or_renewing(self):
        original = self._unavailable()
        self._set_epoch("2" * 32)
        budget_before = self.budget_file.read_bytes()
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            result = self._resume()
        self.assertEqual("STATE_PREPARED", result["status"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual([], self.commit_calls)
        renew.assert_not_called()
        self.assertEqual(budget_before, self.budget_file.read_bytes())
        records = self.session.row["manual_state_patches"]
        self.assertEqual(2, len(records))
        self.assertEqual(original, records[0])
        self.assertEqual(original["identity"], records[1]["resumed_from"])
        self.assertEqual("2" * 32, records[1]["budget_epoch"])
        self.assertEqual(original["manual_context"], records[1]["manual_context"])

    def test_semantic_rejection_is_not_resumable_as_service_unavailable(self):
        self.fail_audit = True
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual("REJECTED", self.session.row["manual_state_patches"][0]["outcome"])
        self.assertFalse(revision_session._unavailable_admission(
            self.session.row, self.session.row["manual_state_patches"][0]))
        self.fail_audit = False
        self._set_epoch("2" * 32)
        # Keep permissions/substance valid for the CURRENT semantic rejection;
        # failure must be service-recovery policy, not an out-of-scope patch.
        fixtures._write_json(self.patch_file, {"updates": {"resources[0]": {"quantity": 1}}})
        result = self._resume()
        self.assertEqual("STOPPED", result["status"])
        self.assertEqual(1, len(self.audit_calls))
        self.assertEqual(1, len(self.session.row["manual_state_patches"]))

    def test_each_original_identity_allows_only_one_explicit_resume_even_if_service_fails_again(self):
        self._unavailable()
        self._set_epoch("2" * 32)
        self.fail_service = True
        first = self._resume()
        self.assertEqual("STOPPED", first["status"])
        self.assertEqual(2, len(self.audit_calls))
        self._set_epoch("3" * 32)
        self.fail_service = False
        second = self._resume()
        self.assertEqual("STOPPED", second["status"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(2, len(self.session.row["manual_state_patches"]))

    def test_less_than_120_seconds_refuses_before_paid_work_or_admission_without_budget_write(self):
        self._set_epoch("1" * 32, remaining=119.0)
        before = self.budget_file.read_bytes()
        with mock.patch.object(chapter_budget.ChapterBudget, "renew") as renew:
            result = self.session.repair_state(self.patch_file)
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("120", result["error"])
        self.assertEqual([], self.audit_calls)
        self.assertEqual([], self.budget_begins)
        self.assertEqual([], self.commit_calls)
        self.assertEqual([], self.session.row.get("manual_state_patches", []))
        self.assertEqual(before, self.budget_file.read_bytes())
        renew.assert_not_called()

    def test_exact_120_second_window_can_admit_one_audit(self):
        self._set_epoch("1" * 32, remaining=120.0)
        result = self.session.repair_state(self.patch_file)
        self.assertEqual("STATE_PREPARED", result["status"])
        self.assertEqual(1, len(self.audit_calls))

    def test_legacy_unmarked_unavailable_can_be_inferred_only_from_matching_uncompleted_events(self):
        original = self._unavailable()
        self.session.row["manual_state_patches"][0].pop("outcome")
        self.session._save()
        self._set_epoch("2" * 32)
        result = self._resume()
        self.assertEqual("STATE_PREPARED", result["status"])
        self.assertEqual(2, len(self.audit_calls))
        self.assertEqual(original["identity"], self.session.row["manual_state_patches"][-1]["resumed_from"])

    def test_legacy_ambiguous_other_chapter_or_completed_events_cannot_infer_unavailable(self):
        self._unavailable()
        record = copy.deepcopy(self.session.row["manual_state_patches"][0])
        record.pop("outcome")
        started = {"event": "manual_state_patch_audit_started", "chapter": 3, "identity": record["identity"]}
        stopped = {"event": "manual_state_patch_stopped", "chapter": 3, "error": self.SERVICE_ERROR}
        for events in ([started, {**stopped, "chapter": 4}],
                       [started, {"event": "manual_state_patch_audit_passed", "chapter": 3,
                                  "identity": record["identity"]}, stopped],
                       [started, {"event": "state_model_call_completed", "chapter": 3}, stopped]):
            with self.subTest(events=events):
                self.session.row["manual_state_patches"] = [copy.deepcopy(record)]
                self.session.row["events"] = copy.deepcopy(events)
                self.session._save()
                self._set_epoch("2" * 32)
                result = self._resume()
                self.assertEqual("STOPPED", result["status"])
                self.assertEqual(1, len(self.audit_calls))

    def test_resume_refuses_changed_body_state_rejection_or_fingerprint(self):
        self._unavailable()
        self._set_epoch("2" * 32)
        original_state = copy.deepcopy(self.state)
        for scenario in ("state", "rejection", "fingerprint", "body"):
            with self.subTest(scenario=scenario):
                self.fp = "a" * 64
                fixtures._write_json(self.checkpoint / "plot/story_state.json", original_state)
                self._store_rejected()
                if scenario == "state":
                    changed = copy.deepcopy(original_state)
                    changed["resources"]["test_change"] = {"quantity": 9}
                    fixtures._write_json(self.checkpoint / "plot/story_state.json", changed)
                elif scenario == "rejection":
                    changed = copy.deepcopy(self.rejected)
                    changed["repair_history"].append({"issues": ["不同拒绝上下文"]})
                    formal_suffix_rewrite.store_preflight_stage(self.checkpoint, chapter=3,
                        context_fingerprint=self.fp, stage="structured_state_rejected", payload=changed)
                elif scenario == "fingerprint":
                    self.fp = "f" * 64
                else:
                    (self.candidates / "第0003章.txt").write_text("第3章 已改正文", encoding="utf-8")
                result = self._resume()
                self.assertEqual("STOPPED", result["status"])
                self.assertEqual(1, len(self.audit_calls))

    def test_resume_still_obeys_two_admission_current_epoch_limit(self):
        original = self._unavailable()
        self._set_epoch("2" * 32)
        for identity in ("b" * 64, "c" * 64):
            current = copy.deepcopy(original)
            current.update(identity=identity, budget_epoch="2" * 32, outcome="REJECTED")
            self.session.row["manual_state_patches"].append(current)
        self.session._save()
        result = self._resume()
        self.assertEqual("STOPPED", result["status"])
        self.assertIn("MANUAL_STATE_AUDIT_STOPPED", result["error"])
        self.assertEqual(1, len(self.audit_calls))

    def test_cli_resume_flags_without_execute_are_strictly_read_only(self):
        before = fixtures._tree_hashes(self.root)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = novel_cli.main(["revision", "--project", str(self.project), "--state-patch",
                                   str(self.patch_file), "--resume-state-audit", "--reason", "显式恢复"])
        self.assertEqual(0, code)
        self.assertEqual(before, fixtures._tree_hashes(self.root))
        self.assertEqual([], self.audit_calls)

    def test_cli_explicit_resume_then_through_advances_only_after_single_pass(self):
        self._unavailable()
        self._set_epoch("2" * 32)
        with mock.patch.object(revision_session.RevisionSession, "advance",
                               return_value={"status": "READY"}) as advance:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = novel_cli.main(["revision", "--project", str(self.project), "--execute",
                                       "--state-patch", str(self.patch_file), "--resume-state-audit",
                                       "--reason", "换明示预算后恢复原未完成服务", "--through", "3"])
        self.assertEqual(0, code)
        self.assertEqual(2, len(self.audit_calls))
        advance.assert_called_once_with(3)
        self.assertEqual([], self.commit_calls)


if __name__ == "__main__":
    unittest.main()
