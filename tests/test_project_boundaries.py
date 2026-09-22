import json
import os
import tempfile
import unittest

import cross_chapter_scanner
import generator


class ProjectBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.old_dirs = dict(generator.DIRS)
        self.old_current_volume = generator.config.get("current_volume")
        generator.DIRS = {
            "world": os.path.join(self.root, "world_building"),
            "chars": os.path.join(self.root, "characters"),
            "plot": os.path.join(self.root, "plot"),
            "out": os.path.join(self.root, "output"),
            "hist": os.path.join(self.root, "history"),
            "logs": os.path.join(self.root, "logs"),
            "publish": os.path.join(self.root, "publish"),
        }
        for path in generator.DIRS.values():
            os.makedirs(path, exist_ok=True)
        with open(os.path.join(self.root, "project_config.json"), "w", encoding="utf-8") as f:
            json.dump({"volume_ranges": [[1, 2, "第一卷"], [3, 4, "第二卷"]]}, f)

    def tearDown(self):
        generator.DIRS = self.old_dirs
        generator.config["current_volume"] = self.old_current_volume
        generator.reload_project_tool_rules()
        self.temp.cleanup()

    def _write(self, volume, filename, text="正文"):
        folder = os.path.join(generator.DIRS["out"], volume)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_next_chapter_switches_to_configured_volume(self):
        latest = self._write("第01卷", "第0002章.txt")
        volume, next_chapter, path, latest_chapter, latest_path = generator.get_latest_chapter_info()
        self.assertEqual((volume, next_chapter, latest_chapter), (2, 3, 2))
        self.assertEqual(latest_path, latest)
        self.assertTrue(path.endswith(os.path.join("第02卷", "第0003章.txt")))

    def test_drafts_and_check_files_are_not_checkpoints(self):
        self._write("第01卷", "第0001章.txt")
        self._write("第02卷", "第0003章_待审.txt")
        self._write("第02卷", "第0003章_托管检查.txt")
        volume, next_chapter, path, latest_chapter, _ = generator.get_latest_chapter_info()
        self.assertEqual((volume, next_chapter, latest_chapter), (1, 2, 1))
        self.assertTrue(path.endswith(os.path.join("第01卷", "第0002章.txt")))

    def test_layout_audit_detects_gap_duplicate_and_wrong_volume(self):
        self._write("第01卷", "第0001章.txt")
        self._write("第01卷", "第0003章.txt")
        self._write("第02卷", "第0003章.txt")
        audit = generator.audit_chapter_layout()
        self.assertEqual(audit["missing"], [2])
        self.assertIn(3, audit["duplicates"])
        self.assertTrue(any(item[0] == 3 for item in audit["misplaced"]))

    def test_consistent_flat_layout_is_valid_and_continues_flat(self):
        first = os.path.join(generator.DIRS["out"], "第0001章.txt")
        second = os.path.join(generator.DIRS["out"], "第0002章.txt")
        for path in (first, second):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("正文")
        audit = generator.audit_chapter_layout()
        self.assertEqual([], audit["missing"])
        self.assertEqual({}, audit["duplicates"])
        self.assertEqual([], audit["misplaced"])

        volume, next_chapter, path, latest_chapter, latest_path = (
            generator.get_latest_chapter_info()
        )
        self.assertEqual((volume, next_chapter, latest_chapter), (2, 3, 2))
        self.assertEqual(second, latest_path)
        self.assertEqual(
            os.path.join(generator.DIRS["out"], "第0003章.txt"),
            path,
        )

    def test_recursive_scanner_crosses_volumes_and_excludes_drafts(self):
        one = self._write("第01卷", "第0002章.txt")
        two = self._write("第02卷", "第0003章.txt")
        self._write("第02卷", "第0004章_待审.txt")
        files = cross_chapter_scanner.get_chapter_files_recursive(
            generator.DIRS["out"], 2, 4
        )
        self.assertEqual(files, [one, two])

    def test_missing_rule_file_clears_previous_project_rules(self):
        rules_path = os.path.join(generator.DIRS["plot"], "tool_rules.json")
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump({"banned_keywords": ["旧项目禁词"]}, f, ensure_ascii=False)
        generator.reload_project_tool_rules()
        self.assertIn("旧项目禁词", generator.FORBIDDEN_KEYWORDS)
        os.remove(rules_path)
        generator.reload_project_tool_rules()
        self.assertEqual(generator.FORBIDDEN_KEYWORDS, {})


if __name__ == "__main__":
    unittest.main()
