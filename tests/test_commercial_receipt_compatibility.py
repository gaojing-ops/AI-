"""Offline receipts: preserve old parsing semantics without weakening hashes.

All receipts in this file are synthetic fixtures. No model is called and no
existing project receipt is modified or manufactured by the tests.
"""

from __future__ import annotations

import copy
import json
import unittest

import commercial_reviewer


class CommercialReceiptCompatibilityTests(unittest.TestCase):
    MODEL = "offline-commercial-review-test"

    @staticmethod
    def _raw(count=12, *, json_format=False):
        evidence = [f"debt_{number:04d}|OPEN|任务{number}仍等待独立正文证据"
                    for number in range(1, count + 1)]
        if json_format:
            return json.dumps({
                "OVERALL": "PASS", "CURRENT": "PASS", "ACTION": "CONTINUE",
                "SUMMARY": "离线证据兼容测试", "DEBT_STATUS": "RESOLVED",
                "DEBT_EVIDENCE": evidence, "NEXT_10_CHAPTERS": [],
            }, ensure_ascii=False)
        return ("OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
                "SUMMARY: 离线证据兼容测试\nDEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n"
                + "\n".join(f"- {row}" for row in evidence))

    def _result(self, version, *, count=12, json_format=False, map_count=None):
        raw = self._raw(count, json_format=json_format)
        result = commercial_reviewer.parse_review(raw)
        if version in (1, 2):
            # Explicitly reconstruct the historical eight-row test fixture,
            # not an update to a persisted receipt or its accepted conclusion.
            result.update(status="PASS", current="PASS", action="CONTINUE", malformed=False)
            result["debt_evidence"] = result["debt_evidence"][:8]
        result.update(reviewed_chapters=[1], chapter_hashes={"1": "1" * 64})
        if version in (1, 3):
            receipt = commercial_reviewer.build_review_receipt(
                "test system", "test user", raw, self.MODEL, [1], result["chapter_hashes"]
            )
        else:
            map_raw = self._raw(count if map_count is None else map_count, json_format=json_format)
            stages = [
                commercial_reviewer._review_stage_receipt(
                    "map", "test map system", "test map user", map_raw, self.MODEL, [1]
                ),
                commercial_reviewer._review_stage_receipt(
                    "reduce", "test reduce system", "test reduce user", raw, self.MODEL, [1]
                ),
            ]
            receipt = commercial_reviewer.build_multi_review_receipt(
                self.MODEL, [1], stages, result["chapter_hashes"]
            )
            result["map_reviews"] = [{"reviewed_chapters": [1], "raw": map_raw,
                                      "status": "PASS", "current": "PASS", "action": "CONTINUE"}]
        receipt["version"] = version
        receipt["receipt_sha256"] = commercial_reviewer._receipt_digest(receipt)
        result["review_receipt"] = receipt
        return result

    def test_new_receipt_builders_emit_single3_and_multi4(self):
        raw = self._raw()
        self.assertEqual(3, commercial_reviewer.build_review_receipt(
            "sys", "user", raw, self.MODEL, [1], {"1": "1" * 64})["version"])
        self.assertEqual(4, commercial_reviewer.build_multi_review_receipt(
            self.MODEL, [1], [], {"1": "1" * 64})["version"])

    def test_legacy_single1_and_multi2_verify_eight_row_projection_without_mutation(self):
        for version in (1, 2):
            for json_format in (False, True):
                with self.subTest(version=version, json_format=json_format):
                    result = self._result(version, json_format=json_format)
                    before = copy.deepcopy(result)
                    self.assertEqual(8, len(result["debt_evidence"]))
                    self.assertEqual(12, len(commercial_reviewer.parse_review(result["raw"])["debt_evidence"]))
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)
                    self.assertEqual(before, result)

    def test_new_single3_and_multi4_verify_complete_evidence(self):
        for version in (3, 4):
            for json_format in (False, True):
                with self.subTest(version=version, json_format=json_format):
                    result = self._result(version, count=40, json_format=json_format)
                    self.assertEqual(40, len(result["debt_evidence"]))
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_new_receipts_refuse_legacy_truncated_result(self):
        for version in (3, 4):
            with self.subTest(version=version):
                result = self._result(version)
                result["debt_evidence"] = result["debt_evidence"][:8]
                with self.assertRaisesRegex(RuntimeError, "债务字段"):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_legacy_receipts_cannot_claim_previously_dropped_evidence(self):
        for version in (1, 2):
            with self.subTest(version=version):
                result = self._result(version)
                result["debt_evidence"] = commercial_reviewer.parse_review(result["raw"])["debt_evidence"]
                with self.assertRaisesRegex(RuntimeError, "债务字段"):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_all_versions_still_refuse_raw_tampering(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                result = self._result(version)
                result["raw"] += "\nRISKS:\n- 追加未经绑定的结论"
                with self.assertRaises(RuntimeError):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_all_versions_still_refuse_model_mismatch(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                with self.assertRaisesRegex(RuntimeError, "模型收据"):
                    commercial_reviewer.verify_review_receipt(self._result(version), model_name="different-model")

    def test_all_versions_still_refuse_changed_chapter_hashes(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                result = self._result(version)
                result["chapter_hashes"] = {"1": "2" * 64}
                with self.assertRaisesRegex(RuntimeError, "正文哈希"):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_all_versions_still_refuse_missing_hash_coverage(self):
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                result = self._result(version)
                result["chapter_hashes"] = {}
                with self.assertRaisesRegex(RuntimeError, "逐章正文哈希"):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_receipt_version_change_without_resealing_is_rejected(self):
        for version, legacy in ((3, 1), (4, 2)):
            with self.subTest(version=version):
                result = self._result(version)
                result["review_receipt"]["version"] = legacy
                result["debt_evidence"] = result["debt_evidence"][:8]
                with self.assertRaisesRegex(RuntimeError, "调用收据已被修改"):
                    commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_parser_overflow_is_explicit_and_keeps_complete_diagnostic_evidence(self):
        for json_format in (False, True):
            with self.subTest(json_format=json_format):
                parsed = commercial_reviewer.parse_review(self._raw(41, json_format=json_format))
                self.assertTrue(parsed["malformed"])
                self.assertEqual("FAIL", parsed["status"])
                self.assertEqual("PAUSE", parsed["action"])
                self.assertEqual(41, len(parsed["debt_evidence"]))

    def test_new_multi_receipt_refuses_overflow_in_map_before_reduce_pass(self):
        result = self._result(4, count=12, map_count=41)
        with self.assertRaises(RuntimeError):
            commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)

    def test_legacy_multi_receipt_keeps_historical_map_semantics_without_rewriting(self):
        result = self._result(2, count=12, map_count=41)
        before = copy.deepcopy(result)
        commercial_reviewer.verify_review_receipt(result, model_name=self.MODEL)
        self.assertEqual(before, result)


if __name__ == "__main__":
    unittest.main()
