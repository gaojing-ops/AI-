# -*- coding: utf-8 -*-
"""Build executable Fanqie publishing rules plus per-book theme guards."""

import copy
import hashlib
import json
import re
from datetime import date


FANQIE_RULESET_VERSION = "2026-07-20"
FANQIE_OFFICIAL_SOURCES = [
    {
        "title": "番茄平台内容发布规范",
        "url": "https://fanqienovel.com/writer/zone/article/7237084151751376957",
        "published": "2025-02-26",
    },
    {
        "title": "内容安全须知",
        "url": "https://fanqienovel.com/writer/zone/article/7231069121566212151/",
        "published": "2023-05-22",
    },
    {
        "title": "长篇网文发文规则（第二版）上线通知",
        "url": "https://fanqienovel.com/writer/zone/article/7639950766869839897",
        "published": "2026-05-21",
    },
]

PLATFORM_SEMANTIC_RULES = [
    "不得包含违反法律法规和相关政策的内容。",
    "不得包含低俗色情、未成年人负面导向、极端血腥或明显引人不适的细节渲染。",
    "不得攻击引战、人肉网暴、歧视侮辱现实群体，或把虚构机构映射成现实对象。",
    "不得植入站外导流、博彩、代充返利、违规盈利或传授平台漏洞利用方法。",
    "不得批量发布无意义内容、恶意水文、重复段落、无关信息或仅靠改名换皮推进。",
    "不得抄袭、洗稿或仿写可识别的在先作品表达；专有名词与核心桥段须保持原创。",
]

BASE_BANNED_KEYWORDS = [
    {"keyword": "肠子流出", "reason": "番茄内容安全：避免极端血腥细节"},
    {"keyword": "内脏外露", "reason": "番茄内容安全：避免极端血腥细节"},
    {"keyword": "活剥人皮", "reason": "番茄内容安全：避免极端血腥细节"},
    {"keyword": "加微信领资源", "reason": "番茄发布规范：禁止站外导流"},
    {"keyword": "加群领资源", "reason": "番茄发布规范：禁止站外导流"},
    {"keyword": "博彩网站", "reason": "番茄发布规范：禁止违规盈利和博彩导流"},
]

BASE_SCANNER_FORBIDDEN = [
    [r"(?:加|联系)(?:微信|V信|vx).{0,12}(?:领取|购买|咨询|资源)", "番茄发布规范：疑似站外导流", 0],
    [r"(?:博彩|赌博).{0,10}(?:网站|平台|群|链接)", "番茄发布规范：疑似博彩导流", 0],
    [r"(?:肠子流出|内脏外露|活剥人皮)", "番茄内容安全：极端血腥细节", 0],
]


def default_tool_rules():
    return {
        "ruleset_metadata": {
            "platform": "番茄小说",
            "ruleset_version": FANQIE_RULESET_VERSION,
            "generated_at": "",
            "project_fingerprint": "",
            "official_sources": copy.deepcopy(FANQIE_OFFICIAL_SOURCES),
            "notice": "自动扫描仅作发布前辅助，平台实时审核结果优先。",
        },
        "platform_semantic_rules": list(PLATFORM_SEMANTIC_RULES),
        "theme_contract": {},
        "theme_avoid_terms": [],
        "banned_keywords": copy.deepcopy(BASE_BANNED_KEYWORDS),
        "high_risk_watch_keywords": [
            "严重时间线回档", "已死亡角色复活", "核心真相提前坐实",
            "未授权新组织", "未铺垫新Boss", "主角能力规则改写",
            "批量无意义内容", "恶意水文", "站外导流", "未成年人负面导向",
        ],
        "style_risk_keywords": [
            "章尾短句堆叠", "章尾破折号过密", "模糊词过密",
            "对白停顿模板过密", "深呼吸模板过密", "突发副词过密",
            "重复段落", "无关信息", "改名换皮",
        ],
        "memory_required_terms": [],
        "scanner_forbidden_terms": copy.deepcopy(BASE_SCANNER_FORBIDDEN),
        "chapter_gated_terms": [],
        "event_patterns": [],
        "event_completion_chapters": {},
        "old_name_patterns": {},
        "scanner_allowed_contexts": {},
    }


def _dedupe(items):
    result = []
    seen = set()
    for item in items or []:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_theme_avoid_terms(book_brief):
    text = str(book_brief or "")
    terms = []
    for match in re.finditer(
        r"(?:禁止|避免|不要|不得)\s*[：:]?\s*([^。！？!?\n]+)",
        text,
    ):
        body = match.group(1)
        # A later directive in the same sentence starts a new list rather than
        # becoming part of the preceding term.
        body = re.sub(r"[；;]\s*(?:禁止|避免|不要|不得)\s*[：:]?", "、", body)
        # Common Chinese list conjunctions are safe to split only when they
        # introduce an obviously separate prohibition.  This keeps phrases
        # such as "攻击引战" intact while handling "后宫和靠新设定解题".
        body = re.sub(
            r"(?:以及|和)(?=(?:靠|用|无|不|连续|临时|极端|降智|万能|抄袭|洗稿))",
            "、",
            body,
        )
        for raw in re.split(r"[、，,；;]", body):
            term = raw.strip(" \t。；;，,")
            term = re.sub(r"^(?:禁止|避免|不要|不得|以及|及|和)", "", term).strip()
            if 2 <= len(term) <= 24:
                terms.append(term)
    return _dedupe(terms)


