# -*- coding: utf-8 -*-
"""Commercial project gate for one-click long-form novel generation.

The gate does not promise sales.  It turns a short user idea into an explicit,
auditable reader promise before the expensive chapter pipeline starts.
"""

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


SCHEMA_VERSION = 1
OFFICIAL_MARKET_URLS = (
    ("男频阅读榜", "https://fanqienovel.com/rank/1_1_257"),
    ("男频新书榜", "https://fanqienovel.com/rank/1_2_258"),
    ("女频新书榜", "https://fanqienovel.com/rank/0_1_748"),
)


def project_fingerprint(title, genre, book_brief, total_chapters):
    payload = json.dumps(
        {
            "title": str(title or "").strip(),
            "genre": str(genre or "").strip(),
            "book_brief": str(book_brief or "").strip(),
            "total_chapters": int(total_chapters or 0),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fetch_rank_page(label, url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
        html = raw.decode("utf-8", errors="ignore")
        book_ids = sorted(set(re.findall(r'"bookId"\s*:\s*"?(\d+)', html)))
        if not book_ids:
            book_ids = sorted(set(re.findall(r"bookId(?:%22|\")?\s*[:=]\s*(?:%22|\")?(\d+)", html)))
        return {
            "label": label,
            "url": url,
            "ok": True,
            "http_bytes": len(raw),
            "book_id_count": len(book_ids),
            "book_ids": book_ids[:30],
            # 番茄榜单正文可能使用字体映射。只记录可核验结构，不猜标题和标签。
            "content_scope": "official_rank_structure_only",
        }
    except Exception as exc:
        return {
            "label": label,
            "url": url,
            "ok": False,
            "book_id_count": 0,
            "error": str(exc)[:240],
            "content_scope": "unavailable",
        }


def fetch_fanqie_market_evidence(timeout=8, urls=None):
    """Fetch a bounded snapshot from official public Fanqie rank pages.

    Failure never blocks planning.  Sparse/obfuscated evidence is labelled so
    the model cannot turn it into invented market conclusions.
    """
    targets = tuple(urls or OFFICIAL_MARKET_URLS)
    rows = []
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(targets)))) as pool:
        futures = {
            pool.submit(_fetch_rank_page, label, url, timeout): (label, url)
            for label, url in targets
        }
        for future in as_completed(futures):
            rows.append(future.result())
    order = {url: index for index, (_label, url) in enumerate(targets)}
    rows.sort(key=lambda row: order.get(row.get("url"), 999))
    unique_ids = {
        book_id
        for row in rows
        for book_id in (row.get("book_ids") or [])
    }
    available_pages = sum(1 for row in rows if row.get("ok"))
    if available_pages >= 2 and len(unique_ids) >= 15:
        quality = "STRUCTURE_ONLY"
        confidence_cap = "MEDIUM"
    elif available_pages:
        quality = "SPARSE"
        confidence_cap = "LOW"
    else:
        quality = "UNAVAILABLE"
        confidence_cap = "LOW"
    return {
        "source": "番茄小说官方公开榜单",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "quality": quality,
        "market_confidence_cap": confidence_cap,
        "available_pages": available_pages,
        "unique_book_id_count": len(unique_ids),
        "limitations": (
            "仅核验官方榜单可访问性、榜单结构和样本量；页面字体映射或字段不足时，"
            "不得臆造书名、标签、收入、读完率或题材趋势。"
        ),
        "pages": rows,
    }


