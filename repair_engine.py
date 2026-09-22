# -*- coding: utf-8 -*-
"""Automatic repair policy for unattended chapter generation.

The GUI owns model calls and file writes. This module only classifies failures,
builds targeted repair instructions, and decides whether a failed chapter can
continue as "needs review" after repair attempts are exhausted.
"""

import re


DEFAULT_MAX_REPAIR_ATTEMPTS = 2
DEFAULT_MIN_SALVAGE_CHARS = 2400
DEFAULT_WORD_COUNT_REWRITE_FLOOR = 2800
REVIEW_CONTINUE_STATUS = "可继续但需复核"
SOFT_REVIEW_CATEGORIES = {"outline_drift", "cross_chapter", "consistency", "quality"}
# A failed semantic or prose gate may be useful as a draft, but it must never
# become official context.  Keep this list explicit so a newly-added failure
# category cannot silently inherit the old "needs review and continue" path.
OFFICIAL_BLOCK_CATEGORIES = {
    "word_count",
    "overlength",
    "format",
    "meta",
    "title",
    "repetition",
    "banned_term",
    "truth_reveal",
    "continuity_hard",
    "outline_drift",
    "cross_chapter",
    "consistency",
    "quality",
}


def chinese_char_count(text):
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def parse_gate_status(result):
    """Parse a quality gate's leading PASS/WARN/FAIL contract safely."""
    text = (result or "").strip()
    match = re.search(r"(?mi)^\s*(PASS|WARN|FAIL)\b", text)
    return match.group(1).upper() if match else "WARN"


def classify_failure(reason):
    text = str(reason or "")
    lowered = text.lower()

    if "insufficient balance" in lowered or "余额不足" in text or "error code: 402" in lowered:
        return "api"
    if "内容为空" in text or "未能生成" in text or "空稿" in text:
        return "empty"
    if "字数不足" in text or ("低于" in text and "硬下限" in text):
        return "word_count"
    if "字数过长" in text or "超长" in text or "超过目标上限" in text:
        return "overlength"
    if "元话语" in text or "创作过程" in text or "编辑层" in text:
        return "meta"
    if "markdown残留" in lowered or ("markdown" in lowered and "残留" in text):
        return "format"
    if "漂移防火墙" in text or "禁词" in text or "硬禁词" in text:
        return "banned_term"
    if (
        "正史连续性" in text
        or "境界回档" in text
        or "写入错误事实" in text
        or "未承接上一章" in text
        or "重演已完成事件" in text
    ):
        return "continuity_hard"
    if "真相" in text or "reveal" in lowered:
        return "truth_reveal"
    if "细纲" in text or "核心事件" in text or "本章主目标" in text:
        return "outline_drift"
    if "重复" in text or "标题重复" in text or "回绕" in text:
        return "repetition"
    if "设定总校" in text or "人设" in text or "时间线" in text or "逻辑" in text:
        return "consistency"
    if "跨章" in text:
        return "cross_chapter"
    if "标题" in text or "章节号" in text:
        return "title"
    return "quality"


def category_label(category):
    return {
        "api": "接口/余额",
        "empty": "空稿",
        "word_count": "字数不足",
        "overlength": "字数过长/疑似拼接",
        "format": "格式残留",
        "meta": "正文元话语",
        "banned_term": "硬禁词",
        "truth_reveal": "真相越界",
        "continuity_hard": "正史连续性",
        "outline_drift": "偏离细纲",
        "repetition": "重复/回绕",
        "consistency": "设定一致性",
        "cross_chapter": "跨章一致性",
        "title": "标题问题",
        "quality": "质量问题",
    }.get(category, "质量问题")


def is_hard_pause(reason, strict_reveal=False):
    category = classify_failure(reason)
    if category in {"api", "empty"}:
        return True
    if category == "banned_term":
        return True
    if category == "truth_reveal" and (strict_reveal or "严格模式" in str(reason or "")):
        return True
    return False


