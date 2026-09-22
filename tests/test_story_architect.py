# -*- coding: utf-8 -*-
import json
from pathlib import Path
import tempfile
import unittest

import story_architect


def outline_section(chapter, title="测试章"):
    return f"""第{chapter}章 {title}
核心事件：推进一项可核查事实。
出场人物：沈砚、唐砺。
承接锚点：承接上一章留下的证物。
冲突/爽点：在程序限制内抢救证据。
伏笔/禁出内容：保留上游身份，不提前揭底。
状态落点：证物完成封存。
不可逆变化：调查范围正式扩大。
章末钩子：新编号指向下一处地点。
下一章承接关键词：编号、地点、证人。
"""


def extended_outline_section(
    chapter,
    title="测试章",
    function="调查发现",
    payoff="知识",
    ending="新问题",
):
    return f"""第{chapter}章 {title}
章节功能：{function}
核心事件：推进一项可核查事实。
出场人物：沈砚、唐砺。
承接锚点：承接上一章留下的证物。
人物选择与代价：沈砚放弃捷径并承担证据失效风险。
反方行动：利益方主动撤回关键授权并转移证物。
冲突/爽点：在程序限制内抢救证据。
兑现类型：{payoff}
伏笔/禁出内容：保留上游身份，不提前揭底。
状态落点：证物完成封存。
不可逆变化：调查范围正式扩大。
结尾类型：{ending}
章末钩子：新编号指向下一处地点。
下一章承接关键词：编号、地点、证人。
"""


class OutlineIntegrityTests(unittest.TestCase):
    def test_transport_truncation_marker_fails_closed(self):
        text = outline_section(14).replace(
            "推进一项可核查事实。",
            "推进一项事实，随后出现 831 tokens truncated 的传输残片。",
        )
        result = story_architect.validate_outline_contract(text, 14, 14)
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("截断标记" in issue for issue in result["issues"]))

    def test_duplicate_chapter_heading_fails_closed(self):
        text = outline_section(14, "第一版") + "\n" + outline_section(14, "第二版")
        result = story_architect.validate_outline_contract(text, 14, 14)
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("标题重复" in issue for issue in result["issues"]))

    def test_complete_unique_outline_passes(self):
        text = outline_section(14, "第一处现场") + "\n" + outline_section(15, "版本历史")
        result = story_architect.validate_outline_contract(text, 14, 15)
        self.assertEqual("PASS", result["status"])

    def test_legacy_outline_remains_backward_compatible(self):
        result = story_architect.validate_outline_contract(
            outline_section(1), 1, 1, require_extended=False
        )
        self.assertEqual("PASS", result["status"])

    def test_new_outline_requires_extended_story_contract(self):
        result = story_architect.validate_outline_contract(
            outline_section(1), 1, 1, require_extended=True
        )
        self.assertEqual("FAIL", result["status"])
        self.assertIn("扩展细纲字段", result["summary"])

        result = story_architect.validate_outline_contract(
            extended_outline_section(1), 1, 1, require_extended=True
        )
        self.assertEqual("PASS", result["status"])

    def test_extended_outline_rejects_three_identical_beats(self):
        text = "\n".join(
            extended_outline_section(
                chapter,
                title=f"测试{chapter}",
                function="调查发现",
                payoff="知识",
                ending="新问题",
            )
            for chapter in range(1, 4)
        )
        result = story_architect.validate_outline_contract(
            text, 1, 3, require_extended=True
        )
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("连续重复" in issue for issue in result["issues"]))

    def test_story_context_injects_operational_volume_fields(self):
        bible = {
            "premise": "测试前提",
            "core_promise": "测试承诺",
            "system_rules": ["不得越界"],
            "volume_contracts": [{
                "start": 1,
                "end": 10,
                "goal": "查清现场",
                "case_models": [{"range": "1-5", "victims": "维修员"}],
                "antagonist_strategy": "用合法停服制造反例",
                "relationship_turns": ["搭档公开分歧后重建合作"],
                "value_choice_contract": "主角在救人速度与证据程序间做选择",
                "environment_constraints": ["暴雨切断唯一进场道路"],
                "payoff_mix": ["知识", "关系", "失败代价"],
                "irreversible_failure": "一次误报造成真实伤害",
                "pacing_guard": "连续两章会商即改写",
                "promise_progress": "兑现能力边界",
                "power_boundary": "不得升级",
                "climax": "现场中止更新",
                "avoid": ["只开会不行动"],
            }],
            "arc_contracts": [],
        }
        with tempfile.TemporaryDirectory() as root:
            plot = Path(root)
            (plot / "story_bible.json").write_text(
                json.dumps(bible, ensure_ascii=False), encoding="utf-8"
            )
            context = story_architect.build_story_context(str(plot), 3, max_chars=5000)
        for expected in (
            "案件发动机", "维修员", "反方策略", "关系转折",
            "不可逆失败", "移动端节奏", "连续两章会商即改写",
            "人物选择合同", "环境行动限制", "兑现组合", "暴雨切断",
        ):
            self.assertIn(expected, context)

    def test_schema_v2_story_bible_requires_operational_contracts(self):
        bible = story_architect.build_default_story_bible(
            "测试书", "现实悬疑", 10, [[1, 10, "第一卷"]]
        )
        self.assertEqual(2, bible["schema_version"])
        self.assertEqual([], story_architect.validate_story_bible_data(bible, 10))
        del bible["arc_contracts"][0]["antagonist_strategy"]
        issues = story_architect.validate_story_bible_data(bible, 10)
        self.assertTrue(any("antagonist_strategy" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
