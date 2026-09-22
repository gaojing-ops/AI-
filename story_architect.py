# -*- coding: utf-8 -*-
"""Structured story contracts for long-form unattended generation."""

import json
import os
import re


STORY_BIBLE_FILENAME = "story_bible.json"
REQUIRED_OUTLINE_FIELDS = (
    "核心事件",
    "出场人物",
    "承接锚点",
    "冲突/爽点",
    "伏笔/禁出内容",
    "状态落点",
    "不可逆变化",
    "章末钩子",
    "下一章承接关键词",
)
EXTENDED_OUTLINE_FIELDS = (
    "章节功能",
    "人物选择与代价",
    "反方行动",
    "兑现类型",
    "结尾类型",
)
CHAPTER_FUNCTIONS = (
    "冲突推进", "调查发现", "兑现回收", "关系转折",
    "代价余波", "铺垫蓄力", "高潮决断",
)
PAYOFF_TYPES = (
    "能力", "知识", "关系", "身份", "资源", "情绪", "谜底", "失败代价",
)
ENDING_TYPES = (
    "新问题", "代价落地", "关系变化", "阶段兑现",
    "信息反差", "危机逼近", "情绪余震",
)
_TRANSPORT_TRUNCATION_RE = re.compile(
    r"(?:\b(?:output\s+)?truncated\b|\boriginal\s+token\s+count\b|"
    r"\b\d+\s+tokens?\s+truncated\b|内容(?:已)?截断)",
    re.IGNORECASE,
)


def contains_transport_truncation_artifact(text):
    """Return True for renderer/tool truncation sentinels, never story ellipses."""
    return bool(_TRANSPORT_TRUNCATION_RE.search(str(text or "")))