def project_rules_fingerprint(project_config=None, reveal_rules=None):
    """Hash every source that can change generated project publishing rules."""
    project_config = project_config if isinstance(project_config, dict) else {}
    reveal_rules = reveal_rules if isinstance(reveal_rules, dict) else {}
    payload = {
        "ruleset_version": FANQIE_RULESET_VERSION,
        "book_title": str(project_config.get("book_title") or "").strip(),
        "genre": str(project_config.get("genre_template") or "").strip(),
        "book_brief": str(project_config.get("book_brief") or "").strip(),
        "real_people_policy": str(project_config.get("real_people_policy") or "").strip(),
        "reveal_rules": reveal_rules,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_project_tool_rules(project_config=None, reveal_rules=None, existing_rules=None):
    project_config = project_config if isinstance(project_config, dict) else {}
    reveal_rules = reveal_rules if isinstance(reveal_rules, dict) else {}
    existing_rules = existing_rules if isinstance(existing_rules, dict) else {}
    rules = default_tool_rules()

    # Preserve manually curated cross-book facts that cannot be inferred from
    # the brief, while refreshing platform policy and reveal gates.
    for key in (
        "event_patterns", "event_completion_chapters", "old_name_patterns",
        "scanner_allowed_contexts", "memory_required_terms",
    ):
        if key in existing_rules:
            rules[key] = copy.deepcopy(existing_rules[key])

    brief = str(project_config.get("book_brief") or "").strip()
    avoid_terms = extract_theme_avoid_terms(brief)
    rules["theme_contract"] = {
        "book_title": str(project_config.get("book_title") or "").strip(),
        "genre": str(project_config.get("genre_template") or "").strip(),
        "core_direction": brief[:3000],
        "real_people_policy": str(project_config.get("real_people_policy") or "").strip(),
    }
    rules["theme_avoid_terms"] = avoid_terms
    rules["ruleset_metadata"]["generated_at"] = date.today().isoformat()
    rules["ruleset_metadata"]["project_fingerprint"] = project_rules_fingerprint(
        project_config, reveal_rules
    )

    gated = []
    for topic in reveal_rules.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        gate = int(topic.get("earliest_hard") or 0)
        label = str(topic.get("label") or "主线真相")
        if gate <= 0:
            continue
        for pattern in topic.get("hard_patterns") or []:
            pattern = str(pattern).strip()
            if pattern:
                # 初始化器要求这里使用简单中文关键词；转成安全的字面量正则，
                # 避免项目词中的括号、问号等被扫描器误当成正则语法。
                gated.append([re.escape(pattern), f"题材真相门控：{label}最早第{gate}章坐实", gate])
    rules["chapter_gated_terms"] = _dedupe(gated)

    for term in avoid_terms:
        rules["scanner_forbidden_terms"].append([
            re.escape(term), f"偏离本书主题：项目简介明确禁止“{term}”", 0
        ])
    rules["scanner_forbidden_terms"] = _dedupe(rules["scanner_forbidden_terms"])
    return rules


def render_rules_document(rules):
    rules = rules if isinstance(rules, dict) else default_tool_rules()
    metadata = rules.get("ruleset_metadata") or {}
    theme = rules.get("theme_contract") or {}
    lines = [
        "番茄发布与项目题材规则",
        "=" * 28,
        f"规则版本：{metadata.get('ruleset_version', FANQIE_RULESET_VERSION)}",
        "说明：本文件用于生成和质检提示，自动扫描不能替代平台实时审核。",
        "",
        "一、番茄发布基线",
    ]
    lines.extend(f"- {item}" for item in rules.get("platform_semantic_rules") or [])
    lines.extend(["", "二、项目题材合同"])
    lines.append(f"- 书名：{theme.get('book_title') or '未填写'}")
    lines.append(f"- 类型：{theme.get('genre') or '未填写'}")
    if theme.get("core_direction"):
        lines.append(f"- 创作方向：{theme['core_direction']}")
    if theme.get("real_people_policy"):
        lines.append(f"- 真人与原型边界：{theme['real_people_policy']}")
    avoid = rules.get("theme_avoid_terms") or []
    lines.append("- 题材禁区：" + ("；".join(avoid) if avoid else "未从项目简介识别，请人工补充"))
    lines.extend(["", "三、官方来源"])
    for item in metadata.get("official_sources") or FANQIE_OFFICIAL_SOURCES:
        lines.append(f"- {item.get('title')}（{item.get('published')}）：{item.get('url')}")
    return "\n".join(lines).strip() + "\n"
