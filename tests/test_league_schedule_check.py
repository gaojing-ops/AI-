import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import league_schedule_check as league


VALID = """# 本人球队赛历
| R01 | 2030/01/21 | 主 | 甲队 | 1:0 | 67 | 已发生 | 第一场 |
| R02 | 2030/01/28 | 客 | 乙队 | 0:0 | 0 | 已发生 | 备用 |
| R03 | 2030/02/04 | 客 | 甲队 | 1:1 | 15 | 待写 | 下一场 |
| R04 | 2030/02/11 | 主 | 乙队 | 0:1 | 0 | 待写 | 之后 |
"""


class LeagueScheduleTests(unittest.TestCase):
    def scan(self, text=VALID, **kwargs):
        return league.check_text(text, expected_opponents=2, **kwargs)

    def codes(self, report):
        return {issue["code"] for issue in report["issues"]}

    def test_complete_schedule_is_not_completed_results(self):
        report = self.scan(require_complete=True)
        self.assertTrue(report["structure_valid"])
        self.assertTrue(report["complete_structure"])
        self.assertFalse(report["all_results_played"])
        self.assertEqual(report["played_points"], 4)
        self.assertEqual(report["pending_target_points"], 1)
        self.assertEqual(report["played_personal_minutes"], 67)
        self.assertEqual(report["pending_target_minutes"], 15)

    def test_duplicate_away_fixture_detected(self):
        broken = VALID.replace("| 主 | 甲队 |", "| 客 | 甲队 |")
        report = self.scan(broken, require_complete=True)
        self.assertIn("DUPLICATE_FIXTURE", self.codes(report))
        self.assertIn("INCOMPLETE_DOUBLE_ROUND_ROBIN", self.codes(report))

    def test_missing_round_only_rejected_when_complete_required(self):
        short = "\n".join(VALID.splitlines()[:-1])
        self.assertTrue(self.scan(short)["structure_valid"])
        self.assertFalse(self.scan(short)["complete_structure"])
        self.assertIn("INCOMPLETE_DOUBLE_ROUND_ROBIN",
                      self.codes(self.scan(short, require_complete=True)))

    def test_declared_blackout_checked(self):
        report = self.scan(blackouts=[league.blackout_range("2030-01-20:2030-01-22")])
        self.assertIn("BLACKOUT_CONFLICT", self.codes(report))
        with self.assertRaises(ValueError):
            league.blackout_range("2030-01-22:2030-01-20")

    def test_malformed_rows_not_silently_skipped(self):
        for change in ("| 2030/01/32 |", "| 不确定 |"):
            broken = VALID.replace("| 2030/01/21 |", change)
            self.assertIn("MALFORMED_ROW", self.codes(self.scan(broken)))
        self.assertIn("MALFORMED_ROW",
                      self.codes(self.scan(VALID.replace("| 67 |", "| 999 |"))))
        self.assertIn("MALFORMED_ROW",
                      self.codes(self.scan(VALID.replace("| 待写 |", "| 已过审 |"))))
        self.assertIn("NO_FIXTURES", self.codes(self.scan("# 还没有表")))

    def test_duplicate_round_and_bad_date_order(self):
        broken = VALID.replace("R03", "R02").replace("2030/02/04", "2030/01/21")
        codes = self.codes(self.scan(broken))
        self.assertIn("DUPLICATE_ROUND", codes)
        self.assertIn("DATE_ORDER_OR_DUPLICATE", codes)

    def test_three_same_venues_detected(self):
        broken = VALID.replace("| 客 |", "| 主 |")
        self.assertIn("THREE_CONSECUTIVE_SAME_VENUE", self.codes(self.scan(broken)))

    def test_file_bom_hash_and_no_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "赛历.md"
            path.write_text(VALID, encoding="utf-8-sig")
            raw = path.read_bytes()
            report = league.check_file(path, expected_opponents=2)
            self.assertEqual(report["raw_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)
            with patch.object(league, "MAX_BYTES", 3), self.assertRaises(ValueError):
                league.check_file(path, expected_opponents=2)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "赛历.md"
            path.write_text(VALID, encoding="utf-8")
            args = ["--ledger", str(path), "--expected-opponents", "2",
                    "--require-complete"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(league.main(args), 0)
            self.assertFalse(json.loads(output.getvalue())["all_results_played"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(league.main(args + ["--blackout",
                                                    "2030-01-20:2030-01-22"]), 1)
                self.assertEqual(league.main(args + ["--expected-opponents", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
