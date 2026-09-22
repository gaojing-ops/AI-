import json
import unittest
from copy import deepcopy

import state_repair
from headless_app import describe_preflight_progress


class StateRepairTests(unittest.TestCase):
    def setUp(self):
        self.base = {"chapter": 75, "characters": [
            {"name": "周予安", "location": "回家路上的车内", "knowledge_add": ["先告知"],
             "evidence_quote": "周予安上车。"},
            {"name": "周建成", "location": "车内", "evidence_quote": "周建成在车内。"}],
            "events": [{"summary": "上车", "evidence_quote": "周予安上车。"}],
            "resources": [{"quantity": 0, "item": "尝试"}]}
        self.permissions = state_repair.plan(self.base, ["characters[0] 保存前连续性审计未通过：地点范围"])

    def test_patch_only_changes_target_field_and_never_mutates_base(self):
        before = deepcopy(self.base)
        result = state_repair.apply(self.base, {"updates": {"characters[0]": {"location": "车内"}}}, self.permissions)
        self.assertEqual("车内", result["characters"][0]["location"])
        self.assertEqual(["先告知"], result["characters"][0]["knowledge_add"])
        self.assertEqual(before["events"], result["events"])
        self.assertEqual(before["resources"], result["resources"])
        self.assertEqual(before, self.base)

    def test_patch_cannot_change_or_delete_unrejected_resource(self):
        for patch in ({"updates": {"resources[0]": {"quantity": 100}}}, {"remove": ["resources[0]"]}):
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, "未被拒绝"):
                state_repair.apply(self.base, patch, self.permissions)

    def test_empty_append_categories_are_noops_but_real_unauthorized_rows_fail(self):
        patch = {'updates': {'characters[0]': {'location': '车内'}},
                 'append': {key: [] for key in state_repair.CATEGORIES}}
        result = state_repair.apply(self.base, patch, self.permissions)
        self.assertEqual('车内', result['characters'][0]['location'])
        self.assertEqual(self.base['events'], result['events'])
        self.assertEqual(self.base['resources'], result['resources'])
        for addition in ({'resources': [{}]}, {'unknown': []}, {'events': None}):
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                state_repair.apply(self.base, {'append': addition}, self.permissions)

    def test_legacy_snapshot_is_supported_but_unrelated_changes_rejected(self):
        fixed = deepcopy(self.base)
        fixed["characters"][0]["location"] = "车内"
        self.assertEqual(fixed, state_repair.apply(self.base, fixed, self.permissions))
        fixed["resources"][0]["quantity"] = 100
        with self.assertRaisesRegex(ValueError, "未被拒绝"):
            state_repair.apply(self.base, fixed, self.permissions)

    def test_snapshot_cannot_drop_an_unrelated_event(self):
        fixed = deepcopy(self.base)
        fixed["events"] = []
        with self.assertRaisesRegex(ValueError, "整表删除"):
            state_repair.apply(self.base, fixed, self.permissions)

    def test_missing_category_allows_append_but_preserves_existing(self):
        permissions = state_repair.plan(self.base, ["characters提取遗漏：补新知"])
        row = {"name": "周予安", "knowledge_add": ["收到集合安排"]}
        result = state_repair.apply(self.base, {"append": {"characters": [row]}}, permissions)
        self.assertEqual(self.base["characters"], result["characters"][:-1])
        with self.assertRaises(ValueError):
            state_repair.apply(self.base, {"append": {"events": [{}]}}, permissions)

    def test_explicit_remove_can_only_remove_rejected_row(self):
        result = state_repair.apply(self.base, {"remove": ["characters[0]"]}, self.permissions)
        self.assertEqual([self.base["characters"][1]], result["characters"])
        self.assertEqual(self.base["resources"], result["resources"])

    def test_identical_rejection_and_a_b_a_cycle_stop_across_serialization(self):
        history = state_repair.record([], self.base, ["characters[0] 地点错误"])
        self.assertEqual("", state_repair.stop_reason(history))
        history = json.loads(json.dumps(history))
        other = deepcopy(self.base)
        other["characters"][0]["location"] = "球场"
        history = state_repair.record(history, other, ["characters[0] 地点不符"])
        history = state_repair.record(history, self.base, ["characters[0] 地点错误"])
        self.assertIn("重复", state_repair.stop_reason(history))

    def test_new_errors_cannot_create_unbounded_retries(self):
        history = []
        for i in range(6):
            history = state_repair.record(history, {"attempt": i}, [f"新问题{i}"])
        self.assertIn("累计6轮", state_repair.stop_reason(history))
        self.assertEqual(6, len(history))

    def test_missing_core_event_allows_only_events_append(self):
        base = deepcopy(self.base)
        base['events'] = []
        permissions = state_repair.plan(base, ['events 至少要登记一条本章已发生的核心事件，不能用空增量跳过正史记忆'])
        result = state_repair.apply(base, {'append': {'events': self.base['events']}}, permissions)
        self.assertEqual(self.base, result)
        with self.assertRaises(ValueError):
            state_repair.apply(base, {'updates': {'characters[0]': {'location': '球场'}}}, permissions)

    def test_wrong_or_missing_chapter_is_repaired_only_to_trusted_target(self):
        for chapter in (2, None):
            base = deepcopy(self.base)
            if chapter is None:
                base.pop('chapter')
            else:
                base['chapter'] = chapter
            issues = [f'chapter 必须为 75，实际为 {chapter}']
            permissions = state_repair.plan(base, issues, expected_chapter=75)
            self.assertEqual(self.base, state_repair.apply(base, {'updates': {}}, permissions))
            self.assertEqual(self.base, state_repair.apply(base, self.base, permissions))
            wrong = deepcopy(self.base)
            wrong['chapter'] = 80
            with self.assertRaises(ValueError):
                state_repair.apply(base, wrong, permissions)
        locked = state_repair.plan(self.base, [], expected_chapter=76)
        self.assertNotIn('chapter', locked)

    def test_elapsed_heartbeat_does_not_claim_model_output(self):
        row = {"chapter": 75, "stage": "structured_state", "attempt": 2,
               "phase": "独立证据审计", "event": "state_model_call_started", "observed_at": 10}
        text = describe_preflight_progress(row, now=75)
        for value in ("第75章", "第2轮", "独立证据审计", "65秒", "心跳不代表模型有新输出"):
            self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