def should_rewrite(reason, content="", char_limits=None, strict_reveal=False):
    """Return True only for failures worth spending another generation on."""
    category = classify_failure(reason)
    if category in {"api", "empty"}:
        return False
    if category in {
        "format", "meta", "title", "repetition", "overlength",
        "banned_term", "truth_reveal", "continuity_hard",
    }:
        return True
    if category == "word_count":
        chars = chinese_char_count(content)
        hard_min = int((char_limits or {}).get("min", DEFAULT_WORD_COUNT_REWRITE_FLOOR) or DEFAULT_WORD_COUNT_REWRITE_FLOOR)
        # The UI labels this value as a hard minimum and the reviewer receives
        # the same contract.  Do not silently downgrade a chapter below it.
        return chars < hard_min
    if category in SOFT_REVIEW_CATEGORIES:
        # These categories may be downgraded to "needs review" only after the
        # configured repair attempts are exhausted.  Returning False here made
        # a hard quality gate pause on its first diagnosis, so the advertised
        # automatic rewrite path was never reached for outline/readability
        # failures.
        return True
    return False


def can_continue_with_review(reason, content, char_limits=None, strict_reveal=False, min_salvage_chars=None):
    if is_hard_pause(reason, strict_reveal=strict_reveal):
        return False, "命中必须暂停的问题"
    category = classify_failure(reason)
    if category in OFFICIAL_BLOCK_CATEGORIES:
        return False, f"{category_label(category)}未通过，不能降级写入正式上下文"

    chars = chinese_char_count(content)
    # This is an absolute continuity floor after all repair attempts. A draft
    # below it is usually a truncated response, not a short but complete chapter.
    floor = max(2400, int(min_salvage_chars or DEFAULT_MIN_SALVAGE_CHARS))

    hard_min = int((char_limits or {}).get("min", 0) or 0)
    if hard_min and chars < hard_min:
        return False, f"草稿仅{chars}字，低于项目硬下限{hard_min}字"

    if chars < floor:
        return False, f"草稿仅{chars}字，低于可托管续跑下限{floor}字"

    hard_max = int((char_limits or {}).get("max", 0) or (char_limits or {}).get("target_max", 0) or 0)
    if hard_max and chars > hard_max:
        return False, f"草稿共{chars}字，超过项目正式章上限{hard_max}字"

    first_line = (content or "").strip().split("\n", 1)[0].strip()
    if not first_line.startswith("第"):
        return False, "缺少章节标题，后续上下文会混乱"

    return True, "可降级为需复核并继续"


def decide_after_retries(reason, content, char_limits=None, strict_reveal=False, min_salvage_chars=None):
    category = classify_failure(reason)
    ok, message = can_continue_with_review(
        reason,
        content,
        char_limits=char_limits,
        strict_reveal=strict_reveal,
        min_salvage_chars=min_salvage_chars,
    )
    if ok:
        return {
            "action": "continue_review",
            "level": "L2",
            "category": category,
            "status": REVIEW_CONTINUE_STATUS,
            "message": message,
        }
    return {
        "action": "pause",
        "level": "L3",
        "category": category,
        "status": "待审暂停",
        "message": message,
    }


def format_repair_note(reason, attempt_index):
    category = classify_failure(reason)
    return f"第{attempt_index}版未通过（{category_label(category)}）：{str(reason or '')[:1200]}"


