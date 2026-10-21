"""Ordinary deterministic resource gates; no models, sorting, or old ledgers."""

from __future__ import annotations

import copy
import math
import unittest

import state_ledger
import state_repair


class ResourceSequenceTests(unittest.TestCase):
    GAIN = "林舟获得100次尝试和3个片段。"
    USE = "林舟选择1个片段，用了18次尝试，还剩82次和2个片段。"
    LOSE = "林舟关闭入口，82次未用尝试和2个空位一并失效。"

    @staticmethod
    def _row(action, quote, *, item="本场尝试", owner="林舟", quantity=None, change=None):
        return {"owner": owner, "item": item, "action": action, "quantity": quantity,
                "quantity_change": change, "status": "", "evidence_quote": quote}

    def _chapter(self, *quotes):
        return "第1章 资源\n\n" + "\n\n".join(quotes or (self.GAIN, self.USE, self.LOSE))

    def _validate(self, rows, text=None, state=None):
        text = text or self._chapter()
        event_quote = text.split("\n\n", 1)[1].split("\n\n", 1)[0]
        payload = {"chapter": 1, "resources": copy.deepcopy(rows), "events": [
            {"event_type": "resource", "summary": "林舟记录本章资源变化", "evidence_quote": event_quote}
        ]}
        before = copy.deepcopy(payload)
        normalized, issues = state_ledger.validate_delta(payload, 1, text, state or state_ledger.initial_state())
        self.assertEqual(before, payload)
        return payload, normalized, issues

    def _attempts(self):
        return [self._row("GAIN", self.GAIN, quantity=100, change=100),
                self._row("USE", self.USE, quantity=82, change=-18),
                self._row("LOSE", self.LOSE, quantity=0, change=-82)]

    def _paths(self, issues):
        return {issue.split("]", 1)[0] + "]" for issue in issues if "资源数量链矛盾" in issue}

    def test_wrong_use_lose_gain_order_is_not_pass_and_all_related_rows_are_repairable(self):
        correct = self._attempts()
        rows = [correct[1], correct[2], correct[0]]
        payload, normalized, issues = self._validate(rows)
        self.assertEqual({"resources[0]", "resources[1]", "resources[2]"}, self._paths(issues))
        self.assertEqual(rows, normalized["resources"])
        self.assertTrue(any("数组顺序" in issue for issue in issues))
        permissions = state_repair.plan(payload, issues, expected_chapter=1)
        self.assertEqual(["resources[0]", "resources[1]", "resources[2]"], permissions["editable"])
        self.assertIn("resources", permissions["append"])

    def test_gain_use_lose_passes_and_replay_ends_at_zero(self):
        _payload, normalized, issues = self._validate(self._attempts())
        self.assertEqual([], issues)
        replayed = state_ledger._apply_delta_to_state(state_ledger.initial_state(), normalized, "a" * 64)
        self.assertEqual(0, next(iter(replayed["resources"].values()))["quantity"])

    def test_inversion_is_rejected_even_when_last_explicit_zero_hides_bad_array_order(self):
        correct = self._attempts()
        _payload, _normalized, issues = self._validate([correct[1], correct[0], correct[2]])
        self.assertEqual({"resources[0]", "resources[1]", "resources[2]"}, self._paths(issues))

    def test_missing_selected_fragment_use_produces_related_numeric_chain_diagnostics(self):
        rows = [self._row("GAIN", self.GAIN, item="本场片段", quantity=3, change=3),
                self._row("LOSE", self.LOSE, item="本场片段", quantity=0, change=-2)]
        _payload, _normalized, issues = self._validate(rows)
        self.assertEqual({"resources[0]", "resources[1]"}, self._paths(issues))
        self.assertTrue(any("quantity_change与quantity不一致" in issue for issue in issues))

    def test_complete_fragment_gain3_use1_lose2_passes(self):
        rows = [self._row("GAIN", self.GAIN, item="本场片段", quantity=3, change=3),
                self._row("USE", self.USE, item="本场片段", quantity=2, change=-1),
                self._row("LOSE", self.LOSE, item="本场片段", quantity=0, change=-2)]
        _payload, normalized, issues = self._validate(rows)
        self.assertEqual([], issues)
        state = state_ledger._apply_delta_to_state(state_ledger.initial_state(), normalized, "b" * 64)
        self.assertEqual(0, next(iter(state["resources"].values()))["quantity"])

    def test_local_explicit_change_and_explicit_balance_must_agree(self):
        wrong_use = "林舟用了18次尝试，还剩90次。"
        rows = [self._row("GAIN", self.GAIN, quantity=100, change=100),
                self._row("USE", wrong_use, quantity=90, change=-18)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(self.GAIN, wrong_use))
        self.assertEqual({"resources[0]", "resources[1]"}, self._paths(issues))

    def test_local_explicit_consumption_cannot_overdraw_without_using_old_state(self):
        gained = "林舟获得2枚钥匙。"
        used = "林舟用掉3枚钥匙。"
        rows = [self._row("GAIN", gained, item="钥匙", quantity=2, change=2),
                self._row("USE", used, item="钥匙", quantity=None, change=-3)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(gained, used))
        self.assertEqual({"resources[0]", "resources[1]"}, self._paths(issues))
        self.assertTrue(any("负余额" in issue for issue in issues))

    def test_different_items_or_owners_are_not_globally_sorted(self):
        later = "林舟获得3枚徽章，杨铭也获得3枚徽章。"
        earlier = "林舟获得2枚钥匙。"
        rows = [self._row("GAIN", later, item="徽章", quantity=3, change=3),
                self._row("GAIN", earlier, item="钥匙", quantity=2, change=2),
                self._row("GAIN", later, item="徽章", owner="杨铭", quantity=3, change=3)]
        _payload, normalized, issues = self._validate(rows, self._chapter(earlier, later))
        self.assertEqual([], issues)
        self.assertEqual(rows, normalized["resources"])

    def test_shared_unique_quote_has_no_invented_within_quote_order(self):
        quote = "林舟获得3枚钥匙，当场用去1枚，剩2枚。"
        rows = [self._row("USE", quote, item="钥匙", quantity=2, change=-1),
                self._row("GAIN", quote, item="钥匙", quantity=3, change=3)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(quote))
        self.assertEqual([], issues)

    def test_repeated_quote_leaves_ambiguous_chronology_to_independent_review(self):
        rows = [self._attempts()[1], self._attempts()[0], self._attempts()[2]]
        _payload, _normalized, issues = self._validate(rows, self._chapter(self.GAIN, self.USE, self.GAIN, self.LOSE))
        self.assertEqual([], issues)

    def test_null_precision_is_not_filled_or_compared_against_guessed_values(self):
        gained = "林舟收到了新的尝试额度。"
        used = "林舟用了其中一部分，随后合上本子。"
        rows = [self._row("USE", used), self._row("GAIN", gained)]
        _payload, normalized, issues = self._validate(rows, self._chapter(gained, used))
        self.assertEqual([], issues)
        self.assertTrue(all(row["quantity"] is None and row["quantity_change"] is None for row in normalized["resources"]))

    def test_explicit_post_balance_is_authoritative_when_current_change_is_not_quoted(self):
        gained = "林舟的尝试次数还剩55次。"
        later = "经过中间多次使用，林舟的尝试次数还剩0次。"
        rows = [self._row("SET", gained, quantity=55),
                self._row("USE", later, quantity=0, change=-9)]
        _payload, normalized, issues = self._validate(rows, self._chapter(gained, later))
        self.assertEqual([], issues)
        state = state_ledger._apply_delta_to_state(state_ledger.initial_state(), normalized, "c" * 64)
        self.assertEqual(0, next(iter(state["resources"].values()))["quantity"])

    def test_single_sparse_explicit_balance_does_not_consume_same_named_old_match_quota(self):
        state = state_ledger.initial_state()
        state["resources"] = {"old": {"owner": "林舟", "item": "本场尝试", "quantity": 52,
                                           "status": "上一场旧额度", "last_changed_chapter": 89}}
        quote = "林舟这一场用了9次尝试，本场剩余0次。"
        row = self._row("USE", quote, quantity=0, change=-9)
        before = copy.deepcopy(state)
        _payload, _normalized, issues = self._validate([row], self._chapter(quote), state)
        self.assertEqual([], issues)
        self.assertEqual(before, state)

    def test_set_and_explicit_gain_reset_anchors_do_not_add_old_batch_balances(self):
        quotes = ["林舟旧窗口剩82次。", "新窗口开启，林舟获得100次新额度。", "林舟用了18次，还剩82次。"]
        rows = [self._row("SET", quotes[0], quantity=82),
                self._row("GAIN", quotes[1], quantity=100, change=100),
                self._row("USE", quotes[2], quantity=82, change=-18)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(*quotes))
        self.assertEqual([], issues)

    def test_multiple_bad_groups_are_collected_and_unrelated_rows_remain_uneditable(self):
        attempts = self._attempts()
        rows = [attempts[1], attempts[2], attempts[0],
                self._row("GAIN", self.GAIN, item="本场片段", quantity=3, change=3),
                self._row("LOSE", self.LOSE, item="本场片段", quantity=0, change=-2),
                self._row("GAIN", "林舟拿到1张通知。", item="通知", quantity=1, change=1)]
        payload, _normalized, issues = self._validate(rows, self._chapter(self.GAIN, self.USE, self.LOSE, "林舟拿到1张通知。"))
        self.assertEqual({f"resources[{i}]" for i in range(5)}, self._paths(issues))
        permissions = state_repair.plan(payload, issues, expected_chapter=1)
        self.assertNotIn("resources[5]", permissions["editable"])

    def test_chinese_explicit_numbers_are_supported_without_inventing_a_balance(self):
        gained = "林舟获得一百次尝试。"
        used = "林舟用了十八次，还剩八十二次。"
        lost = "林舟关闭入口，剩余八十二次全部失效。"
        rows = [self._row("USE", used, quantity=82, change=-18),
                self._row("LOSE", lost, quantity=0, change=-82),
                self._row("GAIN", gained, quantity=100, change=100)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(gained, used, lost))
        self.assertEqual({"resources[0]", "resources[1]", "resources[2]"}, self._paths(issues))

    def test_finite_decimal_arithmetic_tolerates_binary_rounding(self):
        gained = "林舟取得0.3单位药剂。"
        used = "林舟用去0.1单位药剂，还剩0.2单位。"
        rows = [self._row("GAIN", gained, item="药剂", quantity=0.3, change=0.3),
                self._row("USE", used, item="药剂", quantity=0.2, change=-0.1)]
        _payload, _normalized, issues = self._validate(rows, self._chapter(gained, used))
        self.assertEqual([], issues)

    def test_bad_quantity_schema_is_still_fail_closed_and_does_not_shift_repair_indexes(self):
        for field in ("quantity", "quantity_change"):
            for value in (True, "18", math.nan, math.inf):
                with self.subTest(field=field, value=value):
                    row = self._attempts()[0]
                    row[field] = value
                    _payload, _normalized, issues = self._validate([row])
                    self.assertTrue(any(issue.startswith(f"resources[0].{field}") for issue in issues))
        payload = {"chapter": 1, "resources": [None], "events": [{
            "event_type": "resource", "summary": "记录资源", "evidence_quote": self.GAIN}]}
        _normalized, issues = state_ledger.validate_delta(payload, 1, self._chapter())
        self.assertIn("resources[0] 必须是对象", issues)


if __name__ == "__main__":
    unittest.main()
