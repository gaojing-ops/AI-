# -*- coding: utf-8 -*-
import json
import unittest
from unittest import mock

import commercial_planner


def valid_payload():
    return {
        "decision": "PROCEED",
        "commercial_score": 82,
        "score_reasons": ["承诺清楚"],
        "final_title": "回收废弃文明后，我被全人类追杀",
        "title_options": ["回收废弃文明后，我被全人类追杀", "文明回收站", "末日回收人"],
        "target_reader": "喜欢末日求生、规则博弈和成长反馈的18至35岁移动端读者",
        "reader_desire": "看主角利用有限回收权限解决危机并不断改变阵营关系",
        "one_line_promise": "末日回收员必须在文明被销毁前找出错误判定，同时承受每次回收都会遗失记忆的代价",
        "core_mechanism": "回收任务提供资源也夺走记忆，任务选择持续改变盟友、敌人和真相路径",
        "differentiation": ["资源伴随记忆代价", "敌我关系随任务变化", "每卷回收一种文明能力"],
        "synopsis": "世界被标记为废弃文明后，唯一能进入回收站的普通维修员收到最后通牒。每完成一次回收，他能换回一项拯救城市的资源，也会永久丢失一段重要记忆。为了证明判定有误，他必须在同伴逐渐变成陌生人之前找到提交毁灭指令的人。",
        "golden_three_contract": [
            {"chapter": 1, "hook": "城市倒计时", "conflict": "首次选择", "payoff": "救下一人", "cliffhanger": "记忆消失"},
            {"chapter": 2, "hook": "验证代价", "conflict": "资源不足", "payoff": "规则可用", "cliffhanger": "同伴不认识他"},
            {"chapter": 3, "hook": "任务升级", "conflict": "救城或救人", "payoff": "完成首个闭环", "cliffhanger": "发现人为指令"},
        ],
        "retention_contract": {
            "every_chapter": "一个选择和一个后果",
            "every_3_chapters": "一次规则变化",
            "every_10_chapters": "关闭一个危机",
            "every_30_chapters": "改变一个阵营格局",
        },
        "long_form_engine": "任务层级、记忆代价、盟友关系和文明真相同步递进，每卷都有不同资源问题与阶段终点",
        "arc_promises": ["保住第一城", "进入回收站", "反转文明判定"],
        "failure_risks": ["任务重复", "代价失效", "谜底拖延"],
        "must_avoid": ["万能系统", "无代价升级", "只换敌人重复"],
        "market_confidence": "HIGH",
    }


class CommercialPlannerTests(unittest.TestCase):
    def test_normalizes_confidence_and_passes_valid_gate(self):
        evidence = {"market_confidence_cap": "MEDIUM"}
        blueprint = commercial_planner.normalize_blueprint(
            valid_payload(), "原书名", "科幻末世", "文明回收", 300, evidence
        )
        self.assertEqual(blueprint["market_confidence"], "MEDIUM")
        self.assertTrue(any(
            "缺少独立复核" in issue
            for issue in commercial_planner.validate_blueprint(
                blueprint, min_score=78
            )
        ))
        blueprint = commercial_planner.apply_gate_review(
            blueprint,
            {
                "decision": "PROCEED",
                "commercial_score": 82,
                "gate_failures": [],
                "required_changes": [],
                "audit_summary": "独立复核通过",
            },
        )
        self.assertEqual(
            commercial_planner.validate_blueprint(blueprint, min_score=78), []
        )
        self.assertTrue(
            commercial_planner.blueprint_matches(
                blueprint, "原书名", "科幻末世", "文明回收", 300, min_score=78
            )
        )

    def test_top_level_proceed_cannot_replace_independent_gate(self):
        blueprint = commercial_planner.normalize_blueprint(
            valid_payload(), "书名", "科幻", "主题", 100, {}
        )
        self.assertEqual("PROCEED", blueprint["decision"])
        issues = commercial_planner.validate_blueprint(blueprint, min_score=78)
        self.assertTrue(any("缺少独立复核" in issue for issue in issues))

        blueprint["gate_review"] = {
            "decision": "UNVERIFIED",
            "commercial_score": 0,
            "gate_failures": [],
            "required_changes": ["必须独立复核"],
            "audit_summary": "尚未审查",
        }
        issues = commercial_planner.validate_blueprint(blueprint, min_score=78)
        self.assertTrue(any("独立复核结论" in issue for issue in issues))
        self.assertTrue(any("评分与商业蓝图" in issue for issue in issues))

    def test_low_score_or_missing_opening_is_blocked(self):
        payload = valid_payload()
        payload["commercial_score"] = 70
        payload["golden_three_contract"] = payload["golden_three_contract"][:2]
        blueprint = commercial_planner.normalize_blueprint(
            payload, "书名", "科幻", "主题", 100, {}
        )
        issues = commercial_planner.validate_blueprint(blueprint, min_score=78)
        self.assertTrue(any("低于门槛" in issue for issue in issues))
        self.assertTrue(any("黄金三章" in issue for issue in issues))

    def test_independent_gate_score_overrides_planner_self_score(self):
        blueprint = commercial_planner.normalize_blueprint(
            valid_payload(), "书名", "科幻", "主题", 100, {}
        )
        reviewed = commercial_planner.apply_gate_review(
            blueprint,
            {
                "decision": "REVISE",
                "commercial_score": 72,
                "gate_failures": ["前三章只埋谜，没有小闭环"],
                "required_changes": ["第三章完成一次可验证的救援"],
                "audit_summary": "不能放行",
            },
        )
        self.assertEqual(reviewed["planner_score"], 82)
        self.assertEqual(reviewed["commercial_score"], 72)
        issues = commercial_planner.validate_blueprint(reviewed, min_score=78)
        self.assertTrue(any("独立复核" in issue for issue in issues))

    def test_official_rank_snapshot_never_invents_content(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"bookId":"1"}{"bookId":"2"}'

        with mock.patch("commercial_planner.urllib.request.urlopen", return_value=Response()):
            evidence = commercial_planner.fetch_fanqie_market_evidence(
                urls=(("榜一", "https://example.com/1"),)
            )
        self.assertEqual(evidence["available_pages"], 1)
        self.assertEqual(evidence["unique_book_id_count"], 2)
        self.assertEqual(evidence["market_confidence_cap"], "LOW")
        self.assertNotIn("titles", json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
