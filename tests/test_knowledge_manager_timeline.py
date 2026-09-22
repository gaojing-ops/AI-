# -*- coding: utf-8 -*-

import json
from pathlib import Path
import tempfile
import unittest

import knowledge_manager
import state_ledger


class KnowledgeManagerTimelineTests(unittest.TestCase):
    def test_chronicle_and_timeline_are_distinct_evidence_views(self):
        with tempfile.TemporaryDirectory() as root:
            plot = Path(root) / "plot"
            plot.mkdir()
            (plot / "canon_state.json").write_text(
                json.dumps(
                    {
                        "current_location": "旧站值班室",
                        "current_realm": "调查阶段",
                        "current_injury": "无",
                        "open_hook": "七号灯亮起",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            chapter = (
                "第1章 雨夜旧站\n\n"
                "午夜，林舟在旧站值班室守了三分钟，等到七号灯亮起。"
            )
            delta = {
                "chapter": 1,
                "characters": [],
                "resources": [],
                "relationships": [],
                "hooks": [],
                "subplots": [],
                "events": [{
                    "event_type": "investigation",
                    "summary": "林舟等到七号灯亮起",
                    "time_anchor": "午夜",
                    "duration": "三分钟",
                    "location": "旧站值班室",
                    "evidence_quote": "午夜，林舟在旧站值班室守了三分钟，等到七号灯亮起。",
                }],
            }
            normalized, issues = state_ledger.validate_delta(
                delta, 1, chapter, state_ledger.initial_state()
            )
            self.assertEqual([], issues)
            state_ledger.commit_validated_delta(plot, normalized, chapter)
            knowledge_manager.rebuild_memory_sources_from_structured_state(plot)

            chronicle = (plot / "世界编年史.txt").read_text(encoding="utf-8")
            timeline = (plot / "时间线锚点.txt").read_text(encoding="utf-8")
            self.assertIn("第1章：林舟等到七号灯亮起", chronicle)
            self.assertNotIn("时长=三分钟", chronicle)
            self.assertIn("时间=午夜", timeline)
            self.assertIn("时长=三分钟", timeline)
            self.assertIn("地点=旧站值班室", timeline)
            self.assertNotEqual(chronicle, timeline)


if __name__ == "__main__":
    unittest.main()
