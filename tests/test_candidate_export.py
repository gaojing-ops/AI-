"""Offline candidate exports only; no models or changes to real novels."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import candidate_export as ce
import novel_cli


class CandidateExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "book"
        self.stage = self.root / "rewrite_staging"
        self.stage.mkdir(parents=True)
        self.index = self.stage / "index.json"
        self.output = self.stage / "delivery" / "reading.txt"
        self.rows = []
        for n in (1, 2):
            text = f"第{n}章 测试\n\n" + "正文" * 1501 + "\n"
            source = self.stage / f"第{n:04d}章.txt"
            source.write_bytes(text.encode("utf-8"))
            self.rows.append({"chapter": n, "source_path": str(source),
                "raw_sha256": ce._digest(source.read_bytes()),
                "committable_chapter_sha256": ce._digest(text.strip().encode()),
                "chinese_chars": len(ce.HAN.findall(text)), "title": text.splitlines()[0]})
        self.spec = {"schema_version": 1, "formal_completion": False, "chapters": self.rows}
        self.save_index()
        (self.root / "output").mkdir()
        (self.root / "output" / "sentinel.txt").write_text("正式保持", encoding="utf-8")

    def save_index(self):
        self.index.write_text(json.dumps(self.spec, ensure_ascii=False), encoding="utf-8")

    def plan(self, **kwargs):
        return ce.prepare_export(self.root, self.index, self.output, "测试小说", expected_end=2, **kwargs)

    def test_plan_is_read_only(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        plan = self.plan()
        self.assertFalse(self.output.parent.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(plan.summary["source_passes"], 1)
        self.assertFalse(plan.summary["formal_adoption"])

    def test_export_and_idempotent_reuse(self):
        plan = self.plan()
        first = ce.write_export(plan)
        self.assertEqual(len(first["written_files"]), 2)
        stamp = self.output.stat().st_mtime_ns
        second = ce.write_export(self.plan())
        self.assertEqual(second["written_files"], [])
        self.assertEqual(stamp, self.output.stat().st_mtime_ns)
        text = self.output.read_text(encoding="utf-8")
        self.assertLess(text.index("第1章"), text.index("第2章"))
        self.assertEqual(ce._digest(self.output.read_bytes()), first["book_sha256"])
        self.assertEqual((self.root / "output/sentinel.txt").read_text(encoding="utf-8"), "正式保持")
        self.assertEqual(first["model_calls"], 0)

    def test_source_change_fails_before_writes(self):
        Path(self.rows[0]["source_path"]).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ce.ExportError, "来源已变化"):
            self.plan()
        self.assertFalse(self.output.parent.exists())

    def test_missing_duplicate_and_boolean_numbers_fail(self):
        for numbers in ([1], [1, 1], [True, 2]):
            with self.subTest(numbers=numbers):
                self.spec["chapters"] = [dict(self.rows[i], chapter=n) for i, n in enumerate(numbers)]
                self.save_index()
                with self.assertRaises(ce.ExportError):
                    self.plan()
        self.assertFalse(self.output.parent.exists())

    def test_conflicting_output_or_manifest_not_overwritten(self):
        self.output.parent.mkdir()
        for conflict in (self.output, self.output.with_suffix(".export.json")):
            with self.subTest(path=conflict):
                conflict.write_bytes(b"USER CONTENT")
                with self.assertRaisesRegex(ce.ExportError, "拒绝覆盖"):
                    self.plan()
                self.assertEqual(conflict.read_bytes(), b"USER CONTENT")
                conflict.unlink()  # Own fixed test fixture only.

    def test_output_outside_staging_is_rejected(self):
        self.output = self.root / "output/novel.txt"
        with self.assertRaisesRegex(ce.ExportError, "超出允许目录"):
            self.plan()
        self.assertFalse(self.output.exists())

    def test_source_outside_project_is_rejected(self):
        self.rows[0]["source_path"] = str(self.root.parent / "outside.txt")
        self.save_index()
        with self.assertRaisesRegex(ce.ExportError, "超出允许目录"):
            self.plan()

    def test_oversized_source_is_bounded(self):
        Path(self.rows[0]["source_path"]).write_bytes(b"x" * (ce.MAX_CHAPTER_BYTES + 1))
        with self.assertRaisesRegex(ce.ExportError, "读取预算"):
            self.plan()

    def test_cannot_claim_formal_completion(self):
        self.spec["formal_completion"] = True
        self.save_index()
        with self.assertRaisesRegex(ce.ExportError, "不是正式"):
            self.plan()

    def test_bom_crlf_and_prepared_digest_are_explicit(self):
        source = Path(self.rows[0]["source_path"])
        text = source.read_text(encoding="utf-8")
        source.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
        self.rows[0]["raw_sha256"] = ce._digest(source.read_bytes())
        self.save_index()
        plan = self.plan()
        self.assertNotIn(b"\xef\xbb\xbf", plan.book)
        self.assertNotIn(b"\r", plan.book)

    def test_duplicate_json_keys_rejected(self):
        self.index.write_text('{"formal_completion":false,"formal_completion":true}', encoding="utf-8")
        with self.assertRaisesRegex(ce.ExportError, "重复JSON键"):
            self.plan()

    def test_title_and_net_text_not_trusted(self):
        self.rows[0]["title"] = "第2章 错章"
        self.save_index()
        with self.assertRaisesRegex(ce.ExportError, "标题绑定"):
            self.plan()
        text = "第1章 " + "题" * 3100 + "\n短\n"
        Path(self.rows[0]["source_path"]).write_bytes(text.encode())
        self.rows[0].update(title=text.splitlines()[0],
            raw_sha256=ce._digest(text.encode()),
            committable_chapter_sha256=ce._digest(text.strip().encode()),
            chinese_chars=len(ce.HAN.findall(text)))
        self.save_index()
        with self.assertRaisesRegex(ce.ExportError, "净正文"):
            self.plan()

    def test_index_budget_and_schema_are_bounded(self):
        self.spec["schema_version"] = True
        self.save_index()
        with self.assertRaisesRegex(ce.ExportError, "版本"):
            self.plan()
        self.index.write_bytes(b"x" * (ce.MAX_INDEX_BYTES + 1))
        with self.assertRaisesRegex(ce.ExportError, "读取预算"):
            self.plan()

    def test_total_source_budget_stops_without_writing(self):
        with mock.patch.object(ce, "MAX_BOOK_BYTES", 100):
            with self.assertRaisesRegex(ce.ExportError, "全部来源"):
                self.plan()
        self.assertFalse(self.output.parent.exists())

    def test_cli_defaults_to_read_only_without_model(self):
        args = ["candidate-export", "--project", str(self.root), "--index", str(self.index),
                "--output", str(self.output), "--title", "测试小说", "--end", "2"]
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()), mock.patch(
                "novel_cli.run_bounded_batch", side_effect=AssertionError("must not run generation")):
            self.assertEqual(novel_cli.main(args), 0)
        self.assertTrue(json.loads(stdout.getvalue())["read_only"])
        self.assertFalse(self.output.parent.exists())

    def test_write_conflict_after_plan_stops_before_book_creation(self):
        plan = self.plan()
        plan.manifest_path.parent.mkdir()
        plan.manifest_path.write_bytes(b"user-change")
        with self.assertRaises(ce.ExportError):
            ce.write_export(plan)
        self.assertFalse(plan.output.exists())


if __name__ == "__main__":
    unittest.main()
