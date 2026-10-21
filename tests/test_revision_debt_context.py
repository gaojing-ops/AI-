import copy
import unittest

import commercial_reviewer as reviewer


class RevisionDebtContextTests(unittest.TestCase):
    @staticmethod
    def ledger(count=17):
        return {"items": [
            {"id": f"debt_{n:04d}", "status": "OPEN", "opened_chapter": 1,
             "due_by": n + 10, "action": f"第{n}项：兑现完整的行动和代价。"}
            for n in range(1, count + 1)
        ]}

    def test_all_seventeen_fit_without_twelve_item_cap(self):
        data = self.ledger()
        result = reviewer.select_revision_debt_context(data, 90)
        self.assertTrue(result["full_coverage"])
        self.assertEqual(result["selected_count"], 17)
        self.assertEqual(result["omitted_ids"], [])
        self.assertLessEqual(len(result["text"]), 2600)
        for item in data["items"]:
            self.assertIn(item["action"], result["text"])

    def test_review_horizon_is_not_relabelled_as_action_deadline(self):
        data = {"items": [{"id": "debt_window", "status": "OPEN", "opened_chapter": 84,
                           "due_by": 94, "action": "第85—87章完成闭环；第87章必须给出名单反馈。"}]}
        before = copy.deepcopy(data)
        rendered = reviewer.select_revision_debt_context(data, 89)["text"]
        self.assertIn("复核点第94章", rendered)
        self.assertIn("未到复核点", rendered)
        self.assertIn("不是原行动的延期许可", rendered)
        self.assertIn(data["items"][0]["action"], rendered)
        self.assertNotIn("最迟第94章", rendered)
        self.assertNotIn("｜未到期", rendered)
        self.assertEqual(before, data)

    def test_budget_never_cuts_a_debt_action(self):
        data = self.ledger(3)
        data["items"][1]["action"] = "独立来源必须完整保留。" * 300
        result = reviewer.select_revision_debt_context(data, 90, 600)
        self.assertEqual(result["selected_ids"], ["debt_0001"])
        self.assertEqual(result["omitted_ids"], ["debt_0002", "debt_0003"])
        self.assertFalse(result["full_coverage"])
        self.assertNotIn("独立来源必须", result["text"])
        self.assertIn("预算省略2项", result["text"])
        self.assertIn("debt_0002,debt_0003", result["text"])
        self.assertLessEqual(len(result["text"]), 600)

    def test_small_budget_fails_instead_of_truncating(self):
        with self.assertRaisesRegex(reviewer.RevisionDebtError, "预算不足"):
            reviewer.render_revision_debt(self.ledger(), 90, 40)

    def test_future_and_closed_items_remain_in_input_not_context(self):
        data = self.ledger(3)
        data["items"][1]["opened_chapter"] = 100
        data["items"][2]["status"] = "VERIFIED"
        before = copy.deepcopy(data)
        result = reviewer.select_revision_debt_context(data, 90)
        self.assertEqual(data, before)
        self.assertEqual(result["selected_ids"], ["debt_0001"])
        self.assertNotIn("debt_0002", result["text"])
        self.assertNotIn("debt_0003", result["text"])

    def test_priority_and_hash_are_deterministic_without_mutation(self):
        data = self.ledger(3)
        data["items"].reverse()
        before = copy.deepcopy(data)
        first = reviewer.select_revision_debt_context(data, 90)
        second = reviewer.select_revision_debt_context(data, 90)
        self.assertEqual(first, second)
        self.assertEqual(first["selected_ids"], ["debt_0001", "debt_0002", "debt_0003"])
        self.assertEqual(len(first["source_sha256"]), 64)
        data["items"][0]["action"] += "来源变更"
        self.assertNotEqual(first["source_sha256"],
                            reviewer.select_revision_debt_context(data, 90)["source_sha256"])
        self.assertEqual(before["items"][0]["id"], "debt_0003")

    def test_empty_context_has_no_header_and_no_omissions(self):
        self.assertEqual(reviewer.render_revision_debt(None), "")
        result = reviewer.select_revision_debt_context({"items": []})
        self.assertTrue(result["full_coverage"])
        self.assertEqual(result["text"], "")

    def test_invalid_character_budget_is_rejected(self):
        for budget in (True, 0, -1, 128001, "2600", 3.5):
            with self.subTest(budget=budget), self.assertRaises(reviewer.RevisionDebtError):
                reviewer.select_revision_debt_context(self.ledger(), max_chars=budget)


if __name__ == "__main__":
    unittest.main()
