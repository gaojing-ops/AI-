"""Offline recovery tests; mock model transport, never approve a real chapter."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import formal_suffix_rewrite
import generator
import gui_app
import state_ledger
import state_repair


class PendingStateAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plot = self.root / "plot"
        self.plot.mkdir()
        self.text = "第1章 窗\n\n林舟关上窗。"
        self.state = state_ledger.initial_state()
        self.delta = {"chapter": 1, "events": [{"event_type": "world", "summary": "林舟关上窗",
                                                 "evidence_quote": "林舟关上窗。"}]}
        self.payload = state_repair.pending_audit_candidate(
            self.delta, chapter=1, chapter_sha256=state_ledger.chapter_sha256(self.text), state=self.state)
        self.addCleanup(patch.stopall)
        patch.dict(generator.DIRS, {"plot": str(self.plot)}).start()
        patch("gui_app.continuity_guard.load_canon", return_value={}).start()

    def app(self, audit, *, extraction=None):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {"temporal_memory_enabled": False, "state_delta_max_attempts": 1,
                      "state_delta_independent_audit": True}
        app.project_dir = str(self.root)
        app._official_chapter_paths_by_number = lambda: {}
        app._append_batch_audit = Mock()
        app._claim_chapter_repair = Mock()
        app.phases = []

        def call(*args, phase, **kwargs):
            app.phases.append(phase)
            if phase == "状态提取":
                return json.dumps(extraction or self.delta, ensure_ascii=False)
            if isinstance(audit, Exception):
                raise audit
            return json.dumps(audit, ensure_ascii=False)

        app._call_state_review = call
        return app

    def test_exact_binding_restores_but_changed_body_state_or_delta_refuses(self):
        kwargs = dict(chapter=1, chapter_sha256=state_ledger.chapter_sha256(self.text), state=self.state)
        self.assertEqual(self.delta, state_repair.restore_pending_audit(self.payload, **kwargs))
        for field in ("chapter_sha256", "state_sha256", "delta_sha256", "chapter", "status"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.payload)
                changed[field] = "wrong"
                with self.assertRaises(ValueError):
                    state_repair.restore_pending_audit(changed, **kwargs)
        for status in ("REJECTED", "RESOLVED"):
            self.assertIsNone(state_repair.restore_pending_audit({**self.payload, "status": status}, **kwargs))
        with self.assertRaises(ValueError):
            state_repair.restore_pending_audit({**self.payload, "pass": True}, **kwargs)
        with self.assertRaises(ValueError):
            state_repair.restore_pending_audit({**self.payload, "schema_version": True}, **kwargs)

    def test_service_unavailable_persists_and_recovery_only_calls_auditor(self):
        first = self.app(RuntimeError("service unavailable"))
        args = first._formal_suffix_state_repair_arguments(1, "a" * 64)
        with self.assertRaisesRegex(state_ledger.StateExtractionError, "独立证据审计调用未完成"):
            first._prepare_structured_state_delta(1, self.text, state_override=self.state, **args)
        pending = formal_suffix_rewrite.load_preflight_stage(self.root, chapter=1, context_fingerprint="a" * 64,
                                                            stage="structured_state_pending_audit", strict=True)
        self.assertEqual("PENDING", pending["status"])
        self.assertEqual(["状态提取", "独立证据审计"], first.phases)
        second = self.app({"pass": True, "reject": [], "missing": []})
        args = second._formal_suffix_state_repair_arguments(1, "a" * 64)
        result = second._prepare_structured_state_delta(1, self.text, state_override=self.state, **args)
        self.assertEqual(["独立证据审计"], second.phases)
        self.assertEqual(1, result["chapter"])
        done = formal_suffix_rewrite.load_preflight_stage(self.root, chapter=1, context_fingerprint="a" * 64,
                                                         stage="structured_state_pending_audit", strict=True)
        self.assertEqual("RESOLVED", done["status"])
        self.assertFalse((self.plot / "story_state.json").exists())

    def test_pending_requires_real_audit_even_with_optional_audits_disabled(self):
        app = self.app({"pass": True, "reject": [], "missing": []})
        app.config.update(state_delta_independent_audit=False, pre_save_continuity_audit_enabled=False)
        app._prepare_structured_state_delta(1, self.text, state_override=self.state, pending_audit_context=self.payload)
        self.assertEqual(["独立证据审计"], app.phases)

    def test_semantic_rejection_marks_candidate_not_auto_reusable(self):
        app = self.app({"pass": False, "reject": [{"path": "events[0]", "reason": "bad"}], "missing": []})
        saved = []
        with self.assertRaises(state_ledger.StateExtractionError):
            app._prepare_structured_state_delta(1, self.text, state_override=self.state,
                                                pending_audit_context=self.payload, pending_audit_callback=saved.append)
        self.assertEqual(["PENDING", "REJECTED"], [row["status"] for row in saved])
        self.assertIsNone(state_repair.restore_pending_audit(saved[-1], chapter=1,
                         chapter_sha256=state_ledger.chapter_sha256(self.text), state=self.state))

    def test_candidate_write_failure_stops_before_audit(self):
        app = self.app({"pass": True, "reject": [], "missing": []})
        with self.assertRaisesRegex(state_ledger.StateExtractionError, "STATE_PENDING_AUDIT_STORAGE_FAILED"):
            app._prepare_structured_state_delta(1, self.text, state_override=self.state,
                                                pending_audit_callback=Mock(side_effect=OSError("disk")))
        self.assertEqual(["状态提取"], app.phases)

    def test_cache_context_mismatch_and_pruning_keep_pending_evidence(self):
        path = formal_suffix_rewrite.store_preflight_stage(self.root, chapter=1, context_fingerprint="a" * 64,
                  stage="structured_state_pending_audit", payload=self.payload)
        self.assertIsNone(formal_suffix_rewrite.load_preflight_stage(self.root, chapter=1,
                  context_fingerprint="b" * 64, stage="structured_state_pending_audit", strict=True))
        self.assertTrue(formal_suffix_rewrite._preflight_cache_has_recovery_data(Path(path)))
        for number in range(formal_suffix_rewrite.PREFLIGHT_CACHE_MAX_PER_CHAPTER + 3):
            fingerprint = f"{number:064x}"
            formal_suffix_rewrite.store_preflight_stage(self.root, chapter=1, context_fingerprint=fingerprint,
                                                        stage="release_guard", payload={"status": "PASS"})
        self.assertTrue(Path(path).exists())

    def test_failed_large_write_cannot_be_unlocked_by_probe_after_restart(self):
        app = self.app({"pass": True, "reject": [], "missing": []})
        args = app._formal_suffix_state_repair_arguments(1, "a" * 64)
        real_store = formal_suffix_rewrite.store_preflight_stage

        def fail_candidate(*a, **kw):
            if kw.get("stage") == "structured_state_pending_audit":
                raise OSError("large write failed")
            return real_store(*a, **kw)

        with patch("formal_suffix_rewrite.store_preflight_stage", side_effect=fail_candidate):
            with self.assertRaisesRegex(state_ledger.StateExtractionError, "STATE_PENDING_AUDIT_STORAGE_FAILED"):
                app._prepare_structured_state_delta(1, self.text, state_override=self.state, **args)
        restarted = self.app({"pass": True, "reject": [], "missing": []})
        with self.assertRaisesRegex(state_ledger.StateExtractionError, "STATE_REPAIR_STORAGE_FAILED"):
            restarted._formal_suffix_state_repair_arguments(1, "a" * 64)
        self.assertEqual([], restarted.phases)
        # Only recovery of the actual in-memory candidate resolves the intent.
        recovered = app._formal_suffix_state_repair_arguments(1, "a" * 64)
        self.assertEqual("PENDING", recovered["pending_audit_context"]["status"])