def load_story_bible(plot_dir):
    path = os.path.join(plot_dir, STORY_BIBLE_FILENAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def validate_story_bible_data(bible, target_chapters=0):
    """Validate the machine-readable plot spine before it becomes project truth."""
    issues = []
    if not isinstance(bible, dict):
        return ["故事圣经不是 JSON 对象"]
    for key in ("premise", "core_promise", "volume_contracts", "arc_contracts"):
        if not bible.get(key):
            issues.append(f"故事圣经缺少 {key}")

    extended_contract = int(bible.get("schema_version", 1) or 1) >= 2
    volumes = bible.get("volume_contracts") or []
    expected_start = 1
    for item in volumes:
        if not isinstance(item, dict):
            issues.append("分卷契约存在非对象条目")
            continue
        start = int(item.get("start", 0) or 0)
        end = int(item.get("end", 0) or 0)
        if start != expected_start or end < start:
            issues.append(f"分卷契约不连续：{start}-{end}，期望从{expected_start}开始")
        for key in ("goal", "promise_progress", "power_boundary", "climax"):
            if not item.get(key):
                issues.append(f"分卷{start}-{end}缺少{key}")
        if extended_contract:
            for key in (
                "antagonist_strategy", "relationship_turns",
                "value_choice_contract", "environment_constraints",
                "payoff_mix", "pacing_guard",
            ):
                if not item.get(key):
                    issues.append(f"分卷{start}-{end}缺少{key}")
        expected_start = end + 1
    if target_chapters and expected_start - 1 != int(target_chapters):
        issues.append(f"故事圣经终章{expected_start - 1}与目标终章{target_chapters}不一致")

    arcs = bible.get("arc_contracts") or []
    if arcs:
        expected_start = 1
        for item in arcs:
            start = int(item.get("start", 0) or 0)
            end = int(item.get("end", 0) or 0)
            if start != expected_start or end < start:
                issues.append(f"阶段契约不连续：{start}-{end}，期望从{expected_start}开始")
            if extended_contract:
                for key in (
                    "antagonist_strategy", "relationship_turns",
                    "value_choice_contract", "environment_constraints",
                    "payoff_mix", "pacing_guard",
                ):
                    if not item.get(key):
                        issues.append(f"阶段{start}-{end}缺少{key}")
            expected_start = end + 1
        if target_chapters and expected_start - 1 != int(target_chapters):
            issues.append(f"阶段契约终章{expected_start - 1}与目标终章{target_chapters}不一致")
    return issues


def build_default_story_bible(title, genre, total_chapters, volume_ranges):
    """Create a conservative local fallback when the architect response is invalid."""
    total = max(1, int(total_chapters or 1))
    normalized = []
    for index, item in enumerate(volume_ranges or [], start=1):
        try:
            start, end = int(item[0]), int(item[1])
            name = str(item[2] or f"第{index}卷")
        except Exception:
            continue
        normalized.append((start, min(end, total), name))
    if not normalized:
        normalized = [(1, total, "第一卷")]

    volumes = []
    arcs = []
    for index, (start, end, name) in enumerate(normalized, start=1):
        volumes.append({
            "start": start,
            "end": end,
            "name": name,
            "goal": f"完成{name}的阶段主线，并让主要人物关系和核心矛盾产生不可逆推进",
            "promise_progress": f"兑现《{title}》核心卖点的第{index}阶段能力或认知变化",
            "power_boundary": "遵循全书大纲和逐章细纲，不得无铺垫连续跨越大阶段",
            "climax": f"在第{end}章前解决本卷核心冲突，同时留下下一卷的具体行动入口",
            "antagonist_strategy": "反方必须主动改变局势、资源或关系，不能只等待主角上门",
            "relationship_turns": ["至少一次因人物行动与代价造成的关系变化"],
            "value_choice_contract": "主角必须在收益与底线之间做出可见选择，并承担不能撤销的代价",
            "environment_constraints": ["职业、制度、地点或资源限制必须实际改变人物行动"],
            "payoff_mix": ["能力", "知识", "关系", "身份", "失败代价"],
            "pacing_guard": "连续三章不得使用相同章节功能、兑现类型和结尾类型",
            "avoid": ["重复上一卷高潮", "只升级不推进主线", "凭空新增解决问题的设定"],
        })
        size = end - start + 1
        split = start + max(1, size // 2) - 1
        split = min(split, end)
        ranges = [(start, split, "建立与升级冲突")]
        if split < end:
            ranges.append((split + 1, end, "收束证据链并完成阶段高潮"))
        for arc_start, arc_end, phase in ranges:
            arcs.append({
                "start": arc_start,
                "end": arc_end,
                "goal": f"围绕{name}{phase}，每章必须带来新信息、关系变化或行动结果",
                "promise_progress": f"让书名卖点在第{arc_start}-{arc_end}章出现可验证的新用法或新代价",
                "power_boundary": "以本卷边界为上限，升级必须有资源、训练、代价和结果",
                "climax": f"第{arc_end}章形成不可逆落点并明确下一阶段行动",
                "antagonist_strategy": "反方用已经建立的资源主动施压，并根据主角行动调整策略",
                "relationship_turns": ["本阶段至少一次关系因选择、隐瞒、失败或牺牲发生变化"],
                "value_choice_contract": "同一价值难题再次出现时，主角的选择必须比上一阶段有所变化",
                "environment_constraints": ["至少一个环境或制度限制迫使人物改变原计划"],
                "payoff_mix": ["知识", "关系", "能力", "失败代价"],
                "pacing_guard": "调查、冲突、兑现、关系和余波章节交替，不得连续复制同一节拍",
                "avoid": ["重演已完成事件", "连续三章只讨论不行动", "用新名词代替剧情推进"],
            })

    return {
        "schema_version": 2,
        "book_title": title,
        "genre": genre,
        "premise": f"围绕《{title}》的核心矛盾展开，所有支线最终必须回流主线。",
        "core_promise": f"持续兑现《{title}》的题名卖点，每个阶段都必须有可验证的新变化、新代价和新结果。",
        "system_rules": [
            "能力、资源和情报不能凭空出现",
            "重要胜利必须建立在已写铺垫、行动和代价上",
            "新设定必须先埋线索，再验证，最后才能坐实",
        ],
        "volume_contracts": volumes,
        "arc_contracts": arcs,
        "hard_gates": [],
    }


def build_initial_canon(protagonist="主角"):
    return {
        "schema_version": 1,
        "current_chapter": 0,
        "protagonist": protagonist or "主角",
        "current_realm": "未记录",
        "current_location": "以第1章细纲为准",
        "current_injury": "无",
        "open_hook": "按第1章细纲开篇，不得跳过开局核心事件",
        "realm_order": [],
        "completed_events": [],
        "completed_event_patterns": [],
        "forbidden_false_facts": [],
        "forbidden_false_patterns": [],
        "identity_facts": [],
        "consumed_items": [],
        "memory_replacements": {},
        "next_chapter_required_any": [],
    }


def split_outline_sections(outline_text):
    """Return {chapter_number: full outline section}; headings must be anchored."""
    text = (outline_text or "").replace("\r\n", "\n").replace("\r", "\n")
    # A heading may include either "标题：..." or a plain title after the
    # chapter number. Requiring whitespace after "章" keeps prose such as
    # "第10章已完成" from being mistaken for a new section.
    matches = list(re.finditer(r"(?m)^第\s*(\d+)\s*章(?:\s+[^\n]*)?\s*$", text))
    sections = {}
    for index, match in enumerate(matches):
        chap = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[chap] = text[match.start():end].strip()
    return sections


def parse_outline_fields(section_text):
    fields = {}
    for line in (section_text or "").splitlines()[1:]:
        match = re.match(r"^([^：:]{2,20})[：:]\s*(.*)$", line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def _matching_contract(items, chap_num):
    for item in items or []:
        if int(item.get("start", 0) or 0) <= chap_num <= int(item.get("end", 0) or 0):
            return item
    return {}


def get_story_contract(plot_dir, chap_num):
    bible = load_story_bible(plot_dir)
    return {
        "premise": bible.get("premise", ""),
        "core_promise": bible.get("core_promise", ""),
        "system_rules": bible.get("system_rules") or [],
        "volume": _matching_contract(bible.get("volume_contracts"), chap_num),
        "arc": _matching_contract(bible.get("arc_contracts"), chap_num),
    }


def blocked_terms_for_chapter(plot_dir, chap_num):
    """Return author-only terms that must not enter the current stage context."""
    bible = load_story_bible(plot_dir)
    blocked = []
    for gate in bible.get("hard_gates") or []:
        before = int(gate.get("before", 0) or 0)
        if int(chap_num or 0) >= before:
            continue
        blocked.extend(str(term).strip() for term in gate.get("terms") or [] if str(term).strip())
    return list(dict.fromkeys(blocked))


def clamp_outline_batch_end(plot_dir, start_chap, requested_end):
    """Keep one generated outline batch inside a single truth-disclosure stage."""
    start_chap = int(start_chap)
    end_chap = int(requested_end)
    bible = load_story_bible(plot_dir)
    for gate in bible.get("hard_gates") or []:
        unlock_chapter = int(gate.get("before", 0) or 0)
        if start_chap < unlock_chapter <= end_chap:
            end_chap = min(end_chap, unlock_chapter - 1)
    return max(start_chap, end_chap)


def build_stage_truth_context(plot_dir, chap_num, max_chars=2800):
    """Expose reader-visible canon without leaking the author's objective answers."""
    path = os.path.join(plot_dir, "唯一真相设定表.md")
    rows = []
    blocked = blocked_terms_for_chapter(plot_dir, chap_num)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line.startswith("|"):
                        continue
                    cells = [cell.strip() for cell in line.strip("|").split("|")]
                    if len(cells) < 5 or cells[0] in {"主题", "------"} or set(cells[0]) == {"-"}:
                        continue
                    topic, _objective_truth, current_knowledge, reveal_point, _forbidden = cells[:5]
                    if topic and current_knowledge:
                        if any(term in topic for term in blocked):
                            topic = "后期未公开主题"
                        for term in blocked:
                            current_knowledge = current_knowledge.replace(term, "后期未公开内容")
                            reveal_point = reveal_point.replace(term, "后期阶段")
                        rows.append(
                            f"- {topic}：本章只能使用角色当前认知：{current_knowledge} "
                            f"揭示进度参考：{reveal_point}"
                        )
        except Exception:
            rows = []

    lines = ["【本章可见真相边界】"]
    lines.extend(rows)
    if blocked:
        lines.append("- 后期作者层术语已从本章上下文移除，不得自行补全其名称和因果。")
    lines.append("- 未列入角色当前认知的客观因果，不得由旁白、系统或高可信角色代为公布。")
    return "\n".join(lines)[:max_chars]


def build_story_context(plot_dir, chap_num, max_chars=2600):
    contract = get_story_contract(plot_dir, chap_num)
    if not any(contract.values()):
        return ""
    lines = ["【结构化故事契约】"]
    if contract.get("premise"):
        lines.append("- 核心前提：" + str(contract["premise"]))
    if contract.get("core_promise"):
        lines.append("- 书名卖点：" + str(contract["core_promise"]))
    rules = contract.get("system_rules") or []
    if rules:
        lines.append("- 系统边界：" + "；".join(str(x) for x in rules))
    for label, key in (("本卷契约", "volume"), ("当前阶段契约", "arc")):
        item = contract.get(key) or {}
        if not item:
            continue
        lines.append(f"- {label}：第{item.get('start')}-{item.get('end')}章")
        for field_label, field_key in (
            ("任务", "goal"),
            ("案件发动机", "case_models"),
            ("反方策略", "antagonist_strategy"),
            ("关系转折", "relationship_turns"),
            ("人物选择合同", "value_choice_contract"),
            ("环境行动限制", "environment_constraints"),
            ("兑现组合", "payoff_mix"),
            ("不可逆失败", "irreversible_failure"),
            ("移动端节奏", "pacing_guard"),
            ("卖点推进", "promise_progress"),
            ("境界边界", "power_boundary"),
            ("阶段高潮", "climax"),
            ("禁止循环", "avoid"),
        ):
            value = item.get(field_key)
            if value:
                if isinstance(value, list) and all(
                    not isinstance(part, (dict, list)) for part in value
                ):
                    value = "；".join(str(x) for x in value)
                elif isinstance(value, (dict, list)):
                    value = json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                lines.append(f"  {field_label}：{value}")
    return "\n".join(lines)[:max_chars]


def _matches_contract_choice(value, choices):
    value = str(value or "").strip()
    return any(value == choice or value.startswith(choice + "（") for choice in choices)


def validate_outline_contract(
    outline_text, start_chap, end_chap, plot_dir=None, strict=True,
    require_extended=False,
):
    sections = split_outline_sections(outline_text)
    expected = list(range(int(start_chap), int(end_chap) + 1))
    found = sorted(sections)
    issues = []
    raw_headings = [
        int(match.group(1))
        for match in re.finditer(
            r"(?m)^第\s*(\d+)\s*章(?:\s+[^\n]*)?\s*$",
            str(outline_text or "").replace("\r\n", "\n").replace("\r", "\n"),
        )
    ]
    duplicate_headings = sorted(
        {chapter for chapter in raw_headings if raw_headings.count(chapter) > 1}
    )
    if duplicate_headings:
        issues.append(f"章节标题重复：{duplicate_headings}")
    if contains_transport_truncation_artifact(outline_text):
        issues.append("细纲含工具或传输截断标记，拒绝使用不完整内容")
    if found != expected:
        issues.append(f"章节覆盖错误：期望{expected}，实际{found}")

    titles = []
    extended_rows = []
    for chap in expected:
        section = sections.get(chap, "")
        if not section:
            continue
        first_line = section.splitlines()[0].strip()
        title = re.sub(r"^第\s*\d+\s*章\s*", "", first_line).strip(" ：:")
        if title:
            if title in titles:
                issues.append(f"第{chap}章标题重复：{title}")
            titles.append(title)
        fields = parse_outline_fields(section)
        if strict:
            missing = [field for field in REQUIRED_OUTLINE_FIELDS if not fields.get(field)]
            if missing:
                issues.append(f"第{chap}章缺少细纲字段：{'、'.join(missing)}")
        if require_extended:
            missing = [field for field in EXTENDED_OUTLINE_FIELDS if not fields.get(field)]
            if missing:
                issues.append(f"第{chap}章缺少扩展细纲字段：{'、'.join(missing)}")
            if fields.get("章节功能") and not _matches_contract_choice(
                fields["章节功能"], CHAPTER_FUNCTIONS
            ):
                issues.append(f"第{chap}章章节功能不在允许类型中")
            if fields.get("兑现类型") and not _matches_contract_choice(
                fields["兑现类型"], PAYOFF_TYPES
            ):
                issues.append(f"第{chap}章兑现类型不在允许类型中")
            if fields.get("结尾类型") and not _matches_contract_choice(
                fields["结尾类型"], ENDING_TYPES
            ):
                issues.append(f"第{chap}章结尾类型不在允许类型中")
            extended_rows.append(fields)
        keywords = fields.get("下一章承接关键词", "")
        if strict and chap < end_chap and keywords in {"", "无", "暂无", "不需要"}:
            issues.append(f"第{chap}章没有下一章承接关键词")

    if require_extended and len(extended_rows) >= 3:
        for field, label, limit in (
            ("章节功能", "章节功能", 3),
            ("兑现类型", "兑现类型", 3),
            ("结尾类型", "结尾类型", 3),
        ):
            values = [str(row.get(field) or "").split("（", 1)[0] for row in extended_rows]
            for index in range(0, len(values) - limit + 1):
                window = values[index:index + limit]
                if window[0] and len(set(window)) == 1:
                    issues.append(
                        f"第{start_chap + index}-{start_chap + index + limit - 1}章"
                        f"连续重复同一{label}：{window[0]}"
                    )
                    break

    if plot_dir:
        for chap in expected:
            section = sections.get(chap, "")
            for term in blocked_terms_for_chapter(plot_dir, chap):
                if term and term in section:
                    issues.append(f"第{chap}章提前使用受限内容：{term}")

    return {
        "status": "FAIL" if issues else "PASS",
        "issues": issues,
        "summary": "；".join(issues[:5]),
        "sections": sections,
    }


def outline_state_update(section_text):
    """Extract deterministic canon updates from an accepted chapter outline."""
    fields = parse_outline_fields(section_text)
    state = fields.get("状态落点", "")
    result = {
        "open_hook": fields.get("章末钩子", ""),
        "next_required": [
            item.strip()
            for item in re.split(r"[/、,，；;]", fields.get("下一章承接关键词", ""))
            if item.strip() and item.strip() not in {"无", "暂无"}
        ],
    }
    progression = re.search(
        r"(?:境界|修为|等级|阶段|职级)[=：:]\s*([^；;，,\n]{1,24})",
        state,
    )
    if progression:
        result["current_realm"] = progression.group(1).strip()
    location = re.search(r"(?:地点|位置)[=：:]\s*([^；;，,]+)", state)
    if location:
        result["current_location"] = location.group(1).strip()
    injury = re.search(r"(?:伤势|身体)[=：:]\s*([^；;]+)", state)
    if injury:
        result["current_injury"] = injury.group(1).strip()
    irreversible = fields.get("不可逆变化", "")
    if irreversible:
        result["completed_event"] = irreversible
    return result


def audit_story_bible(plot_dir, config):
    bible = load_story_bible(plot_dir)
    if not bible:
        return [{"status": "FAIL", "message": "story_bible.json 缺失或无效"}]
    target = int(config.get("target_total_chapters", 0) or 0)
    issues = validate_story_bible_data(bible, target)
    return (
        [{"status": "FAIL", "message": issue} for issue in issues]
        or [{"status": "PASS", "message": "故事圣经结构完整"}]
    )