def _excerpt(text, limit=1800):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    half = max(300, limit // 2)
    return text[:half] + "\n\n[中间内容已省略]\n\n" + text[-half:]


def build_targeted_rules(reason, char_limits=None):
    category = classify_failure(reason)
    rules = []

    if category == "word_count":
        target_min = (char_limits or {}).get("target_min", 3500)
        target_max = (char_limits or {}).get("target_max", 4500)
        hard_min = (char_limits or {}).get("min", 3200)
        rules.extend([
            "上一版字数不足。重写一章全新的完整正文，不要只写提纲或摘要。",
            f"正文中文字符目标为{target_min}-{target_max}字，硬下限{hard_min}字。",
            "可以参考上一版的事件走向，但必须从头重写所有段落，不能复制上一版开头后拼接新内容。",
        ])
    elif category == "format":
        rules.extend([
            "清除所有 Markdown 残留：不要使用 #、**、列表符号、反引号。",
            "第一行只保留标准章节标题，标题后空一行进入正文。",
        ])
    elif category == "banned_term":
        rules.extend([
            "完整重写本章，避开失败原因中列出的禁词或高风险表达。",
            "不要用谐音、拆字、变体继续表达同一违规内容；改用安全的剧情动作和角色反应推进。",
        ])
    elif category == "truth_reveal":
        rules.extend([
            "完整重写本章，降低真相揭示等级。",
            "只写角色怀疑、误判、碎片线索和现场反应，不要坐实机制、幕后主因或客观答案。",
            "如果某个主题被指出越界，必须删除或模糊化相关暗示。",
        ])
    elif category == "continuity_hard":
        rules.extend([
            "完整重写本章，严格服从正史状态，不得让境界、地点、伤势或亲属关系回档。",
            "上一章已经结束的重大事件只能被简短提及，禁止重新演一遍。",
            "失败原因点名的错误事实必须完全删除，不能用换一种说法保留。",
        ])
    elif category == "outline_drift":
        rules.extend([
            "完整重写本章，逐条覆盖本章细纲中的核心事件。",
            "不要自行改写细纲目标，不要空降新角色、新势力或新设定解决问题。",
            "章末钩子必须服务于细纲指定的推进方向。",
        ])
    elif category == "overlength":
        target_min = (char_limits or {}).get("target_min", 3500)
        target_max = (char_limits or {}).get("target_max", 4500)
        rules.extend([
            "上一版超过单章上限，可能包含两版拼接或重复场景。必须从头重写，禁止机械截断。",
            f"正文中文字符必须控制在{target_min}-{target_max}字之间。",
            "每个事件只展开一次，保留完整因果闭环和章末钩子，删除重复开场、重复审讯与重复结论。",
        ])
    elif category == "meta":
        rules.extend([
            "完整重写本章，删除所有章纲、审稿、作者或读者视角的元话语。",
            "只写故事世界内的人物、动作、对白和可见证据，不得说明本章作用、题名兑现或后续安排。",
        ])
    elif category == "repetition":
        rules.extend([
            "完整重写本章，禁止重复上一版或前文已经发生过的桥段。",
            "同一事件只能作为简短承接，不得再次当作新事件展开。",
            "标题必须与近期章节不同，正文内只能出现一个章节标题。",
        ])
    elif category == "consistency":
        rules.extend([
            "完整重写本章，优先修正设定、人设、时间线和因果问题。",
            "只采用已知设定和本章细纲明确给出的事实；不确定的内容不要写成客观事实。",
        ])
    elif category == "cross_chapter":
        rules.extend([
            "完整重写本章，检查人物知道什么、不知道什么，以及前文已经发生和已经结束的事件。",
            "不要让已死亡、已离场、已解决或未解锁的内容在本章错误出现。",
        ])
    elif category == "title":
        rules.extend([
            "修正章节标题：第一行必须是当前章节号加一个不重复的4-8字标题。",
            "正文内不要再次出现章节标题。",
        ])
    else:
        rules.extend([
            "根据失败原因完整重写本章。",
            "优先修复硬伤，保持本章细纲、章节号和上一章衔接不变。",
        ])

    return rules


def build_repair_prompt(base_prompt, repair_notes, failed_content="", chapter_outline="", prev_content="", char_limits=None, chap_num=None):
    latest_reason = repair_notes[-1] if repair_notes else ""
    category = classify_failure(latest_reason)
    rules = build_targeted_rules(latest_reason, char_limits=char_limits)
    notes_text = "\n".join(f"- {note}" for note in repair_notes[-3:])
    rules_text = "\n".join(f"- {rule}" for rule in rules)

    failed_block = ""
    if failed_content and category not in {
        "banned_term", "truth_reveal", "repetition", "meta", "overlength",
    }:
        failed_block = (
            "\n\n【上一版失败草稿（只作诊断，不要照抄问题段落）】\n"
            f"{_excerpt(failed_content, 1800)}"
        )

    outline_block = ""
    if chapter_outline:
        outline_block = f"\n\n【本章细纲核对】\n{_excerpt(chapter_outline, 1200)}"

    prev_block = ""
    if prev_content:
        prev_block = f"\n\n【上一章结尾再次核对】\n{_excerpt(prev_content[-700:], 900)}"

    chapter_line = f"第{chap_num}章" if chap_num else "当前章节"
    return (
        base_prompt
        + "\n\n【自动自修复任务】\n"
        + f"你正在修复{chapter_line}的失败稿。不要解释原因，直接输出修复后的完整小说正文。\n\n"
        + "【失败诊断】\n"
        + (notes_text or "- 未提供具体诊断，但上一版未通过托管检查。")
        + "\n\n【本轮必须执行的修复规则】\n"
        + rules_text
        + "\n- 失败诊断是逐项清单，必须修完其中每一项；输出前按原词逐字计数并复核，不得只修第一项。"
        + failed_block
        + outline_block
        + prev_block
        + "\n\n【输出要求】\n"
        + "- 只输出完整章节正文。\n"
        + "- 第一行必须是本章章节标题。\n"
        + "- 不要输出分析、清单、修复说明或 Markdown。\n"
    )
