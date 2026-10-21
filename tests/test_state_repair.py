import json
import unittest
from copy import deepcopy
from unittest import mock

import state_repair
import gui_app
from headless_app import describe_preflight_progress, relay_preflight_progress


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

    def manual_binding(self):
        rejected = {"status": "REJECTED", "repair_scope": "state_only", "chapter": 75,
                    "chapter_sha256": "a" * 64, "previous_raw": json.dumps(self.base),
                    "issues": ["characters[0] 地点证据不足"],
                    "repair_history": [{"issues": ["历史拒绝"]}]}
        state = {"current_chapter": 74}
        context = {"schema_version": 1, "chapter": 75, "chapter_sha256": "a" * 64,
                   "state_sha256": state_repair.digest(state),
                   "rejection_sha256": state_repair.digest(rejected),
                   "patch": {"updates": {"characters[0]": {"location": "车内"}}}}
        return context, rejected, state

    def test_manual_candidate_is_changed_scoped_and_preserves_history(self):
        context, rejected, state = self.manual_binding()
        before = deepcopy(rejected)
        candidate = state_repair.manual_candidate(context, rejected, chapter=75,
                            chapter_sha256="a" * 64, state=state)
        self.assertEqual("车内", candidate["characters"][0]["location"])
        self.assertEqual(self.base["events"], candidate["events"])
        self.assertEqual(before, rejected)

    def test_manual_candidate_rejects_stale_unscoped_unchanged_and_prose_conflicts(self):
        for kind in ("state", "hash", "rejection", "prose", "unchanged", "unrelated"):
            context, rejected, state = self.manual_binding()
            if kind == "state": state["current_chapter"] = 73
            if kind == "hash": context["chapter_sha256"] = "b" * 64
            if kind == "rejection": rejected["issues"].append("新的拒绝")
            if kind == "prose":
                rejected["repair_scope"] = "prose"
                context["rejection_sha256"] = state_repair.digest(rejected)
            if kind == "unchanged": context["patch"] = {"updates": {}}
            if kind == "unrelated": context["patch"] = {"updates": {"resources[0]": {"quantity": 100}}}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                state_repair.manual_candidate(context, rejected, chapter=75,
                                            chapter_sha256="a" * 64, state=state)

    def test_manual_state_runs_one_mandatory_audit_without_extraction_or_auto_repair(self):
        quote = "周予安上车。"
        state = gui_app.state_ledger.initial_state()
        base = {"chapter": 75, "events": [{"event_type": "other", "summary": "上车", "evidence_quote": quote}]}
        text_hash = gui_app.state_ledger.chapter_sha256(quote)
        rejected = {"schema_version": 1, "status": "REJECTED", "repair_scope": "state_only",
                    "chapter": 75, "chapter_sha256": text_hash, "previous_raw": json.dumps(base),
                    "issues": ["events[0] 摘要主体缺失"], "repair_history": [{"issues": ["失败"]}] * 2}
        context = {"schema_version": 1, "chapter": 75, "chapter_sha256": text_hash,
                   "state_sha256": state_repair.digest(state), "rejection_sha256": state_repair.digest(rejected),
                   "patch": {"updates": {"events[0]": {"summary": "周予安上车"}}}}
        app = object.__new__(gui_app.NovelGeneratorGUI)
        app.config = {"temporal_memory_enabled": False, "state_delta_independent_audit": False,
                      "pre_save_continuity_audit_enabled": False}
        app._official_chapter_paths_by_number = lambda: {}
        app._append_batch_audit = mock.Mock()
        app._claim_chapter_repair = mock.Mock(side_effect=AssertionError("no auto repair"))
        for verdict in (True, False):
            payload = {"pass": verdict, "missing": [], "reject": [] if verdict else [{
                "path": "events[0]", "reason": "测试拒绝", "draft_quote": quote,
                "repair_instruction": "只修状态"}]}
            app._call_state_review = mock.Mock(return_value=json.dumps(payload))
            callback = mock.Mock()
            with self.subTest(verdict=verdict), mock.patch.dict(gui_app.generator.DIRS, {"plot": "."}), \
                    mock.patch.object(gui_app.continuity_guard, "load_canon", return_value={}):
                if verdict:
                    prepared = app._prepare_structured_state_delta(75, quote, state_override=state,
                        repair_context=rejected, manual_patch_context=context, rejection_callback=callback)
                    self.assertEqual("周予安上车", prepared["delta"]["events"][0]["summary"])
                    callback.assert_not_called()
                else:
                    with self.assertRaises(gui_app.state_ledger.StateExtractionError):
                        app._prepare_structured_state_delta(75, quote, state_override=state,
                            repair_context=rejected, manual_patch_context=context, rejection_callback=callback)
                    self.assertEqual(3, len(callback.call_args.args[0]["repair_history"]))
                app._call_state_review.assert_called_once()
                self.assertEqual("独立证据审计", app._call_state_review.call_args.kwargs["phase"])
                self.assertEqual(rejected["repair_history"], app._call_state_review.call_args.kwargs["repair_history"])
                app._claim_chapter_repair.assert_not_called()

    def test_prepend_is_scoped_and_preserves_resource_transaction_order(self):
        permissions = state_repair.plan(self.base, ["resources提取遗漏：初始取得未记"])
        initial = {"quantity": 100, "action": "GAIN"}
        result = state_repair.apply(self.base, {"prepend": {"resources": [initial]}}, permissions)
        self.assertEqual([initial] + self.base["resources"], result["resources"])
        self.assertEqual(self.base["characters"], result["characters"])
        with self.assertRaises(ValueError):
            state_repair.apply(self.base, {"prepend": {"characters": [{}]}}, permissions)

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

    def location_snapshot(self, state, quote="罗竞在校门外与同伴分开。", name="罗竞"):
        return {"chapter": 84, "characters": [{"name": name, "location_state": state,
                 "location": "", "evidence_quote": quote}], "events": []}

    def test_location_cycle_ignores_other_changes_wording_and_row_index(self):
        a = self.location_snapshot("NO_CHANGE")
        h = state_repair.record([], json.dumps(a), ["characters[0] 旧地点已失效，应UNKNOWN"])
        b = self.location_snapshot("UNKNOWN")
        h = state_repair.record(h, b, ["characters[0] location_state缺少移动证据"])
        self.assertEqual("", state_repair.stop_reason(h))
        a["events"] = [{"summary": "另一个问题已修复"}]
        a["characters"].insert(0, {"name": "其他人"})
        h = state_repair.record(json.loads(json.dumps(h)), a,
                                ["characters[1] NO_CHANGE错误继承旧位置", "events[0] 新意见"])
        self.assertIn("同一人物地点", state_repair.stop_reason(h))
        self.assertIn("罗竞", state_repair.stop_reason(h))

    def test_better_evidence_different_entity_and_other_fields_are_not_cycles(self):
        h = state_repair.record([], self.location_snapshot("UNKNOWN"),
                                ["characters[0] location_state引用不足"])
        cases = [
            (self.location_snapshot("UNKNOWN", "罗竞离开球场，在校门外与同伴分开。"),
             ["characters[0] location_state引用待审"]),
            (self.location_snapshot("UNKNOWN", name="程野"), ["characters[0] location_state有误"]),
            (self.location_snapshot("UNKNOWN"), ["characters[0] knowledge_add主体不明"]),
        ]
        for snapshot, issues in cases:
            with self.subTest(snapshot=snapshot):
                self.assertEqual("", state_repair.stop_reason(state_repair.record(h, snapshot, issues)))

    def test_independent_location_row_changes_effective_entity_state(self):
        base = self.location_snapshot("NO_CHANGE", "罗竞收到选拔通知。")
        h = state_repair.record([], base, ["characters[0] NO_CHANGE继承旧地点"])
        fixed = deepcopy(base)
        fixed["characters"].extend(self.location_snapshot("UNKNOWN")["characters"])
        h = state_repair.record(h, fixed, ["characters[0] NO_CHANGE需核对同人物地点"])
        self.assertEqual("", state_repair.stop_reason(h))

    def test_history_is_bounded_untrusted_context_and_does_not_change_permissions(self):
        h = state_repair.record([], self.base, ["characters[0] 旧地点失效，应UNKNOWN"])
        h = state_repair.record(h, self.base, ["characters[0] 引文不足，扩大连续证据"])
        text = state_repair.history_context(h)
        for value in ("旧地点失效", "引文不足", "不是正史", "独立地点条目", "不扩大本轮补丁权限"):
            self.assertIn(value, text)
        self.assertEqual("", state_repair.history_context(h, max_chars=1))
        self.assertEqual(self.permissions, state_repair.plan(self.base, ["characters[0] 地点有误"]))

    def test_legacy_history_and_unparseable_output_still_count(self):
        h = [{"snapshot_sha256": "old", "issue_keys": ["legacy"], "issues": ["旧反馈"]}]
        h = state_repair.record(h, "not json", ["解析失败"])
        self.assertEqual({}, h[-1]["rejected_locations"])
        self.assertEqual("", state_repair.stop_reason(h))
        self.assertIn("旧反馈", state_repair.history_context(h))

    def test_unapplied_patch_is_not_a_new_location_verdict_but_consumes_budget(self):
        a = self.location_snapshot("UNKNOWN")
        issues = ["characters[0] location_state引用不足"]
        h = state_repair.record([], a, issues)
        failed = issues + ["局部状态补丁未应用：同一条目不能同时更新和删除"]
        h = state_repair.record(h, a, failed)
        self.assertEqual({}, h[-1]["rejected_locations"])
        self.assertEqual("", state_repair.stop_reason(h))
        self.assertEqual(2, len(h))
        self.assertIn("累计2轮", state_repair.stop_reason(h, 2))
        # Read persisted pre-fix data without deleting or rewriting history.
        h[-1]["rejected_locations"] = deepcopy(h[0]["rejected_locations"])
        self.assertEqual("", state_repair.stop_reason(h))
        a["events"] = [{"summary": "不相关的新增事件"}]
        newer = state_repair.record(h, a, issues)
        self.assertIn("同一人物地点", state_repair.stop_reason(newer))

    def test_repeated_identical_malformed_patch_still_stops(self):
        failed = ["局部状态补丁未应用：同一条目不能同时更新和删除"]
        h = state_repair.record([], self.base, failed)
        h = state_repair.record(h, self.base, failed)
        self.assertIn("重复", state_repair.stop_reason(h))

    def test_progress_relay_preserves_phase_without_duplicate_keyword(self):
        observed = []
        row = {"event": "state_model_call_started", "phase": "独立证据审计",
               "chapter": 84, "attempt": 2}
        relay_preflight_progress(lambda event, **fields: observed.append((event, fields)), row)
        self.assertEqual("state_model_call_started", observed[0][0])
        self.assertEqual("独立证据审计", observed[0][1]["phase"])
        self.assertEqual(2, observed[0][1]["attempt"])
        self.assertEqual("state_model_call_started", row["event"])
        relay_preflight_progress(lambda event, **fields: observed.append((event, fields)), {})
        self.assertEqual("isolated_preflight", observed[-1][1]["phase"])

    def test_rejection_limit_honors_one_and_normalizes_config(self):
        for value, expected in [(1, 1), ('1', 1), (0, 1), (-3, 1), (20, 12),
                                (' 2 ', 2), (None, 6), (True, 6), (1.5, 6),
                                ('oops', 6), ('', 6), ('1.5', 6), ({}, 6)]:
            with self.subTest(value=value):
                self.assertEqual(expected, state_repair.rejection_limit(value))
        h = state_repair.record([], self.base, ['events[0] 未通过'])
        self.assertIn('累计1轮', state_repair.stop_reason(h, 1))
        self.assertEqual('', state_repair.stop_reason(h, 'invalid'))

    def test_same_state_paraphrased_rejections_stop_after_three_rounds(self):
        h = []
        formats = [self.base, json.dumps(self.base, indent=2),
                   json.dumps(dict(reversed(list(self.base.items()))), separators=(',', ':'))]
        for index, snapshot in enumerate(formats):
            h = state_repair.record(json.loads(json.dumps(h)), snapshot, [f'events[0] 变化措辞{index}'])
            if index < 2:
                self.assertEqual('', state_repair.stop_reason(h))
        self.assertIn('连续3轮状态内容未变化', state_repair.stop_reason(h))

    def test_real_state_change_resets_stagnation_window(self):
        h = state_repair.record([], self.base, ['events[0] 意见一'])
        h = state_repair.record(h, self.base, ['events[0] 意见二'])
        changed = deepcopy(self.base)
        changed['events'][0]['evidence_quote'] += '周建成在车内。'
        h = state_repair.record(h, changed, ['events[0] 意见三'])
        self.assertEqual('', state_repair.stop_reason(h))
        self.assertNotEqual(h[-2]['canonical_state_sha256'], h[-1]['canonical_state_sha256'])

    def test_legacy_invalid_and_unapplied_records_do_not_prove_stagnation(self):
        first = state_repair.record([], self.base, ['events[0] 意见一'])[0]
        for middle in ('legacy', 'invalid', 'unapplied'):
            with self.subTest(middle=middle):
                h = state_repair.record([first], 'invalid' if middle == 'invalid' else self.base,
                    ['局部状态补丁未应用：格式错误'] if middle == 'unapplied' else ['events[0] 意见二'])
                if middle == 'legacy':
                    h[-1].pop('canonical_state_sha256')
                h = state_repair.record(h, self.base, ['events[0] 意见三'])
                self.assertEqual('', state_repair.stop_reason(h))
                self.assertIn('累计3轮', state_repair.stop_reason(h, 3))

    def test_history_budget_includes_wrapper_and_preserves_complete_issues(self):
        h = state_repair.record([], self.base, ['旧反馈' * 30])
        h = state_repair.record(h, self.base, ['过长反馈' * 5000, '短反馈：请核对原句。'])
        before = deepcopy(h)
        for budget in (0, 1, 300, 500, 700, 16000):
            with self.subTest(budget=budget):
                text = state_repair.history_context(h, budget)
                self.assertLessEqual(len(text), budget)
                if text:
                    payload = json.loads(text.split('\n', 3)[3].split('\n历史下标', 1)[0])
                    self.assertGreater(payload['omitted_issues'], 0)
                    self.assertIn('不是正史', text)
                    self.assertIn('不扩大本轮补丁权限', text)
                    for row in payload['rounds']:
                        self.assertTrue(all(issue in h[row['round'] - 1]['issues'] for issue in row['issues']))
        text = state_repair.history_context(h, 500)
        self.assertIn('短反馈：请核对原句。', text)
        self.assertNotIn('过长反馈', text)
        self.assertEqual(before, h)

    def test_history_recent_rounds_have_priority_and_report_omissions(self):
        h = [{'issues': [f'反馈{i}']} for i in range(15)]
        text = state_repair.history_context(h)
        payload = json.loads(text.split('\n', 3)[3].split('\n历史下标', 1)[0])
        self.assertEqual(list(range(4, 16)), [row['round'] for row in payload['rounds']])
        self.assertEqual(3, payload['omitted_rounds'])
        self.assertEqual(3, payload['omitted_issues'])

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
