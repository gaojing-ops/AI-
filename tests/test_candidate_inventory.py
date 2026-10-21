import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import candidate_inventory as ci


class CandidateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.stage = self.project / "rewrite_staging" / "本次"
        self.stage.mkdir(parents=True)

    def write(self, number, text, encoding="utf-8"):
        path = self.stage / f"第{number:04d}章.txt"
        path.write_text(text, encoding=encoding)
        return path

    def scan(self, **kwargs):
        return ci.inventory(self.project, "本次", **kwargs)

    def test_one_stage_only_ranges_and_no_writes(self):
        first = self.write(91, "第91章 开始\n足球" * 1)
        self.write(93, "第93章 继续\n足球")
        other = self.project / "rewrite_staging" / "旧稿"
        other.mkdir()
        (other / "第0092章.txt").write_text("第92章 占位\n足球", encoding="utf-8")
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        result = self.scan(start=91, end=94, min_body_han=2)
        self.assertEqual(result["present_ranges"], [[91, 91], [93, 93]])
        self.assertEqual(result["missing_ranges"], [[92, 92], [94, 94]])
        self.assertFalse(result["range_text_complete"])
        self.assertFalse(result["formal_adoption_verified"])
        self.assertEqual(result["chapters"][0]["raw_sha256"],
                         hashlib.sha256(first.read_bytes()).hexdigest())
        self.assertEqual(before, {p: p.read_bytes()
                                 for p in self.project.rglob("*") if p.is_file()})

    def test_title_cannot_top_up_body_threshold(self):
        self.write(1, "第1章 很长很长的标题\n足球")
        result = self.scan(start=1, end=1, min_body_han=3)
        row = result["chapters"][0]
        self.assertGreater(row["han_including_title"], 3)
        self.assertEqual(row["body_han"], 2)
        self.assertEqual(row["issues"], ["SHORT_BODY"])
        self.assertEqual(result["attention_chapters"], [1])

    def test_bom_crlf_valid_and_title_number_mismatch(self):
        path = self.stage / "第0001章.txt"
        path.write_bytes(b"\xef\xbb\xbf" + "第1章 正文\r\n\r\n足球\r\n".encode("utf-8"))
        self.write(2, "第3章 错号\n足球")
        rows = self.scan(start=1, end=2, min_body_han=2)["chapters"]
        self.assertEqual(rows[0]["issues"], [])
        self.assertEqual(rows[0]["body_han"], 2)
        self.assertIn("TITLE_NUMBER_MISMATCH", rows[1]["issues"])

    def test_missing_title_and_non_utf8_reported(self):
        self.write(1, "足球")
        (self.stage / "第0002章.txt").write_bytes(b"\xff\xfe")
        rows = self.scan(start=1, end=2, min_body_han=1)["chapters"]
        self.assertEqual(rows[0]["issues"], ["MISSING_TITLE"])
        self.assertEqual(rows[1]["issues"], ["INVALID_UTF8"])

    def test_bounds_and_traversal_rejected(self):
        for name in ("..", "../本次", r"..\本次", "C:本次", "本次."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ci.inventory(self.project, name)
        for args in ({"start": 0}, {"start": 5, "end": 4},
                     {"end": 10000}, {"min_body_han": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.scan(**args)

    def test_resolved_directory_escape_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        original = Path.resolve

        def resolved(path, *args, **kwargs):
            if path == self.stage:
                return original(outside, *args, **kwargs)
            return original(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolved), self.assertRaises(ValueError):
            self.scan(start=1, end=1)

    def test_link_to_external_file_never_read(self):
        path = self.write(1, "第1章 正文\n足球")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("SECRET", encoding="utf-8")
        original = Path.resolve

        def resolved(value, *args, **kwargs):
            if value == path:
                return original(outside, *args, **kwargs)
            return original(value, *args, **kwargs)

        with patch.object(Path, "resolve", resolved):
            row = self.scan(start=1, end=1)["chapters"][0]
        self.assertEqual(row["issues"], ["UNSAFE_LINK"])
        self.assertNotIn("raw_sha256", row)

    def test_oversized_candidate_and_noncanonical_name(self):
        self.write(1, "第1章 标题\n足球足球")
        (self.stage / "第2章.txt").write_text("足球", encoding="utf-8")
        with patch.object(ci, "MAX_CHAPTER_BYTES", 4):
            result = self.scan(start=1, end=2, min_body_han=1)
        self.assertEqual(result["chapters"][0]["issues"], ["FILE_TOO_LARGE"])
        self.assertEqual(result["invalid_names"], ["第2章.txt"])
        self.assertEqual(result["missing_ranges"], [[2, 2]])

    def test_cli_exit_codes_and_candidate_boundary(self):
        self.write(1, "第1章 正文\n足球")
        args = ["--project", str(self.project), "--stage-name", "本次",
                "--start", "1", "--end", "1", "--min-body-han", "2"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ci.main(args)
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["range_text_complete"])
        self.assertFalse(report["literary_quality_assessed"])
        self.assertFalse(report["formal_adoption_verified"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ci.main(args + ["--end", "2"]), 1)
            self.assertEqual(ci.main(args + ["--stage-name", "../"]), 2)


if __name__ == "__main__":
    unittest.main()
