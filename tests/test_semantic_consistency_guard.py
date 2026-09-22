# -*- coding: utf-8 -*-

import json
from pathlib import Path
import tempfile
import unittest

import semantic_consistency_guard as guard


def _base_config():
    return {
        "identity_bindings": [
            {
                "id": "shaocen_b_real_name",
                "entity": "邵岑B",
                "claim_patterns": [
                    r"邵岑B(?:的)?真名(?:是|为|叫)\s*(?P<value>陈术|方旭|马会)"
                ],
                "allowed_values": ["马会"],
                "severity": "critical",
            }
        ],
        "exclusive_fact_groups": [
            {
                "id": "jiang_bai_cause_of_death",
                "severity": "critical",
                "max_active": 1,
                "facts": [
                    {
                        "id": "basilar_artery",
                        "label": "基底动脉撕裂",
                        "patterns": [r"基底动脉撕裂|颅内出血"],
                    },
                    {
                        "id": "alveolar_tear",
                        "label": "急性肺泡撕裂",
                        "patterns": [r"急性肺泡撕裂"],
                    },
                    {
                        "id": "tension_pneumothorax",
                        "label": "张力性气胸",
                        "patterns": [r"张力性气胸"],
                    },
                    {
                        "id": "sternal_penetration",
                        "label": "胸骨后贯通性骨裂",
                        "patterns": [r"胸骨后贯通性骨裂"],
                    },
                ],
            }
        ],
        "forbidden_patterns": [
            {
                "id": "review_report",
                "patterns": [r"审稿报告"],
                "severity": "error",
            }
        ],
        "numeric_rules": [
            {
                "id": "ambient_oxygen_percent",
                "patterns": [
                    r"(?:环境|室内|舱内|空气)(?:氧气|氧)浓度(?:降至|为|是|达到)?\s*"
                    r"(?P<value>\d+(?:\.\d+)?)\s*%"
                ],
                "min": 15,
                "max": 25,
                "unit": "%",
                "severity": "critical",
            }
        ],
    }


class SemanticGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp.name)
        (self.project_dir / "plot").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, config=None):
        path = self.project_dir / "plot" / "semantic_invariants.json"
        path.write_text(
            json.dumps(config or _base_config(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def issue_codes(self, report):
        return [issue["code"] for issue in report["issues"]]

    def test_missing_config_is_explicitly_unavailable_and_blocking(self):
        report = guard.scan_chapter(self.project_dir, "沈砚推开证物室的门。", 1)

        self.assertEqual("unavailable", report["status"])
        self.assertFalse(report["available"])
        self.assertFalse(report["passed"])
        self.assertIn("semantic_config_missing", self.issue_codes(report))

    def test_corrupt_or_incomplete_config_fails_closed(self):
        path = self.project_dir / "plot" / "semantic_invariants.json"
        path.write_text('{"identity_bindings": [', encoding="utf-8")
        corrupt = guard.scan_chapter(self.project_dir, "正文。", 1)
        self.assertEqual("blocked", corrupt["status"])
        self.assertFalse(corrupt["config_valid"])
        self.assertIn("semantic_config_invalid", self.issue_codes(corrupt))

        path.write_text(
            json.dumps({"identity_bindings": []}),
            encoding="utf-8",
        )
        incomplete = guard.scan_chapter(self.project_dir, "正文。", 1)
        self.assertEqual("blocked", incomplete["status"])
        self.assertIn("semantic_config_invalid", self.issue_codes(incomplete))

    def test_identity_binding_detects_chen_fang_and_ma_conflict(self):
        self.write_config()
        report = guard.scan_chapters(
            self.project_dir,
            {
                23: "邵岑B的真名是陈术。",
                24: "名单写明，邵岑B真名为方旭。",
                25: "马会低声承认：邵岑B真名叫马会。",
            },
        )

        self.assertEqual("fail", report["status"])
        conflicts = [
            issue
            for issue in report["issues"]
            if issue["code"] == "identity_binding_conflict"
        ]
        self.assertEqual(1, len(conflicts))
        values = {row["claimed_value"] for row in conflicts[0]["evidence"]}
        self.assertEqual({"陈术", "方旭", "马会"}, values)
        self.assertTrue(conflicts[0]["blocking"])

    def test_exclusive_group_detects_all_four_jiang_bai_causes(self):
        self.write_config()
        report = guard.scan_chapters(
            self.project_dir,
            [
                (25, "江柏的基底动脉撕裂，最终形成颅内出血。"),
                (26, "记录把江柏死因写成急性肺泡撕裂。"),
                (27, "尸检结论又变成张力性气胸。"),
                (30, "结案书认定为胸骨后贯通性骨裂。"),
            ],
        )

        conflicts = [
            issue
            for issue in report["issues"]
            if issue["code"] == "exclusive_fact_conflict"
        ]
        self.assertEqual(1, len(conflicts))
        fact_ids = {row["fact_id"] for row in conflicts[0]["evidence"]}
        self.assertEqual(
            {
                "basilar_artery",
                "alveolar_tear",
                "tension_pneumothorax",
                "sternal_penetration",
            },
            fact_ids,
        )

    def test_ambient_oxygen_88_percent_is_rejected(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            "警报亮起，室内氧气浓度降至88%。",
            28,
        )

        violations = [
            issue
            for issue in report["issues"]
            if issue["code"] == "numeric_rule_violation"
        ]
        self.assertEqual(1, len(violations))
        self.assertEqual(88.0, violations[0]["evidence"][0]["value"])
        self.assertEqual(28, violations[0]["evidence"][0]["chapter"])

    def test_exact_repeated_sentence_and_paragraph_are_rejected(self):
        self.write_config()
        sentence = "沈砚把密封袋放回恒温证物箱。"
        text = f"第22章 旧伤\n\n{sentence}\n\n{sentence}"
        report = guard.scan_chapter(self.project_dir, text, 22)

        self.assertIn("duplicate_sentence", self.issue_codes(report))
        duplicate = next(
            issue for issue in report["issues"] if issue["code"] == "duplicate_sentence"
        )
        self.assertEqual(2, len(duplicate["evidence"]))
        self.assertEqual([3, 5], [row["line"] for row in duplicate["evidence"]])

    def test_meta_narrative_leak_is_rejected(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            "这是第二案第一次正向兑现题名能力，沈砚终于得到技痕。",
            17,
        )

        leaks = [
            issue
            for issue in report["issues"]
            if issue["code"] == "meta_narrative_leak"
        ]
        self.assertTrue(leaks)
        self.assertEqual(17, leaks[0]["evidence"][0]["chapter"])

    def test_bare_chapter_back_reference_and_whole_chapter_commentary_are_rejected(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            (
                "第29章 死亡资格赛\n\n"
                "第27章那次逆伤还没稳定。随后，他开始全章唯一一次主动复演。"
            ),
            29,
        )

        leaks = [
            issue
            for issue in report["issues"]
            if issue["code"] == "meta_narrative_leak"
        ]
        self.assertGreaterEqual(len(leaks), 2)
        self.assertTrue(all(row["evidence"][0]["line"] > 1 for row in leaks))

    def test_document_section_reference_is_not_treated_as_author_meta(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            "第8章 合同\n\n律师翻到合同第十章，逐条核对违约责任。",
            8,
        )

        self.assertNotIn("meta_narrative_leak", self.issue_codes(report))

    def test_transport_truncation_artifact_is_rejected(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            "第14章 第四个名字\n\n章…831 tokens truncated…随后进入下一幕。",
            14,
        )

        leaks = [
            issue
            for issue in report["issues"]
            if issue["code"] == "meta_narrative_leak"
        ]
        self.assertTrue(leaks)
        self.assertEqual(
            "transport_truncation_artifact",
            leaks[0]["rule_id"],
        )

    def test_configured_forbidden_pattern_is_enforced(self):
        self.write_config()
        report = guard.scan_chapter(
            self.project_dir,
            "审稿报告认为这一段已经通过。",
            30,
        )
        self.assertIn("forbidden_pattern", self.issue_codes(report))

    def test_normal_manuscript_passes(self):
        self.write_config()
        report = guard.scan_chapters(
            self.project_dir,
            {
                1: "雨水敲在解剖室的窗上。沈砚重新核对了封条编号。",
                2: "宋停云戴好手套，将第二只证物袋放上冷光台。",
            },
        )

        self.assertEqual("pass", report["status"], report)
        self.assertTrue(report["passed"])
        self.assertEqual(0, report["summary"]["total"])


if __name__ == "__main__":
    unittest.main()