def parse_json_object(text):
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            value = json.loads(raw[start:end + 1])
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def build_planning_prompts(title, genre, book_brief, total_chapters, market_evidence, publishing_context=""):
    evidence_text = json.dumps(market_evidence or {}, ensure_ascii=False, indent=2)
    system_prompt = (
        "你是番茄长篇网文的商业立项总编。你的任务不是吹捧创意，而是在写几十万字之前，"
        "把题材改造成目标读者清楚、点击承诺明确、前三章能兑现、长线可持续的项目。"
        "【反同构与场景轮换铁律】"
        "1. 严禁连续超过两章出现封闭训练场内的同构对抗（传球失误、找第一接应、叫停签字）。"
        "2. 细纲规划必须在以下四类场景中强制轮换交替："
        "   - A. 正式正赛 / 关键公开测试（具有清晰胜负、比分压力与观众裁判）；"
        "   - B. 战术研判 / 更衣室与录像室（具有实质性观点交锋、权限博弈）；"
        "   - C. 校园学业 / 运动医学与身体代价（真实的成绩门槛、伤病恢复、家庭现实）；"
        "   - D. 队内合练 / 针对性战术推演（必须与上一场比赛的教训直接挂钩，且有不可逆的顺位或职责改变）。"
        "3. 严禁全员口吻同质化：不同角色必须具备鲜明性格差异与利益诉求，禁止全员机械复读'别替我决定/别替我喊'。"
        "你必须区分作品方案质量和真实市场数据；没有可靠数据时不得虚构热度、收入、读完率或竞品结论。"
        "输出必须是单个合法 JSON 对象，不要 Markdown，不要解释。"
    )
    user_prompt = f"""请对以下新书自动完成商业立项。允许优化书名和具体机制，但不得背离用户主题与禁用要求。

【用户输入】
原书名：{title}
题材：{genre}
主题/卖点/禁用要求：{book_brief}
计划篇幅：{int(total_chapters)}章

【番茄官方榜单证据快照】
{evidence_text}

【番茄发布与项目规则】
{(publishing_context or '暂无')[:7000]}

严格输出下列 JSON 字段：
{{
  "decision": "PROCEED或REVISE或REJECT",
  "commercial_score": 0到100的整数,
  "score_reasons": ["最多5条，说明方案本身的依据，不冒充真实流量数据"],
  "final_title": "最终采用的书名；原名足够好则保留",
  "title_options": ["共3个可点击但不标题党的备选，第一项必须等于final_title"],
  "target_reader": "具体读者画像与阅读场景",
  "reader_desire": "读者持续追读想得到的核心满足",
  "one_line_promise": "一句话说明主角、异常机制、主要冲突和独特收益",
  "core_mechanism": "可反复制造选择、代价和升级的故事发动机",
  "differentiation": ["至少3条可落到剧情的差异点"],
  "synopsis": "适合详情页的120到260字简介，先冲突后机制再悬念",
  "golden_three_contract": [
    {{"chapter": 1, "hook": "前800字钩子", "conflict": "当章冲突", "payoff": "当章兑现", "cliffhanger": "章末追读问题"}},
    {{"chapter": 2, "hook": "规则验证", "conflict": "现实阻力", "payoff": "阶段收益或代价", "cliffhanger": "章末追读问题"}},
    {{"chapter": 3, "hook": "卖点升级", "conflict": "更强选择", "payoff": "首个小闭环", "cliffhanger": "不可逆变化和更大威胁"}}
  ],
  "retention_contract": {{
    "every_chapter": "每章必须推进或兑现什么",
    "every_3_chapters": "每3章的变化",
    "every_10_chapters": "每10章的阶段闭环",
    "every_30_chapters": "每30章的差异化高潮"
  }},
  "long_form_engine": "为什么这个机制足以支撑{int(total_chapters)}章且不会只换敌人重复",
  "arc_promises": ["至少3个递进阶段，每项写清新问题与新兑现"],
  "failure_risks": ["至少3条最可能扑街的具体原因及预警信号"],
  "must_avoid": ["至少3条必须禁用的套路或内容"],
  "market_confidence": "HIGH或MEDIUM或LOW"
}}

评分规则：
1. 评分评的是当前最终方案能否进入生产，不是预测收入；低于78分不得写PROCEED。
2. market_confidence 不得高于证据快照中的 market_confidence_cap。
3. final_title 必须让读者快速知道题材冲突或核心机制，不能只剩抽象宏大概念。
4. 黄金三章每章都要有独立变化；第3章必须完成首个小闭环，不能只埋谜。
5. 长线发动机必须包含升级、代价、关系变化和阶段终点，不能只写“不断遇到更强敌人”。
"""
    return system_prompt, user_prompt


def build_revision_prompts(original_system, original_user, previous, issues, min_score):
    previous_text = json.dumps(previous or {}, ensure_ascii=False, indent=2)
    issue_text = "；".join(issues or ["方案未通过商业立项门槛"])
    return (
        original_system,
        original_user
        + f"""

【上一版未通过，必须完整重做】
门槛：decision=PROCEED 且 commercial_score>={int(min_score)}。
问题：{issue_text}
上一版：
{previous_text[:12000]}

请修复具体方案后重新输出完整 JSON。不能靠虚高打分过关；若主题确实无法形成可持续商业故事，保持REJECT。
""",
    )


