import json
import tempfile
import unittest
from pathlib import Path

import knowledge_manager


class KnowledgeManagerTailTests(unittest.TestCase):
    def test_update_fact_db_synchronizes_legacy_progress_alias(self):
        for latest, legacy, chapter, expected in ((87, 62, 88, 88), (88, 62, 80, 88)):
            with self.subTest(chapter=chapter), tempfile.TemporaryDirectory() as temp:
                plot = Path(temp)
                (plot / knowledge_manager.FACT_DB_FILENAME).write_text(
                    json.dumps({"latest_chapter": latest, "current_chapter": legacy}),
                    encoding="utf-8",
                )
                knowledge_manager.update_fact_db(str(plot), chapter, "章节正文")
                result = knowledge_manager.load_fact_db(str(plot))
                self.assertEqual(expected, result["latest_chapter"])
                self.assertEqual(expected, result["current_chapter"])

    def test_truncate_fact_db_synchronizes_legacy_alias_to_empty_history(self):
        with tempfile.TemporaryDirectory() as temp:
            plot = Path(temp)
            (plot / knowledge_manager.FACT_DB_FILENAME).write_text(
                json.dumps({"latest_chapter": 4, "current_chapter": 4,
                            "chapters": {"4": {"chapter": 4}}}),
                encoding="utf-8",
            )
            knowledge_manager.truncate_fact_db_after(str(plot), 0)
            result = knowledge_manager.load_fact_db(str(plot))
            self.assertEqual(0, result["latest_chapter"])
            self.assertEqual(0, result["current_chapter"])

    def test_truncate_fact_db_removes_newer_chapters_and_recomputes_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            plot = Path(temp) / "plot"
            plot.mkdir()
            (plot / "canon_state.json").write_text(
                json.dumps({"current_chapter": 2}, ensure_ascii=False),
                encoding="utf-8",
            )
            payload = {
                "schema_version": 1,
                "latest_chapter": 4,
                "current_chapter": 4,
                "updated_at": "old",
                "chapters": {
                    str(chapter): {"chapter": chapter, "title": f"第{chapter}章"}
                    for chapter in range(1, 5)
                },
                "recent_chapters": [
                    {"chapter": chapter, "title": f"第{chapter}章"}
                    for chapter in range(1, 5)
                ],
                "sources": {},
                "canon": {"current_chapter": 4},
            }
            (plot / knowledge_manager.FACT_DB_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

            knowledge_manager.truncate_fact_db_after(str(plot), 2)

            result = knowledge_manager.load_fact_db(str(plot))
            self.assertEqual(result["latest_chapter"], 2)
            self.assertEqual(result["current_chapter"], 2)
            self.assertEqual(sorted(result["chapters"]), ["1", "2"])
            self.assertEqual(
                [item["chapter"] for item in result["recent_chapters"]], [1, 2]
            )
            self.assertEqual(result["canon"]["current_chapter"], 2)


if __name__ == "__main__":
    unittest.main()
