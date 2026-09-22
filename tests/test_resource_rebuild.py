import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import state_ledger
from maintenance import rebuild_materialized_resources as repair


class ResourceRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.plot = self.project / "plot"
        self.plot.mkdir()
        output = self.project / "output" / "第01卷"
        output.mkdir(parents=True)
        text = "第1章 测试\n\n林舟的尝试次数已经归零。"
        (output / "第0001章.txt").write_text(text, encoding="utf-8")
        delta, issues = state_ledger.validate_delta({
            "chapter": 1,
            "resources": [{"owner": "林舟", "item": "次数", "action": "SET",
                           "quantity": 0, "quantity_change": None, "status": "已用完",
                           "evidence_quote": "林舟的尝试次数已经归零。"}],
            "events": [{"event_type": "resource", "summary": "次数用完",
                        "evidence_quote": "林舟的尝试次数已经归零。"}],
        }, 1, text, state_ledger.initial_state())
        self.assertEqual([], issues)
        state = state_ledger.commit_validated_delta(self.plot, delta, text)
        next(iter(state["resources"].values()))["quantity"] = 33
        state_ledger._write_materialized_state(self.plot, state)

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_then_execute_preserves_evidence_and_backs_up_state(self):
        old = (self.plot / "story_state.json").read_bytes()
        protected = repair.protected_hashes(self.project)
        report = repair.rebuild(self.project, 1)
        self.assertFalse(report["executed"])
        self.assertEqual(old, (self.plot / "story_state.json").read_bytes())
        self.assertFalse((self.project / ".runtime").exists())
        report = repair.rebuild(self.project, 1, True)
        self.assertTrue(report["executed"])
        self.assertEqual(old, (Path(report["backup"]) / "story_state.json").read_bytes())
        self.assertEqual(protected, repair.protected_hashes(self.project))
        state_ledger.verify_materialized_state(self.plot)
        self.assertFalse(repair.rebuild(self.project, 1, True)["executed"])

    def test_refuses_unrelated_state_change(self):
        state = json.loads((self.plot / "story_state.json").read_text(encoding="utf-8"))
        state["characters"]["错误人物"] = {"location": "错误地点"}
        state_ledger._write_materialized_state(self.plot, state)
        before = (self.plot / "story_state.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "非资源语义差异"):
            repair.rebuild(self.project, 1, True)
        self.assertEqual(before, (self.plot / "story_state.json").read_bytes())

    def test_validation_failure_restores_original_state_and_hash(self):
        before = {name: (self.plot / name).read_bytes()
                  for name in ("story_state.json", "story_state.sha256")}
        with mock.patch.object(state_ledger, "verify_materialized_state", side_effect=ValueError("test")):
            with self.assertRaisesRegex(ValueError, "test"):
                repair.rebuild(self.project, 1, True)
        for name, data in before.items():
            self.assertEqual(data, (self.plot / name).read_bytes())
