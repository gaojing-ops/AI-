import json
import tempfile
import unittest
from pathlib import Path

import project_status


class ProjectStatusTests(unittest.TestCase):
    def _write_chapters(self, root: Path, directory: str, numbers: list[int]) -> None:
        target = root / directory
        target.mkdir(parents=True, exist_ok=True)
        for number in numbers:
            (target / f"第{number:04d}章.txt").write_text(
                f"第{number}章\n正文", encoding="utf-8"
            )

    def test_complete_book_is_marked_finished_from_formal_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_chapters(root, "output", [1, 2, 3])
            self._write_chapters(root, "publish", [1, 2, 3])

            text = project_status.render_status(
                root,
                {"book_title": "测试书", "target_total_chapters": 3,
                 "model": "gpt-5.6-sol", "review_model": "gpt-5.6-sol"},
                now="2026-08-17T00:00:00",
            )

            self.assertIn("已完结", text)
            self.assertIn("禁止生成第 4 章", text)
            self.assertIn("output/ 与 publish/ 章节范围一致且连续", text)

    def test_gap_or_publish_mismatch_cannot_be_marked_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_chapters(root, "output", [1, 3])
            self._write_chapters(root, "publish", [1, 2])

            text = project_status.render_status(
                root,
                {"book_title": "测试书", "target_total_chapters": 3},
                now="2026-08-17T00:00:00",
            )

            self.assertIn("未完结", text)
            self.assertNotIn("禁止生成第 4 章", text)
            self.assertIn("output/ 与 publish/ 章节范围存在差异", text)
            self.assertIn("缺号：2", text)


if __name__ == "__main__":
    unittest.main()