def build_gate_review_prompts(title, genre, book_brief, total_chapters, candidate, market_evidence):
    system_prompt = (
        "你是独立商业立项否决人。你没有参与方案创作，职责是找出会导致点击弱、前三章流失、"
        "中期重复或平台不适配的具体问题。不得因为文档完整就放行，不得虚构市场数据。"
        "只输出单个合法 JSON 对象，不要 Markdown。"
    )
    user_prompt = f"""独立复核下面的新书方案。

【原始需求】
原书名：{title}
题材：{genre}
主题与禁用要求：{book_brief}
计划篇幅：{int(total_chapters)}章

【候选立项方案】
{json.dumps(candidate or {}, ensure_ascii=False, indent=2)[:18000]}

【证据边界】
{json.dumps(market_evidence or {}, ensure_ascii=False, indent=2)[:5000]}

只输出：
{{
  "decision": "PROCEED或REVISE或REJECT",
  "commercial_score": 0到100的整数,
  "gate_failures": ["0到5条可定位的硬伤"],
  "required_changes": ["0到5条下一版必须完成的修改"],
  "audit_summary": "一句话结论"
}}

必须逐项检查：
1. 书名和简介能否让陌生读者快速理解冲突、机制与收益，是否标题党。
2. 目标读者是否具体，核心满足是否能在正文反复兑现。
3. 黄金三章是否每章都有变化，第3章是否真的闭环而非继续拖谜。
4. 核心机制是否同时产生收益、限制、代价和关系变化。
5. {int(total_chapters)}章长线是否有至少三个性质不同的阶段，是否会沦为换敌人重复。
6. 差异点能否落到事件而不是形容词，失败风险是否有真实预警信号。
7. 是否遵守用户禁用要求与番茄内容边界。

评分纪律：任一项存在结构性硬伤，最高77分且decision不得为PROCEED；没有真实数据时只评方案质量，不预测收入。
"""
    return system_prompt, user_prompt


def apply_gate_review(candidate, review):
    result = dict(candidate or {})
    gate = dict(review or {})
    try:
        gate_score = int(gate.get("commercial_score") or 0)
    except (TypeError, ValueError):
        gate_score = 0
    gate_decision = str(gate.get("decision") or "REVISE").upper()
    result["planner_score"] = int(result.get("commercial_score") or 0)
    result["commercial_score"] = max(0, min(100, gate_score))
    result["decision"] = gate_decision
    result["gate_review"] = {
        "decision": gate_decision,
        "commercial_score": result["commercial_score"],
        "gate_failures": gate.get("gate_failures") or [],
        "required_changes": gate.get("required_changes") or [],
        "audit_summary": str(gate.get("audit_summary") or "").strip(),
    }
    return result


def normalize_blueprint(data, title, genre, book_brief, total_chapters, market_evidence):
    result = dict(data or {})
    try:
        result["commercial_score"] = int(result.get("commercial_score") or 0)
    except (TypeError, ValueError):
        result["commercial_score"] = 0
    result["decision"] = str(result.get("decision") or "REVISE").upper()
    result["market_confidence"] = str(result.get("market_confidence") or "LOW").upper()
    cap = str((market_evidence or {}).get("market_confidence_cap") or "LOW").upper()
    levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    if levels.get(result["market_confidence"], 0) > levels.get(cap, 0):
        result["market_confidence"] = cap
    result["schema_version"] = SCHEMA_VERSION
    result["original_title"] = str(title or "").strip()
    result["genre"] = str(genre or "").strip()
    result["book_brief"] = str(book_brief or "").strip()
    result["total_chapters"] = int(total_chapters or 0)
    result["project_fingerprint"] = project_fingerprint(
        title, genre, book_brief, total_chapters
    )
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    result["market_evidence"] = market_evidence or {}
    return result


