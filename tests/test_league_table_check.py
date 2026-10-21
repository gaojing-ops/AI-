import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import league_table_check as checker


def matrix():
    names = ["A", "B", "C", "D"]
    lines = ["| 主队 | " + " | ".join(names) + " |",
             "| --- | --- | --- | --- | --- |"]
    for home in names:
        lines.append("| " + home + " | " +
                     " | ".join("—" if home == away else "1:0" for away in names) + " |")
    return "\n".join(lines)


class CompleteLeagueTests(unittest.TestCase):
    def report(self, text):
        return checker.check_text(text, expected_teams=4)

    def assert_invalid(self, text):
        result = self.report(text)
        self.assertFalse(result["structure_valid"])
        self.assertEqual(result["teams_in_source_order_not_ranking"], [])

    def test_complete_home_and_away_totals(self):
        result = self.report(matrix())
        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["planned_matches"], 12)
        self.assertEqual(result["total_points"], 36)
        self.assertEqual(result["goals_for_total"], result["goals_against_total"])
        for row in result["teams_in_source_order_not_ranking"]:
            self.assertEqual((row["played"], row["points"], row["goals_for"],
                              row["goals_against"]), (6, 9, 3, 3))
        for key in ("rankings_calculated", "confirmed_results", "formal_adoption"):
            self.assertFalse(result[key])

    def test_missing_home_row(self):
        self.assert_invalid("\n".join(matrix().splitlines()[:-1]))

    def test_duplicate_home_row(self):
        self.assert_invalid(matrix().replace("| D | 1:0", "| C | 1:0"))

    def test_duplicate_team_column(self):
        self.assert_invalid(matrix().replace("| 主队 | A | B | C | D |",
                                             "| 主队 | A | B | C | C |"))

    def test_bad_score_refuses_partial_table(self):
        self.assert_invalid(matrix().replace("1:0", "0:100", 1))

    def test_self_match_forbidden(self):
        self.assert_invalid(matrix().replace("—", "0:0", 1))

    def test_two_matrices_are_ambiguous(self):
        self.assert_invalid(matrix() + "\n\n" + matrix())

    def test_column_width_and_separator(self):
        with self.subTest("width"):
            self.assert_invalid(matrix().replace("| A | — | 1:0 | 1:0 | 1:0 |",
                                                 "| A | — | 1:0 | 1:0 |"))
        with self.subTest("extra empty cell"):
            self.assert_invalid(matrix().replace("| A | — | 1:0 | 1:0 | 1:0 |",
                                                 "| A | — | 1:0 | 1:0 | 1:0 ||"))
        with self.subTest("separator"):
            self.assert_invalid(matrix().replace("| --- |", "| bad |", 1))

    def test_file_budget_encoding_links_and_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "scores.md"
            source.write_text(matrix(), encoding="utf-8-sig")
            result = checker.check_file(source, expected_teams=4)
            self.assertTrue(result["structure_valid"])
            self.assertEqual(len(result["source_sha256"]), 64)
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    checker.check_file(source, expected_teams=4)
            source.write_bytes(b"\xff")
            with self.assertRaises(UnicodeError):
                checker.check_file(source, expected_teams=4)
            source.write_bytes(b"x" * (checker.MAX_BYTES + 1))
            with self.assertRaises(ValueError):
                checker.check_file(source, expected_teams=4)
        for count in (1, 27):
            with self.assertRaises(ValueError):
                checker.check_text(matrix(), expected_teams=count)


if __name__ == "__main__":
    unittest.main()
