"""Model-free regression tests for per-debt evidence attribution and history."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import commercial_reviewer


class RevisionDebtAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "revision_debt.json"

    def _seed(self, count=3):
        items = [{
            "id": f"debt_{number:04d}", "action": f"完成独立任务{number}", "status": "OPEN",
            "opened_chapter": 1, "due_by": 11, "last_reviewed_chapter": 2,
            "last_review_evidence": [f"debt_{number:04d}|OPEN|第2章该任务仍待完成"],
            "repeat_count": 1, "last_seen_chapter": 1,
        } for number in range(1, count + 1)]
        row = {"schema_version": commercial_reviewer.REVISION_DEBT_SCHEMA_VERSION,
               "updated_at": "2026-10-01T12:00:00", "last_review_chapter": 2,
               "items": items, "review_history": []}
        self.path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return copy.deepcopy(items)

    @staticmethod
    def _review(evidence, *, review_id="review_attribution", debt_status="RESOLVED"):
        return {"review_id": review_id, "status": "WARN", "current": "PASS", "action": "ADJUST",
                "debt_status": debt_status, "debt_evidence": list(evidence),
                "next_actions": [], "reviewed_chapters": list(range(1, 11))}

    def _update(self, evidence, **kwargs):
        return commercial_reviewer.update_revision_debt(
            str(self.path), 10, self._review(evidence, **kwargs), horizon=10
        )

    @staticmethod
    def _by_id(data):
        return {item["id"]: item for item in data["items"]}

    def test_open_items_store_only_their_own_explicit_evidence(self):
        self._seed()
        first = "debt_0001|OPEN|第8章任务一仍未完成"
        first_more = "debt_0001|OPEN|第9章任务一继续等待实际动作"
        second = "debt_0002|OPEN|第8章任务二有独立缺口"
        evidence = [first, second, "通用说明：本轮输出完整", first_more,
                    "debt_foreign|VERIFIED|不属于已登记任务"]
        result = self._update(evidence)
        items = self._by_id(result)
        self.assertEqual([first, first_more], items["debt_0001"]["last_review_evidence"])
        self.assertEqual([second], items["debt_0002"]["last_review_evidence"])
        self.assertEqual(10, items["debt_0001"]["last_reviewed_chapter"])
        self.assertEqual(10, items["debt_0002"]["last_reviewed_chapter"])
        self.assertEqual("OPEN", items["debt_0001"]["status"])
        self.assertEqual("OPEN", items["debt_0002"]["status"])
        self.assertEqual(evidence, result["review_history"][-1]["evidence"])

    def test_uncovered_id_preserves_all_previous_review_fields(self):
        before = self._seed()
        evidence = ["debt_0001|VERIFIED|第8章任务一已有完整正文依据"]
        result = self._update(evidence)
        items = self._by_id(result)
        self.assertEqual("VERIFIED", items["debt_0001"]["status"])
        self.assertEqual(10, items["debt_0001"]["closed_chapter"])
        self.assertEqual(before[1], items["debt_0002"])
        self.assertEqual(before[2], items["debt_0003"])

    def test_legacy_mentions_wrong_status_or_empty_proof_do_not_cover_id(self):
        before = self._seed()
        evidence = [
            "debt_0001|OPEN|任务一仍需正文兑现",
            "debt_0002：尚未到核销区间",
            "debt_0002|RESOLVED|这不是允许的逐项裁决",
            "debt_0002|VERIFIED|   ",
            "debt_0001|OPEN|正文提到debt_0002但不是对它的裁决",
        ]
        result = self._update(evidence)
        items = self._by_id(result)
        self.assertEqual(before[1], items["debt_0002"])
        self.assertEqual(before[2], items["debt_0003"])
        self.assertEqual([evidence[0], evidence[4]], items["debt_0001"]["last_review_evidence"])

    def test_history_retains_all_forty_in_scope_evidence_rows(self):
        self._seed(40)
        evidence = [f"debt_{number:04d}|OPEN|第9章任务{number}仍有独立缺口"
                    for number in range(1, 41)]
        result = self._update(evidence)
        self.assertEqual(evidence, result["review_history"][-1]["evidence"])
        items = self._by_id(result)
        for number in range(1, 41):
            with self.subTest(number=number):
                item = items[f"debt_{number:04d}"]
                self.assertEqual([evidence[number - 1]], item["last_review_evidence"])
                self.assertEqual(10, item["last_reviewed_chapter"])
                self.assertEqual("OPEN", item["status"])

    def test_after_eighth_id_verified_proof_is_preserved_and_attributed(self):
        self._seed(12)
        evidence = [f"debt_{number:04d}|OPEN|任务{number}尚待完整依据"
                    for number in range(1, 9)]
        evidence += [f"debt_{number:04d}|VERIFIED|第9章已兑现任务{number}"
                     for number in range(9, 13)]
        result = self._update(evidence)
        items = self._by_id(result)
        self.assertEqual(evidence, result["review_history"][-1]["evidence"])
        for number in range(9, 13):
            with self.subTest(number=number):
                self.assertEqual("VERIFIED", items[f"debt_{number:04d}"]["status"])
                self.assertEqual([evidence[number - 1]], items[f"debt_{number:04d}"]["last_review_evidence"])

    def test_resolved_output_marker_alone_does_not_close_or_touch_items(self):
        before = self._seed()
        result = self._update(["本轮DEBT_STATUS为RESOLVED，仅说明输出完整"])
        self.assertEqual(before, result["items"])
        self.assertEqual("RESOLVED", result["review_history"][-1]["debt_status"])

    def test_open_evidence_stays_open_despite_resolved_output_marker(self):
        self._seed(1)
        evidence = ["debt_0001|OPEN|已核对正文，但实际行动尚未完成"]
        result = self._update(evidence)
        item = result["items"][0]
        self.assertEqual("OPEN", item["status"])
        self.assertNotIn("closed_chapter", item)
        self.assertEqual(evidence, item["last_review_evidence"])

    def test_partial_output_cannot_close_even_with_explicit_verified_proof(self):
        self._seed(1)
        evidence = ["debt_0001|VERIFIED|第9章出现该任务的实际兑现"]
        result = self._update(evidence, debt_status="PARTIAL")
        item = result["items"][0]
        self.assertEqual("OPEN", item["status"])
        self.assertNotIn("closed_chapter", item)
        self.assertEqual(evidence, item["last_review_evidence"])

    def test_duplicate_review_id_is_byte_idempotent_even_with_different_later_payload(self):
        self._seed()
        first = self._update(["debt_0001|OPEN|任务一尚待实际兑现"], review_id="same_review")
        first_bytes = self.path.read_bytes()
        conflicting = self._review(["debt_0002|VERIFIED|另一轮不同的证据"], review_id="same_review")
        result = commercial_reviewer.update_revision_debt(str(self.path), 100, conflicting)
        self.assertEqual(first, result)
        self.assertEqual(first_bytes, self.path.read_bytes())
        self.assertEqual(1, len(result["review_history"]))
        self.assertEqual(10, result["last_review_chapter"])

    def test_json_parser_to_ledger_preserves_forty_evidence_rows(self):
        self._seed(40)
        evidence = [f"debt_{number:04d}|OPEN|任务{number}仍待正文核销"
                    for number in range(1, 41)]
        raw = json.dumps({"OVERALL": "WARN", "CURRENT": "PASS", "ACTION": "ADJUST",
                          "SUMMARY": "逐项完整核验", "DEBT_STATUS": "RESOLVED",
                          "DEBT_EVIDENCE": evidence, "NEXT_10_CHAPTERS": []}, ensure_ascii=False)
        parsed = commercial_reviewer.parse_review(raw)
        self.assertFalse(parsed["malformed"])
        self.assertEqual(evidence, parsed["debt_evidence"])
        result = commercial_reviewer.update_revision_debt(str(self.path), 10, parsed)
        self.assertEqual(evidence, result["review_history"][-1]["evidence"])
        self.assertEqual([evidence[-1]], self._by_id(result)["debt_0040"]["last_review_evidence"])

    def test_text_parser_to_ledger_preserves_after_eighth_id_verified_proof(self):
        self._seed(12)
        evidence = [f"debt_{number:04d}|VERIFIED|第9章已兑现独立任务{number}"
                    for number in range(1, 13)]
        raw = ("OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\nSUMMARY: 逐项核验完整\n"
               "DEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\n" + "\n".join(f"- {row}" for row in evidence))
        parsed = commercial_reviewer.parse_review(raw)
        self.assertFalse(parsed["malformed"])
        self.assertEqual(evidence, parsed["debt_evidence"])
        result = commercial_reviewer.update_revision_debt(str(self.path), 10, parsed)
        self.assertEqual(evidence, result["review_history"][-1]["evidence"])
        self.assertEqual("VERIFIED", self._by_id(result)["debt_0012"]["status"])

    def test_more_than_forty_evidence_rows_are_rejected_without_ledger_changes(self):
        self._seed()
        before = self.path.read_bytes()
        evidence = [f"debt_{number:04d}|OPEN|任务{number}仍未完成" for number in range(1, 42)]
        with self.assertRaises(commercial_reviewer.RevisionDebtError):
            self._update(evidence)
        self.assertEqual(before, self.path.read_bytes())

    def test_malformed_review_cannot_mutate_ledger(self):
        self._seed()
        before = self.path.read_bytes()
        result = self._review(["debt_0001|VERIFIED|该项的测试证据"])
        result["malformed"] = True
        with self.assertRaises(commercial_reviewer.RevisionDebtError):
            commercial_reviewer.update_revision_debt(str(self.path), 10, result)
        self.assertEqual(before, self.path.read_bytes())

    def test_future_debt_is_not_reviewed_before_its_opened_chapter(self):
        self._seed(1)
        data = commercial_reviewer.load_revision_debt(str(self.path))
        data["items"][0]["opened_chapter"] = 12
        data["items"][0]["due_by"] = 22
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = copy.deepcopy(data["items"][0])
        result = self._update(["debt_0001|VERIFIED|不能提前核销未来任务"])
        self.assertEqual(before, result["items"][0])

    def test_render_hides_future_debt_without_deleting_or_mutating_it(self):
        self._seed()
        data = commercial_reviewer.load_revision_debt(str(self.path))
        data["items"][2]["opened_chapter"] = 12
        data["items"][2]["due_by"] = 22
        before = copy.deepcopy(data)
        before_bytes = self.path.read_bytes()
        rendered = commercial_reviewer.render_revision_debt(data, current_chapter=10)
        self.assertIn("debt_0001", rendered)
        self.assertIn("debt_0002", rendered)
        self.assertNotIn("debt_0003", rendered)
        self.assertIn("debt_0003", commercial_reviewer.render_revision_debt(data, current_chapter=0))
        self.assertEqual(before, data)
        self.assertEqual(before_bytes, self.path.read_bytes())

    @staticmethod
    def _gate_debt():
        return {"items": [
            {"id": "debt_0001", "status": "OPEN", "opened_chapter": 1, "due_by": 8},
            {"id": "debt_0002", "status": "OPEN", "opened_chapter": 2, "due_by": 10},
            {"id": "debt_future", "status": "OPEN", "opened_chapter": 12, "due_by": 22},
        ]}

    def _gate_blocked(self, evidence, **updates):
        result = self._review(evidence)
        result.update(updates)
        return commercial_reviewer.hard_gate_debt_blocked(
            10, result, self._gate_debt(), {"commercial_hard_gate_chapters": [10]}
        )

    def test_hard_gate_requires_own_verified_proof_for_every_due_open_id(self):
        first = "debt_0001|VERIFIED|第9章实际完成任务一"
        second = "debt_0002|VERIFIED|第10章实际完成任务二"
        self.assertTrue(self._gate_blocked([]))
        self.assertTrue(self._gate_blocked(["RESOLVED仅输出完整"]))
        self.assertTrue(self._gate_blocked([first]))
        self.assertFalse(self._gate_blocked([first, second]))

    def test_hard_gate_open_empty_or_other_id_evidence_cannot_verify_due_item(self):
        first = "debt_0001|VERIFIED|第9章实际完成任务一"
        for second in ("debt_0002|OPEN|任务二尚待完成", "debt_0002|VERIFIED|   ",
                       "debt_foreign|VERIFIED|此句提到debt_0002但不是它的裁决"):
            with self.subTest(second=second):
                self.assertTrue(self._gate_blocked([first, second]))

    def test_hard_gate_cannot_accept_malformed_partial_or_overflow_review(self):
        evidence = ["debt_0001|VERIFIED|第9章完成任务一", "debt_0002|VERIFIED|第10章完成任务二"]
        self.assertTrue(self._gate_blocked(evidence, malformed=True))
        self.assertTrue(self._gate_blocked(evidence, debt_status="PARTIAL"))
        self.assertTrue(self._gate_blocked(evidence + [f"diagnostic_{n}" for n in range(39)]))

    def test_future_opened_or_not_yet_due_items_do_not_block_current_gate(self):
        data = {"items": [
            {"id": "debt_future", "status": "OPEN", "opened_chapter": 12, "due_by": 22},
            {"id": "debt_later", "status": "OPEN", "opened_chapter": 4, "due_by": 11},
        ]}
        self.assertFalse(commercial_reviewer.hard_gate_debt_blocked(
            10, {"debt_status": "PARTIAL"}, data, {"commercial_hard_gate_chapters": [10]}
        ))

    def test_debt_opened_on_current_gate_still_needs_proof_if_immediately_due(self):
        debt = {"items": [{"id": "debt_now", "status": "OPEN", "opened_chapter": 10, "due_by": 10}]}
        config = {"commercial_hard_gate_chapters": [10]}
        self.assertTrue(commercial_reviewer.hard_gate_debt_blocked(
            10, {"debt_status": "RESOLVED"}, debt, config
        ))
        self.assertFalse(commercial_reviewer.hard_gate_debt_blocked(
            10, {"debt_status": "RESOLVED", "debt_evidence": ["debt_now|VERIFIED|本章已实际兑现"]},
            debt, config
        ))

    def test_conflicting_own_open_and_verified_evidence_blocks_gate_and_ledger_write(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self._seed(2)
                before = self.path.read_bytes()
                contradiction = ["debt_0002|OPEN|任务二仍缺必要行动",
                                 "debt_0002|VERIFIED|另一行却声称任务二已完成"]
                if reverse:
                    contradiction.reverse()
                evidence = ["debt_0001|VERIFIED|任务一实际完成", *contradiction]
                self.assertTrue(self._gate_blocked(evidence))
                with self.assertRaisesRegex(commercial_reviewer.RevisionDebtError, "冲突"):
                    self._update(evidence)
                self.assertEqual(before, self.path.read_bytes())

    def test_next_actions_exact_and_semantic_dedup_do_not_modify_future_debt(self):
        future_action = "第88—97章让周予安在右侧完成一次实际接应并在受压后将球回传给杨铭"
        semantic_action = "第88-97章让周予安在右侧完成一次真实接应并在受压后将球回传给杨铭"
        self.assertTrue(commercial_reviewer._actions_semantically_duplicate(future_action, semantic_action))
        for action in (future_action, semantic_action):
            with self.subTest(action=action):
                self._seed(1)
                data = commercial_reviewer.load_revision_debt(str(self.path))
                future = data["items"][0]
                future.update(action=future_action, opened_chapter=87, due_by=97,
                              last_reviewed_chapter=85, last_seen_chapter=85, repeat_count=4)
                self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                before = copy.deepcopy(future)
                review = self._review(["首次建立"], review_id="chapter_3_action")
                review["next_actions"] = [action]
                result = commercial_reviewer.update_revision_debt(str(self.path), 3, review)
                self.assertEqual(before, self._by_id(result)[before["id"]])
                current = [item for item in result["items"] if item["opened_chapter"] == 3]
                self.assertEqual(1, len(current))
                self.assertEqual(action, current[0]["action"])
                self.assertEqual(1, current[0]["repeat_count"])
                self.assertEqual(3, current[0]["last_seen_chapter"])


if __name__ == "__main__":
    unittest.main()