def validate_blueprint(blueprint, min_score=78, require_gate=True):
    data = blueprint or {}
    issues = []
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        issues.append("商业蓝图版本无效")
    score = int(data.get("commercial_score") or 0)
    if not 0 <= score <= 100:
        issues.append("商业评分必须为0到100")
    if require_gate and score < int(min_score):
        issues.append(f"商业评分{score}低于门槛{int(min_score)}")
    decision = str(data.get("decision") or "").upper()
    if decision not in {"PROCEED", "REVISE", "REJECT"}:
        issues.append("立项结论无效")
    if require_gate and decision != "PROCEED":
        issues.append(f"立项结论为{decision or '空'}")
    gate_review = data.get("gate_review")
    if require_gate and not isinstance(gate_review, dict):
        issues.append("商业蓝图缺少独立复核结论")
    if isinstance(gate_review, dict):
        gate_decision = str(gate_review.get("decision") or "").upper()
        try:
            gate_score = int(gate_review.get("commercial_score") or 0)
        except (TypeError, ValueError):
            gate_score = -1
        if require_gate and gate_decision != "PROCEED":
            issues.append(f"独立复核结论为{gate_decision or '空'}")
        if require_gate and gate_score != score:
            issues.append("独立复核评分与商业蓝图评分不一致")
        if require_gate and not str(gate_review.get("audit_summary") or "").strip():
            issues.append("独立复核缺少审查摘要")
        failures = [str(item).strip() for item in (gate_review.get("gate_failures") or []) if str(item).strip()]
        if failures:
            issues.extend(f"独立复核：{item}" for item in failures[:5])
    for field, label, minimum in (
        ("final_title", "最终书名", 2),
        ("target_reader", "目标读者", 12),
        ("reader_desire", "读者欲望", 12),
        ("one_line_promise", "一句话承诺", 18),
        ("core_mechanism", "核心机制", 18),
        ("synopsis", "作品简介", 60),
        ("long_form_engine", "长线发动机", 30),
    ):
        if len(str(data.get(field) or "").strip()) < minimum:
            issues.append(f"{label}过短或缺失")
    for field, label, minimum in (
        ("title_options", "书名备选", 3),
        ("differentiation", "差异化", 3),
        ("arc_promises", "阶段承诺", 3),
        ("failure_risks", "失败风险", 3),
        ("must_avoid", "禁用项", 3),
    ):
        value = data.get(field)
        if not isinstance(value, list) or len(value) < minimum:
            issues.append(f"{label}不足{minimum}条")
    title_options = data.get("title_options") or []
    if title_options and str(title_options[0]).strip() != str(data.get("final_title") or "").strip():
        issues.append("首选书名与最终书名不一致")
    opening = data.get("golden_three_contract")
    if not isinstance(opening, list) or {item.get("chapter") for item in opening if isinstance(item, dict)} != {1, 2, 3}:
        issues.append("黄金三章合同不完整")
    else:
        for item in opening:
            if any(not str(item.get(field) or "").strip() for field in ("hook", "conflict", "payoff", "cliffhanger")):
                issues.append(f"第{item.get('chapter')}章合同字段不完整")
                break
    retention = data.get("retention_contract")
    required_retention = ("every_chapter", "every_3_chapters", "every_10_chapters", "every_30_chapters")
    if not isinstance(retention, dict) or any(not str(retention.get(key) or "").strip() for key in required_retention):
        issues.append("追读节奏合同不完整")
    return issues


def blueprint_matches(blueprint, title, genre, book_brief, total_chapters, min_score=78):
    if not isinstance(blueprint, dict):
        return False
    if blueprint.get("project_fingerprint") != project_fingerprint(
        title, genre, book_brief, total_chapters
    ):
        return False
    return not validate_blueprint(blueprint, min_score=min_score, require_gate=True)


def render_blueprint(blueprint, max_chars=7000):
    data = blueprint or {}
    opening_lines = []
    for item in data.get("golden_three_contract") or []:
        if not isinstance(item, dict):
            continue
        opening_lines.append(
            f"第{item.get('chapter')}章：钩子={item.get('hook', '')}；冲突={item.get('conflict', '')}；"
            f"兑现={item.get('payoff', '')}；章末={item.get('cliffhanger', '')}"
        )
    retention = data.get("retention_contract") or {}
    lines = [
        "# 商业立项执行合同",
        f"最终书名：{data.get('final_title', '')}",
        f"立项评分：{data.get('commercial_score', 0)}（只代表方案质量，不代表收入预测）",
        f"目标读者：{data.get('target_reader', '')}",
        f"读者欲望：{data.get('reader_desire', '')}",
        f"一句话承诺：{data.get('one_line_promise', '')}",
        f"核心机制：{data.get('core_mechanism', '')}",
        "差异化：" + "；".join(str(item) for item in (data.get("differentiation") or [])),
        f"作品简介：{data.get('synopsis', '')}",
        "黄金三章：",
        *opening_lines,
        "追读节奏："
        f"每章={retention.get('every_chapter', '')}；"
        f"每3章={retention.get('every_3_chapters', '')}；"
        f"每10章={retention.get('every_10_chapters', '')}；"
        f"每30章={retention.get('every_30_chapters', '')}",
        f"长线发动机：{data.get('long_form_engine', '')}",
        "阶段承诺：" + "；".join(str(item) for item in (data.get("arc_promises") or [])),
        "高风险：" + "；".join(str(item) for item in (data.get("failure_risks") or [])),
        "必须避免：" + "；".join(str(item) for item in (data.get("must_avoid") or [])),
    ]
    return "\n".join(lines)[:max_chars]
