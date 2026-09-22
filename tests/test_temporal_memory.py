import json
import tempfile
import unittest
from pathlib import Path

import state_ledger
import temporal_memory


class TemporalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plot = Path(self.temp.name) / "plot"
        self.plot.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def delta(self, chapter, name, event, quote):
        return {
            "chapter": chapter,
            "characters": [{
                "name": name,
                "location": "旧站" if chapter == 1 else "七号仓",
                "condition": "",
                "emotion": "",
                "status": "active",
                "knowledge_add": [event],
                "abilities_add": [],
                "evidence_quote": quote,
            }],
            "resources": [],
            "relationships": [],
            "hooks": [],
            "subplots": [],
            "events": [{
                "event_type": "investigation",
                "summary": event,
                "evidence_quote": quote,
            }],
        }

    def test_retrieves_old_entity_memory_by_relevance_not_only_recency(self):
        old_delta = self.delta(1, "林舟", "林舟发现铜钥匙刻着七号", "林舟发现铜钥匙刻着七号。")
        recent_delta = self.delta(50, "周岚", "周岚检查南门电路", "周岚检查南门电路。")
        temporal_memory.sync_validated_delta(self.plot, old_delta, "a" * 64)
        temporal_memory.sync_validated_delta(self.plot, recent_delta, "b" * 64)

        hits = temporal_memory.retrieve(
            self.plot,
            "林舟准备使用七号铜钥匙",
            before_chapter=100,
            max_hits=10,
        )
        self.assertTrue(hits)
        self.assertEqual(hits[0]["chapter"], 1)
        self.assertIn("林舟", hits[0]["entity"])
        rendered = temporal_memory.render_retrieval(hits)
        self.assertIn("只证明相应章节当时发生", rendered)

    def test_future_memory_is_never_retrieved(self):
        future_delta = self.delta(9, "林舟", "林舟进入七号仓", "林舟进入七号仓。")
        temporal_memory.sync_validated_delta(self.plot, future_delta, "c" * 64)
        hits = temporal_memory.retrieve(
            self.plot, "林舟进入七号仓", before_chapter=9, max_hits=10
        )
        self.assertEqual(hits, [])

    def test_corrupt_database_rebuilds_from_immutable_state_deltas(self):
        chapter = "第1章\n\n林舟发现铜钥匙刻着七号。"
        proposed = self.delta(1, "林舟", "林舟发现铜钥匙刻着七号", "林舟发现铜钥匙刻着七号。")
        normalized, issues = state_ledger.validate_delta(
            proposed, 1, chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        state_ledger.commit_validated_delta(self.plot, normalized, chapter)

        db_path = temporal_memory.database_path(self.plot)
        db_path.write_bytes(b"not a sqlite database")
        temporal_memory.ensure_synced(self.plot)
        audit = temporal_memory.audit(self.plot, 1)
        self.assertEqual(audit["status"], "PASS")
        self.assertGreater(audit["items"], 0)
        self.assertTrue(list(self.plot.glob("story_memory.db.corrupt-*")))

    def test_same_chapter_different_hash_is_rejected(self):
        delta = self.delta(1, "林舟", "林舟取得钥匙", "林舟取得钥匙。")
        temporal_memory.sync_validated_delta(self.plot, delta, "d" * 64)
        with self.assertRaises(temporal_memory.TemporalMemoryError):
            temporal_memory.sync_validated_delta(self.plot, delta, "e" * 64)


if __name__ == "__main__":
    unittest.main()
