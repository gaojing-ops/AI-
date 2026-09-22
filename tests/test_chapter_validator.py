# -*- coding: utf-8 -*-

import copy
import json
from pathlib import Path
import tempfile
import unittest

import chapter_validator


def _empty_semantic_config():
    return {
        "identity_bindings": [{
            "id": "test_identity",
            "entity": "林舟身份",
            "claim_patterns": ["林舟身份[：:](?P<value>主角|反派)"],
            "allowed_values": ["主角"],
        }],
        "exclusive_fact_groups": [],
        "forbidden_patterns": [],
        "numeric_rules": [],
    }


class ChapterValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "plot").mkdir()
        (self.project / "plot" / "semantic_invariants.json").write_text(
            json.dumps(_empty_semantic_config(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.limits = {"min": 20, "target_max": 4500}
        self.good = (
            "第1章 开门\n\n"
            "雨水打在石阶上，林川推开仓库的铁门。"
            "他检查锁芯，发现里面卡着一枚新鲜铜屑。"
        )

    def tearDown(self):
        self.temp.cleanup()

    def codes(self, report):
        return {item["code"] for item in report["issues"]}

    def test_valid_chapter_passes(self):
        report = chapter_validator.validate_candidate(
            self.project, 1, self.good, self.limits
        )
        self.assertTrue(report["passed"], report["issues"])
        self.assertEqual("pass", report["status"])
        self.assertEqual(64, len(report["chapter_sha256"]))
        self.assertEqual(64, len(report["config_digest"]))

    def test_6500_chinese_characters_are_too_long(self):
        body = ("甲乙丙丁戊己庚辛壬癸" * 651)[:6501]
        report = chapter_validator.validate_candidate(
            self.project,
            1,
            "第1章 潮声\n\n" + body,
            {"min": 3000, "target_max": 4500},
        )
        self.assertFalse(report["passed"])
        self.assertIn("chapter_too_long", self.codes(report))

    def test_double_title_is_rejected(self):
        text = self.good + "\n\n第2章 错页\n\n他又读了一遍记录。"
        report = chapter_validator.validate_candidate(
            self.project, 1, text, self.limits
        )
        self.assertIn("chapter_title_count_invalid", self.codes(report))

    def test_wrong_chapter_number_is_rejected(self):
        text = self.good.replace("第1章", "第2章", 1)
        report = chapter_validator.validate_candidate(
            self.project, 1, text, self.limits
        )
        self.assertIn("chapter_number_mismatch", self.codes(report))

    def test_meta_commentary_is_rejected(self):
        text = self.good + "\n\n这是第二案第一次正向兑现题名能力。"
        report = chapter_validator.validate_candidate(
            self.project, 1, text, self.limits
        )
        self.assertIn("meta_narrative_leak", self.codes(report))

    def test_sentence_repeated_twice_is_rejected(self):
        sentence = "他把密封袋放回恒温证物箱。"
        text = "第1章 密封\n\n" + sentence + "\n\n" + sentence
        report = chapter_validator.validate_candidate(
            self.project, 1, text, {"min": 10, "target_max": 500}
        )
        self.assertIn("duplicate_sentence", self.codes(report))

    def test_unpaired_quote_is_rejected(self):
        text = self.good + "\n\n他只说了一句：“门后有人。"
        report = chapter_validator.validate_candidate(
            self.project, 1, text, self.limits
        )
        self.assertIn("unpaired_quote", self.codes(report))

    def test_missing_semantic_config_fails_closed(self):
        (self.project / "plot" / "semantic_invariants.json").unlink()
        report = chapter_validator.validate_candidate(
            self.project, 1, self.good, self.limits
        )
        self.assertFalse(report["passed"])
        self.assertEqual("blocked", report["status"])
        self.assertIn("semantic_config_missing", self.codes(report))

    def test_receipt_tamper_old_version_and_text_change_fail_closed(self):
        report = chapter_validator.validate_candidate(
            self.project, 1, self.good, self.limits
        )
        receipt = chapter_validator.build_validation_receipt(report)
        verified = chapter_validator.verify_validation_receipt(
            receipt, self.project, 1, self.good, self.limits
        )
        self.assertTrue(verified["receipt_valid"], verified["issues"])

        forged = copy.deepcopy(receipt)
        forged["chapter_sha256"] = "0" * 64
        verified = chapter_validator.verify_validation_receipt(
            forged, self.project, 1, self.good, self.limits
        )
        self.assertFalse(verified["passed"])
        self.assertIn("validation_receipt_text_mismatch", self.codes(verified))
        self.assertIn("validation_receipt_digest_mismatch", self.codes(verified))

        old = copy.deepcopy(receipt)
        old["validation_version"] = chapter_validator.VALIDATION_VERSION - 1
        verified = chapter_validator.verify_validation_receipt(
            old, self.project, 1, self.good, self.limits
        )
        self.assertFalse(verified["passed"])
        self.assertIn("validation_receipt_old_version", self.codes(verified))

        changed = self.good.replace("铜屑", "铁屑")
        verified = chapter_validator.verify_validation_receipt(
            receipt, self.project, 1, changed, self.limits
        )
        self.assertFalse(verified["passed"])
        self.assertIn("validation_receipt_text_mismatch", self.codes(verified))


if __name__ == "__main__":
    unittest.main()
