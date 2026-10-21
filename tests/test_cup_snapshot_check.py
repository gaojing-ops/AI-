import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import cup_snapshot_check as cup

VALID = """# 只作作者预案
| 组 | 1 | 2 | 3 | 4 | 三个比赛日 |
| --- | --- | --- | --- | --- | --- |
| A | A甲 | A乙 | A丙 | A丁 | 6/13、19、25 |
| B | B甲 | B乙 | B丙 | B丁 | 6/14、20、26 |

| 组 | 1-2 | 3-4 | 1-3 | 4-2 | 4-1 | 2-3 |
| --- | --- | --- | --- | --- | --- | --- |
| A | 2:0 | 2:0 | 1:0 | 0:2 | 0:3 | 1:0 |
| B | 2:1 | 1:0 | 0:0 | 0:2 | 0:3 | 1:0 |
"""


class CupSnapshotTests(unittest.TestCase):
    def scan(self, text=VALID, as_of="2030-06-24"):
        return cup.check_text(text, year=2030, as_of=as_of, expected_groups=2)

    def test_current_two_rounds_without_ranking_or_confirmation(self):
        report = self.scan()
        self.assertTrue(report["structure_valid"])
        self.assertEqual((report["included_matches"], report["excluded_future_matches"]), (8, 4))
        self.assertEqual(report["completed_groups_by_plan_date"], [])
        self.assertFalse(report["rankings_calculated"])
        self.assertFalse(report["confirmed_results"])
        self.assertFalse(report["formal_adoption"])
        teams = report["groups"][0]["teams_in_source_order_not_ranking"]
        self.assertEqual([row["points"] for row in teams], [6, 3, 3, 0])

    def test_before_first_match_and_after_all_matches(self):
        self.assertEqual(self.scan(as_of="2030-06-01")["included_matches"], 0)
        report = self.scan(as_of="2030-06-26")
        self.assertEqual(report["included_matches"], 12)
        self.assertEqual(report["excluded_future_matches"], 0)
        self.assertEqual(report["completed_groups_by_plan_date"], ["A", "B"])

    def test_one_group_complete_and_future_scores_never_used(self):
        report = self.scan(as_of="2030-06-25")
        self.assertEqual(report["included_matches"], 10)
        self.assertEqual(report["completed_groups_by_plan_date"], ["A"])
        changed = VALID.replace("| B | 2:1 | 1:0 | 0:0 | 0:2 | 0:3 | 1:0 |",
                                "| B | 2:1 | 1:0 | 0:0 | 0:2 | 8:9 | 9:8 |")
        self.assertEqual(report["groups"], self.scan(changed, "2030-06-25")["groups"])

    def test_duplicate_team_rejected_without_partial_snapshot(self):
        report = self.scan(VALID.replace("B甲", "A甲"))
        self.assertFalse(report["structure_valid"])
        self.assertEqual(report["groups"], [])
        self.assertIn("DUPLICATE_TEAM", {x["code"] for x in report["issues"]})

    def test_bad_or_reversed_dates_rejected(self):
        for bad in ("6/13、12、25", "6/31、7/1、2", "13、19、25", "2031/6/13、19、25"):
            with self.subTest(bad=bad):
                self.assertFalse(self.scan(VALID.replace("6/13、19、25", bad))["structure_valid"])
        with self.assertRaises(ValueError):
            self.scan(as_of="2031-06-24")

    def test_missing_duplicate_or_malformed_score_group_rejected(self):
        score = "| B | 2:1 | 1:0 | 0:0 | 0:2 | 0:3 | 1:0 |"
        for broken in (VALID.replace(score, ""), VALID + score + "\n",
                       VALID.replace(score, score.replace("0:3", "-1:3"))):
            self.assertFalse(self.scan(broken)["structure_valid"])

    def test_repeated_header_and_missing_calendar_group_rejected(self):
        header = "| 组 | 1 | 2 | 3 | 4 | 三个比赛日 |"
        self.assertFalse(self.scan(VALID + "\n" + header)["structure_valid"])
        self.assertFalse(self.scan(VALID.replace("| B | B甲 | B乙 | B丙 | B丁 | 6/14、20、26 |", ""))["structure_valid"])
        with self.assertRaises(ValueError):
            cup.check_text(VALID, year=2030, as_of="2030-06-24", expected_groups=27)

    def test_input_limit_and_utf8_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.md"
            path.write_bytes(b"x" * (cup.MAX_BYTES + 1))
            with self.assertRaises(ValueError):
                cup.check_file(path, year=2030, as_of="2030-06-24", expected_groups=2)
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeError):
                cup.check_file(path, year=2030, as_of="2030-06-24", expected_groups=2)
        with self.assertRaises(ValueError):
            cup.check_text("x" * (cup.MAX_BYTES + 1), year=2030, as_of="2030-06-24")

    def test_cli_is_read_only_and_reports_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.md"
            path.write_text(VALID, encoding="utf-8-sig")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cup.main([str(path), "--year", "2030", "--as-of", "2030-06-24",
                                 "--expected-groups", "2"])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(report["read_only"])
            self.assertEqual(report["scope"], "authored_score_plan_only")
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
