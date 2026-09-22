import json
import tempfile
import unittest
from pathlib import Path

import cross_chapter_scanner


class RhetoricalTemplateScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "plot").mkdir()

    def tearDown(self):
        cross_chapter_scanner.reload_project_tool_rules(None)
        self.temp.cleanup()

    def write_rules(self, rules):
        (self.project / "plot" / "tool_rules.json").write_text(
            json.dumps({"rhetorical_template_rules": rules}, ensure_ascii=False),
            encoding="utf-8",
        )
        cross_chapter_scanner.reload_project_tool_rules(self.project)

    def test_configured_cross_chapter_density_is_high_and_evidenced(self):
        self.write_rules([
            {
                "id": "evidence_boundary",
                "label": "证据边界免责声明",
                "patterns": ["(?:不能证明|不等于|只能证明)"],
                "per_chapter_max": 3,
                "window_chapters": 10,
                "window_total_max": 5,
                "min_chapters": 3,
                "severity": "HIGH",
            }
        ])
        chapters = [
            (1, "", "这条记录不能证明幕后者。"),
            (2, "", "地址不等于实际使用人。"),
            (3, "", "账单只能证明发生过结算。"),
            (4, "", "录音不能证明现在的立场。"),
            (5, "", "字段不等于机构名称。"),
            (6, "", "单据只能证明付款发生。"),
        ]
        issues = cross_chapter_scanner.scan_rhetorical_templates(chapters)
        self.assertEqual(1, len(issues))
        issue = issues[0]
        self.assertEqual("叙述模板重复", issue["type"])
        self.assertEqual("HIGH", issue["severity"])
        self.assertEqual(6, issue["total_matches"])
        self.assertEqual([1, 2, 3, 4, 5, 6], issue["matched_chapters"])
        self.assertTrue(issue["evidence"])

    def test_legitimate_sparse_boundary_language_passes(self):
        self.write_rules([
            {
                "id": "evidence_boundary",
                "patterns": ["不能证明"],
                "per_chapter_max": 2,
                "window_chapters": 10,
                "window_total_max": 4,
                "min_chapters": 3,
            }
        ])
        chapters = [
            (1, "", "这条记录不能证明幕后者。"),
            (2, "", "他们转去核对设备。"),
            (8, "", "单据还要与账户比对。"),
        ]
        self.assertEqual(
            [], cross_chapter_scanner.scan_rhetorical_templates(chapters)
        )

    def test_single_chapter_over_limit_is_detected(self):
        self.write_rules([
            {
                "id": "evidence_boundary",
                "patterns": ["不能证明"],
                "per_chapter_max": 2,
                "window_chapters": 30,
                "window_total_max": 99,
                "min_chapters": 4,
            }
        ])
        text = "不能证明甲。不能证明乙。不能证明丙。"
        issue = cross_chapter_scanner.scan_rhetorical_templates([(10, "", text)])[0]
        self.assertEqual(3, issue["max_per_chapter"])
        self.assertEqual([10], issue["matched_chapters"])

    def test_repeated_named_subject_negative_openers_are_detected(self):
        self.write_rules([
            {
                "id": "subject_negative_action_density",
                "label": "人物否定式起手",
                "patterns": ["(?:沈砚|唐砺)没有"],
                "per_chapter_max": 4,
                "window_chapters": 30,
                "window_total_max": 6,
                "min_chapters": 3,
                "severity": "HIGH",
            }
        ])
        chapters = [
            (1, "", "沈砚没有回答。唐砺没有追问。"),
            (2, "", "沈砚没有停步。唐砺没有开枪。"),
            (3, "", "沈砚没有解释。唐砺没有靠近。"),
            (4, "", "沈砚没有回头。"),
        ]
        issue = cross_chapter_scanner.scan_rhetorical_templates(chapters)[0]
        self.assertEqual("subject_negative_action_density", issue["rule_id"])
        self.assertEqual(7, issue["total_matches"])
        self.assertEqual("HIGH", issue["severity"])

    def test_invalid_regex_fails_closed(self):
        self.write_rules([
            {
                "id": "broken",
                "patterns": ["("],
            }
        ])
        issues = cross_chapter_scanner.scan_rhetorical_templates(
            [(1, "", "普通正文")]
        )
        self.assertEqual("scanner_config_error", issues[0]["type"])
        self.assertEqual("HIGH", issues[0]["severity"])

    def test_invalid_numeric_limit_fails_closed_instead_of_crashing(self):
        self.write_rules([
            {
                "id": "bad_limit",
                "patterns": ["不能证明"],
                "per_chapter_max": "three",
            }
        ])
        issues = cross_chapter_scanner.scan_rhetorical_templates(
            [(1, "", "普通正文")]
        )
        self.assertEqual("scanner_config_error", issues[0]["type"])
        self.assertEqual("HIGH", issues[0]["severity"])
        self.assertIn("per_chapter_max", issues[0]["message"])


if __name__ == "__main__":
    unittest.main()
