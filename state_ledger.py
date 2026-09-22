"""Evidence-backed long-term story state for chapter-by-chapter generation.

The ledger is intentionally conservative: a model may propose state changes, but
only changes carrying an exact quote from the saved chapter are accepted.  The
materialized state can be rebuilt from immutable per-chapter delta files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DELTA_SCHEMA_VERSION = 2
DELTA_CHAIN_GENESIS = "0" * 64
ALLOWED_EVENT_TYPES = {
    "conflict",
    "bond",
    "faction",
    "world",
    "reveal",
    "resource",
    "investigation",
    "other",
}
EVENT_TYPE_LABELS = {
    "conflict": "冲突/战斗",
    "bond": "关系/情感",
    "faction": "阵营/权力",
    "world": "世界扩展",
    "reveal": "真相揭示",
    "resource": "资源得失",
    "investigation": "调查推进",
    "other": "其他",
}
DEFAULT_EVENT_COOLDOWNS = {
    "conflict": 2,
    "bond": 1,
    "faction": 2,
    "world": 3,
    "reveal": 2,
    "resource": 1,
    "investigation": 1,
    "other": 0,
}
MAX_ITEMS = {
    # One person may need separate evidence rows for knowledge and final location.
    # Keep a finite structural bound without treating 16 rows as 16 characters.
    "characters": 32,
    "resources": 20,
    "relationships": 12,
    "hooks": 12,
    "subplots": 10,
    "events": 10,
}

# Extraction and omission review must agree on who actually received a fact.
KNOWLEDGE_EVIDENCE_RULE = (
    "人物知识须由连续引文明示接收者及其获知的具体内容；在场、摆筷子或相邻段落出现不等于听到。"
    "发送、转发或群发消息只证明发送动作，不能据此认定收件人已经收到、读到或获知内容。"
    "发信人向第三方回复“收到”，不是其他收件人的接收确认；必须逐一核对目标角色与同一具体信息。"
    "一句答复后接旁白回顾不得自动扩成向同场其他角色复述全部历史内容。"
    "但叙述明确写出某人亲历、听到、读到或被告知时，仍可按原文登记该人的对应知识，"
    "不能把所有叙述句一概排除，不强制要求对话回执；历史亲历也不自动变成本章新获知。"
    "发送动作本身仍可按原文记入events，不得为填知识字段要求正文新增收信情节。"
)

_MESSAGE_SEND_RE = re.compile(
    r"(?:发来|发给|发送|转发|群发|发出|发了(?:一条|一份|消息|通知))"
)
_EXPLICIT_RECEIPT_VERBS = (
    r"(?:点开|打开|读到|读完|读了|看完|看见|看到|看清|听到|听见|得知|获知|"
    r"被告知|收到(?:了)?(?:消息|通知|文件|结果)?|看了[一二两三四五六七八九十几]+遍|"
    r"记下|记进|回复|确认收到)"
)


def _knowledge_quote_is_send_only(name: str, quote: str) -> bool:
    """Check that a message recipient is explicitly shown acquiring content."""
    name = _compact_text(name, 40)
    quote = _compact_text(quote, None)
    if not name or not quote or not _MESSAGE_SEND_RE.search(quote):
        return False
    target = re.escape(name)
    explicit_patterns = (
        rf"{target}[^。！？\n]{{0,80}}{_EXPLICIT_RECEIPT_VERBS}",
        rf"(?:告诉|告知|通知){target}",
        rf"{target}(?:被)?(?:明确)?(?:告诉|告知)",
        rf"{_EXPLICIT_RECEIPT_VERBS}[^。！？\n]{{0,40}}{target}",
    )
    return not any(re.search(pattern, quote) for pattern in explicit_patterns)


def _missing_knowledge_target(reason: str) -> str:
    reason = _compact_text(reason, 180)
    reason = re.sub(r"^(?:漏记|未登记|遗漏)", "", reason)
    match = re.match(
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,20}?)(?:明确)?(?:新)?"
        r"(?:获知|得知|知道)",
        reason,
    )
    if match:
        return match.group("name")
    match = re.match(
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,20}?)的?knowledge_add",
        reason,
        flags=re.IGNORECASE,
    )
    return match.group("name") if match else ""


_TRANSIENT_LOCATION_RE = re.compile(
    r"(?:边线外|场地另一端|电脑旁|打印机旁|门边|门旁|器材筐旁|标志盘旁|"
    r"左侧窄道|右侧窄道|身前|身后|脚边)"
)
_DEPARTURE_RE = re.compile(
    r"(?:转身(?:去|走|跑)|走到|走向|离开|跑向|追(?:上|向|最后)|回到|移到|"
    r"来到|上车|下车|进入|进了)"
)


def _missing_location_is_transient(reason: str, quote: str, chapter_text: str) -> bool:
    reason = _compact_text(reason, 180)
    cleaned = re.sub(r"^(?:漏记|未登记|遗漏)", "", reason)
    if not re.search(r"现实(?:位置|地点)", cleaned):
        return False

    target_match = re.search(
        r"地点可最小记录为[“\"](?P<location>[^”\"]+)[”\"]",
        cleaned,
    ) or re.search(
        r"现实(?:位置|地点)[：:](?P<location>[^。；;]+)",
        cleaned,
    )
    location = (
        target_match.group("location").rstrip("。；; ")
        if target_match else ""
    )
    quote_text = _compact_text(quote, None)
    if location and location not in quote_text:
        # A missing-field request must meet the same direct-evidence standard as
        # an existing claim.  Do not materialize a broad scene name that appears
        # only in the auditor's prose while the supplied quote names merely a
        # desk-side/process position.
        return True
    if not _TRANSIENT_LOCATION_RE.search(location or quote_text):
        return False
    chapter = _compact_text(chapter_text, None)
    if _DEPARTURE_RE.search(quote_text):
        return True
    name_match = re.match(
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9·]{2,20}?)(?:本章|明确|章末|仍在|位于)",
        cleaned,
    )
    name = name_match.group("name") if name_match else ""
    if not name:
        return False
    quote_pos = chapter.find(quote_text)
    if quote_pos < 0:
        return False
    later = chapter[quote_pos + len(quote_text):]
    return bool(
        re.search(
            rf"{re.escape(name)}[^。！？\n]{{0,50}}{_DEPARTURE_RE.pattern}",
            later,
        )
    )


def _audit_reject_misreads_sparse_location(
    path: str, reason: str, delta: dict[str, Any]
) -> bool:
    """Ignore an audit claim that a sibling NO_CHANGE row resets location.

    Character rows are sparse field deltas.  A NO_CHANGE row contributes no
    location operation, so it cannot overwrite a SET/UNKNOWN row for the same
    character elsewhere in the same chapter delta.
    """
    match = re.fullmatch(r"characters\[(\d+)\]", path)
    if not match or "NO_CHANGE" not in reason.upper() or "继承" not in reason:
        return False
    rows = delta.get("characters", [])
    index = int(match.group(1))
    if not isinstance(rows, list) or not 0 <= index < len(rows):
        return False
    row = rows[index]
    if not isinstance(row, dict) or str(row.get("location_state") or "").upper() != "NO_CHANGE":
        return False
    name = str(row.get("name") or "").strip()
    if not name:
        return False
    return any(
        isinstance(sibling, dict)
        and str(sibling.get("name") or "").strip() == name
        and str(sibling.get("location_state") or "").upper() in {"SET", "UNKNOWN"}
        for sibling in rows
    )


# Optional additive field: absent/empty progression must not change old deltas.
PROGRESSION_EVIDENCE_RULE = (
    "characters.progression只登记主角当前已生效阶段的规范标签，独立成条并附连续正文证据。"
    "标签只能来自提供的项目realm_order；未提供主角和阶段表时省略此字段。"
    "正文自然说法可以与规范标签同义，但须由独立证据审计确认，不要求作者把标签硬写进正文。"
    "证据须同时明确主体和已经生效的身份、注册或境界；申请、候选、意向、条件、将来计划、"
    "他人身份和仅属过去的回忆都不能更新当前阶段，不得从年龄、章号、赛季或大纲推断升级。"
    "本章明确的新阶段不能仅记在knowledge_add或events而漏掉progression；重复旧状态不强制新增。"
    "不确定是否等价时不填标签；不能把未注册的候选评价映射成已注册球员。"
)


def _progression_context_prompt(context: dict[str, Any] | None) -> str:
    context = context if isinstance(context, dict) else {}
    protagonist = str(context.get("protagonist") or "").strip()
    order = context.get("realm_order")
    if not protagonist or not isinstance(order, list) or not order:
        return "未提供阶段表；省略progression，不自行创造标签。"
    return json.dumps({
        "protagonist": protagonist,
        "realm_order": order,
        "previous_progression": context.get("current_realm") or "",
        "note": "仅提供主体和规范词表，不是本章生效证据。",
    }, ensure_ascii=False)


class StateLedgerError(RuntimeError):
    pass


class StateExtractionError(StateLedgerError):
    """Repair bookkeeping or retry its reviewer; never rewrite valid prose."""


class StateProseConflictError(StateLedgerError):
    """An independently grounded rejection explicitly targets the draft."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chapter_sha256(text: str) -> str:
    return _sha256_text(text)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compact_text(value: Any, limit: int | None = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Truncation can land on a normalized separator.  Strip again so a second
    # validation pass produces the exact same payload as the first one.
    return text[:limit].strip()


def _normalized_evidence(value: Any) -> str:
    """Normalize only for quote containment; never use this as stored evidence."""
    text = str(value or "").lower()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def _valid_quote(quote: Any, chapter_text: str) -> bool:
    needle = _normalized_evidence(quote)
    haystack = _normalized_evidence(chapter_text)
    return len(needle) >= 4 and needle in haystack


def _stable_id(prefix: str, *values: Any) -> str:
    raw = "|".join(_normalized_evidence(v) for v in values if str(v or "").strip())
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _safe_id(value: Any, prefix: str, label: str) -> str:
    candidate = re.sub(r"[^0-9A-Za-z_\-]", "", str(value or ""))[:48]
    return candidate or _stable_id(prefix, label)


def initial_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "current_chapter": 0,
        "updated_at": "",
        "characters": {},
        "relationships": {},
        "resources": {},
        "hooks": {},
        "subplots": {},
        "timeline": [],
        "event_history": [],
        "applied_chapters": {},
        "delta_count": 0,
        "ledger_tip_sha256": DELTA_CHAIN_GENESIS,
    }


def parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise StateLedgerError("状态增量不是有效 JSON 对象")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StateLedgerError(f"状态增量 JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise StateLedgerError("状态增量顶层必须是 JSON 对象")
    return payload


def build_extraction_prompts(
    chapter_number: int,
    chapter_text: str,
    outline: str = "",
    prior_context: str = "",
    *,
    progression_context: dict[str, Any] | None = None,
) -> tuple[str, str]:
    system_prompt = (
        "你是小说正史记账员，不是续写作者。只登记本章正文明确发生、明确说出或明确持有的事实。"
        "严禁推测、补全动机、把读者知道的秘密写成角色知道、把计划写成已完成。"
        "每一条变化必须附正文中逐字复制的 evidence_quote；找不到原句就不要输出该条。"
        "每个对象的同一条 evidence_quote 必须直接支持该对象全部非空字段；若一句话只支持能力，"
        "location、condition、emotion、status、knowledge_add 必须留空，不得用同章其他段落补齐。"
        "涉及人物知识、能力、资源保管或关系归属时，evidence_quote 必须连续包含人物姓名或无歧义身份，"
        "不能只用‘他’‘她’‘我’‘他说’等代词让审计员猜主体。"
        "复合变化要拆成多个最小对象，禁止把人物在场、情绪、知识、能力、地点和状态捆进一条。"
        "本章连续原文明示人物已到达或位于新地点，且该位置是章末仍有效的现实地点时，"
        "须用最小characters.location条目记录，不能只写事件摘要而沿用旧地点。"
        "地点不要求专有场馆名：球场内、车厢内等正文明确的现实位置也可最小记录，"
        "不能补造城市、场馆名或未经描写的移动路线。characters.location_state必须区分三种语义："
        "SET表示正文明确支持location中的章末地点，NO_CHANGE表示本章没有证据证明旧地点失效，"
        "UNKNOWN表示正文明确证明旧地点已失效、但最终位置没有明确写出；UNKNOWN时location必须为空。"
        "空location配NO_CHANGE才会继承前章旧地点；"
        "若能力条目引文不支持地点，仍须为有独立连续原文支持的章末位置单独增加地点条目，"
        "不得把稀疏字段规则误用为整章不记现实地点变化。"
        "逐一核对本章实际出场人物，包括父母、教练等配角，不得只核对主角或delta已有姓名。"
        "配角地点有本章明确证据时也须单列，不能因其无新知识或能力变化而漏记；"
        "称谓须结合已验证关系消歧，不得猜认身份。"
        "已离开的瞬时站位不作章末地点；‘站在边线外’、‘场地另一端收球后转身追球’、"
        "‘走到电脑旁’等过程动作不能仅因是人物最后一次出场就写成章末SET。"
        "人物仍在同一场景时，优先记录有连续原文支持的较粗场景，"
        "不能通过NO_CHANGE继承已失效的旧地点，也不能猜测移动终点；这种情况使用UNKNOWN显式清除陈旧地点。"
        "引文须同时明确人物主体与地点；计划、回忆、梦境或灰影的位置不更新现实地点，"
        "没有明确位置变化的角色不强制填地点，也不得按大纲推断移动。"
        "资源quantity只填本次事件之后正文明确的余额或持有总量，不能填本次增减量；"
        "quantity_change填写本次增减量。正文没有明确余额时quantity必须为null。"
        "明确的事后余额优先于从稀疏事件增减量推算的余额。"
        "正文明确发生的资源消耗、取得、锁定或解锁不得漏记；使用最小且有证据的字段，"
        "同一场次/批次的余额优先沿用已有资源item名称，避免新建别名却留下旧余额。"
        "影响后续连续性的纸张、笔记、表格或文件发生批注、改写、签署、转交或归还时，"
        "也须在resources记录有连续原文支持的内容或保管状态变化；沿用已有item，"
        "内容修改用SET及最小status描述，不得把内容修改记成新增一份物品。"
        "数量无明确变化时quantity和quantity_change留null；不同段落的变化拆成独立条目，"
        "每条均须包含明确主体和动作的原文，不能拼接引文或自行补出授权与所有权。"
        "影响后续行动的新通知、报名材料等实物的明确领取或收存须记resources；"
        "角色本章明确新得知的关键日期、出发安排或行动限制须记characters.knowledge_add，"
        "不能只用events代替；不要求登记全部闲聊、已知信息或旁白信息，不得补造未给出的具体时刻。"
        "普通赞同、简短评价或对已执行动作的重复反馈不强制记knowledge_add，账本不是逐句对话转录；"
        "新的指令、约定、限制或影响后续行动的具体信息仍须记录，不能因为说得简短就省略。"
        "人物评价不能无归因地当成客观事实；若登记评价，只记谁明确表达了什么。"
        + KNOWLEDGE_EVIDENCE_RULE + PROGRESSION_EVIDENCE_RULE +
        "同一物品的交接须体现章末最终保管状态，沿用同一item，不得用别名留下交出者仍持有的并列现态。"
        "若分条记录中间修改与最终交接，交出者条目的最终status须由交接原句支持；"
        "已有旧持有记录须明确更新为已交出，接收者只登记原文支持的保管，不把保管转移当作所有权转移。"
        "已有伏笔只有在正文明确证明同一来源或同一因果时才能推进；相似设备动作必须新开伏笔，"
        "不得擅自并入旧伏笔。宁可少填没有证据的字段，不得漏掉已有连续原文支持的关键变化。"
        "只输出一个 JSON 对象，不要 Markdown，不要解释。"
    )
    user_prompt = f"""请提取第{chapter_number}章的结构化正史增量。

数组容量上限（按最小证据条目计数，不是人数）：{json.dumps(MAX_ITEMS, ensure_ascii=False)}。
完全相同的条目可以去重；同一连续引文支持的同一人物字段可以合并，不同证据不得拼接。
超限必须报错，不得按顺序裁掉独有事实；不能靠删掉有证据的关键变化来凑容量。

输出结构（没有变化的数组必须为 []）：
{{
  "chapter": {chapter_number},
  "characters": [{{
    "name": "角色名", "location": "仅明确地点或空字符串",
    "location_state": "SET|UNKNOWN|NO_CHANGE",
    "condition": "仅明确身体/生死状态或空字符串",
    "emotion": "仅正文明确情绪或空字符串",
    "status": "active|missing|dead|unknown 或空字符串",
    "progression": "可省略；仅主角已生效阶段的规范标签",
    "knowledge_add": ["本章新明确得知的事实"],
    "abilities_add": ["本章新明确获得/展现的能力"],
    "evidence_quote": "正文逐字原句"
  }}],
  "resources": [{{
    "owner": "正文明确的所有、持有或保管主体；若只证明保管，写‘某人（保管）’",
    "item": "资源/物品", "action": "GAIN|USE|LOSE|SET",
    "quantity_change": null, "quantity": null, "status": "当前明确状态或空字符串",
    "evidence_quote": "正文逐字原句"
  }}],
  "relationships": [{{
    "a": "角色A", "b": "角色B", "type": "关系类型",
    "status": "当前明确状态", "evidence_quote": "正文逐字原句"
  }}],
  "hooks": [{{
    "id": "已有伏笔ID；新伏笔留空", "label": "伏笔短名",
    "action": "OPEN|ADVANCE|RESOLVE", "due_by": null,
    "evidence_quote": "正文逐字原句"
  }}],
  "subplots": [{{
    "id": "已有支线ID；新支线留空", "label": "支线短名",
    "action": "OPEN|ADVANCE|RESOLVE", "due_by": null,
    "evidence_quote": "正文逐字原句"
  }}],
  "events": [{{
    "event_type": "conflict|bond|faction|world|reveal|resource|investigation|other",
    "summary": "已发生事件的一句话客观摘要",
    "time_anchor": "正文明确出现的时间短语；没有则空字符串",
    "duration": "正文明确出现的时长短语；没有则空字符串",
    "location": "本事件原句明确出现的地点；没有则空字符串",
    "evidence_quote": "同时支撑摘要及上述非空时间/地点字段的正文逐字原句"
  }}]
}}

硬规则：
1. evidence_quote 必须能在下方正文中连续找到，至少 4 个有效字符。
2. 同一证据只能支撑它明确表达的变化；不要从语气、常识或大纲推断。
3. knowledge_add 只登记该角色本章新知道的内容；旁白披露不等于角色知道。
4. 计划、预告、梦境、假设和传闻不得登记成已完成事件，除非摘要明确标注其性质。
5. 数量不明确就填 null，禁止估算。
6. 大纲只帮助识别遗漏，不是事实证据；事实必须来自正文。
7. events 的 time_anchor、duration、location 只可逐字摘录 evidence_quote 中明确出现的短语；
   没写明就填空字符串，禁止把章节顺序、常识或上一章地点推断成本章时间地点。
8. characters 每个对象只登记 evidence_quote 直接支撑的字段。不得因为角色在本章出现就自动填写地点、active、情绪或知识；
   同一角色可拆成多个最小对象，每个对象使用各自的连续证据。location_state=SET时location必须是引文支持的地点；
   旧地点已失效但最终位置没有明确写出时用UNKNOWN且location留空；没有地点变化证据时用NO_CHANGE且location留空。
9. 伏笔推进必须由 evidence_quote 明确建立与已有伏笔的同一性；仅仅同为联网、擦除、重连或设备异常，不得合并。
10. resources.owner 不等于法律所有权。证据只说明暂存、封存或放入保险柜时，必须标成“某人（保管）”，不得写成所有者。
11. abilities_add 和 events.summary 不得添加“按授权范围”“已经获批”“合法”“正式”等评价，除非同一 evidence_quote 明确出现该事实。
12. 涉及人物、保管人、签署人或关系主体的字段，evidence_quote 必须连续出现对应姓名或无歧义身份；
    仅含“他”“她”“我”“他说”“我留存”等代词的引文不得用于归属主体，宁可不登记。
13. duration 只登记事件持续了多久。两个时间点之间的差值、年龄差、时间异常或统计间隔只能写进摘要，
    不得填入 duration；若正文未明确某事件“持续”该时长，duration 必须为空字符串。
14. time_anchor 必须是 summary 所述事件本身的发生时间。页面当前刷新、人物当前查看或本章当前发现时，
    引文中某份通知、档案、录音或日志的历史生成时间只属于被查看材料，不得填成当前事件的 time_anchor；
    可把该历史时间写进客观摘要，time_anchor 留空。
    未来活动日期不等于当前获知时间：当前收到通知、读到赛程或得知安排时，通知里的未来日期
    只属于预定活动，不能填成当前获知事件的time_anchor。引文没有当前获知时间就留空；
    可以在summary中注明未来计划，不能把尚未进行的选拔或比赛写成已发生。
15. 到达、站在门外、携带材料或等待核验不等于已经报到、获准进入或办完手续；只有 evidence_quote
    明写核验完成、登记完成或手续办完，summary 才能使用相应完成态动词。
16. hooks.label 与 subplots.label 也是待证事实。正文只写候选顺序、分组或排序时，标签只能写相应的
    “候选排序”“训练分组”等中性短名，不得扩写成争夺名额、淘汰竞争或已获注册。
17. events.summary 中每个关键主语、动作和结果都必须由同一 evidence_quote 直接写出。若摘要写“甲传球、
    乙射门”，引文必须同时包含甲的出球动作与乙的射门结果；只引用乙接球、加速或射门，不得反推甲已传球。
18. 人物能力和事件摘要须保持正文动作顺序，不得倒置先后关系；不明因果时只记录可见动作。
19. resources的GAIN必须有取得或转交动作；引文仅说明持有、收好时，用SET记录该持有状态，不能反推取得过程。
20. 支线仅重申待定、顺位不变或重复已知目标时，不能登记为ADVANCE；有新的行动或结果实质改变进程才可推进。
21. 预定结束时间不能证明已经完成。引文仍在倒计时、等待或说明日程时，不能据此登记已完成；
    须把交卷、收卷、终场或签署等实际完成动作纳入连续引文，或将摘要收窄为该引文明确的进行态。
    全文后来确已完成，也不能用早先的计划段落单独证明完成态。
22. 相对日期不得凭同章其他段落扩写：引文只有“那天”“明天”时，knowledge_add、status和summary
    不得另加未在该连续引文明确出现的年月日；保留原文的相对说法或省去具体日期。
    若具体日期是必要事实，扩大连续引文同时涵盖日期和动作，不能拼接前后段。

阶段词表（仅用于规范命名，不可作为本章证据）：
{_progression_context_prompt(progression_context)}

已有正史上下文（只用于识别已有 ID 和避免重复，不可作为本章证据）：
{prior_context[:7000] or "（空）"}

本章大纲（不可作为证据）：
{outline[:4000] or "（空）"}

第{chapter_number}章正文：
---
{chapter_text}
---
"""
    return system_prompt, user_prompt


def build_verified_prose_context(
    state: dict[str, Any],
    chapter: int,
    chapter_paths: dict[int, Any],
    *,
    max_chars: int = 12000,
) -> str:
    """Read up to two complete recent formal chapters bound to the state hashes."""
    applied = state.get("applied_chapters", {})
    recent = sorted(
        ((int(number), path) for number, path in chapter_paths.items()
         if 0 < int(number) < int(chapter)), reverse=True,
    )[:2]
    blocks = []
    remaining = max(0, int(max_chars))
    for number, path in recent:
        expected = str(applied.get(str(number)) or "")
        if not expected:
            continue
        text = Path(path).read_text(encoding="utf-8")
        if chapter_sha256(text) != expected:
            raise StateLedgerError(f"第{number}章历史正式原文哈希与正史不一致")
        block = f"【历史正式第{number}章，SHA256={expected}】\n{text}\n【该章结束】"
        if len(block) > remaining:
            continue  # Never present a truncated chapter as complete evidence.
        blocks.append(block)
        remaining -= len(block) + 2
    return "\n\n".join(reversed(blocks))


def build_delta_audit_prompts(
    delta: dict[str, Any],
    state_context: str = "",
    chapter_text: str = "",
    verified_source_context: str = "",
    *,
    progression_context: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Audit evidence entailment and draft continuity in one independent pass."""
    claims = []
    for key in MAX_ITEMS:
        for index, item in enumerate(delta.get(key, [])):
            claim = {k: v for k, v in item.items() if k != "evidence_quote"}
            claims.append(
                {
                    "path": f"{key}[{index}]",
                    "claim": claim,
                    "evidence_quote": item.get("evidence_quote", ""),
                }
            )
    system_prompt = (
        "你是独立的小说正史证据与连续性审计员。先逐条判断 claim 是否由 evidence_quote 直接支持，"
        "再检查整章正文是否违反已有正史。不要使用常识、上下文猜测或大纲补全。"
        "结构化账本是选择性摘要，不穷尽历史事实；历史原文有明确依据的回顾，"
        "不能因为摘要未登记而判为未发生。应查阅附带且哈希核验过的历史正式原文，"
        "区分角色亲历/已获知与仅旁白披露；无原文来源的秘密或能力仍不得臆造。"
        "历史原文只用于核对历史连续性，不能替代本章条目的 evidence_quote。"
        "历史状态后续若已改变，仍以明确的后续变化为准，不得用早期原文复活旧资源。"
        "证据只支持一部分字段，也必须拒绝整条并说明字段。尤其注意：旁白知道不等于角色知道；"
        "打算做不等于已经做；疑似或传闻不等于事实；拿起不一定等于永久持有；"
        "受威胁不等于受伤；能力使用必须在证据中明确。还要检查角色凭空知道秘密、"
        "死者无解释复活、已丢失或耗尽资源重新出现、伤势与动作冲突、地点瞬移、"
        "未获得能力突然使用、已解决伏笔原样重开、把旧事件再演一次。"
        "资源名相似或含‘本场’‘第一枚’等相对称呼时，先核对是否属于同一场次、批次或时段；"
        "结合来源章节及正文明确的获取过程判定，不得把旧场次的耗尽或锁定套到已获证据支持的新场次。"
        "不得仅因章号较新就认定为新资源；新额度必须有已验证正史或本章连续原文支持。"
        "同一份旧资源的无依据恢复仍必须拒绝，单纯改名、换章或计划获取都不算取得新资源。"
        "还须检查本章明确且影响后续正史的人物现实地点变化、不可逆资源变化或核心事件是否遗漏。"
        "人物地点只核对连续原文已明确且章末仍有效的新现实位置，不因出场或提及地名推断移动。遗漏不是正文矛盾，"
        "在missing中列出类别、连续原句和待提取事实，要求补提取，不能通过删条目逃避记账。"
        "characters.location_state必须区分SET、UNKNOWN、NO_CHANGE：SET写入有据章末地点；"
        "旧地点已失效但最终位置没有明确写出时必须用UNKNOWN清除陈旧地点；"
        "只有NO_CHANGE配空location才会继承前章旧地点，须核对该继承是否与本章明确现实位置冲突。"
        "按同一人物的全部characters条目聚合判断最终地点；NO_CHANGE只是该行不修改地点，"
        "同人物另有SET或UNKNOWN时，不得把知识/能力稀疏行的NO_CHANGE误判为重新继承旧地点。"
        "地点不要求专有场馆名：球场内、车厢内等可按原文最小记录，"
        "不能补造城市、场馆名或未经描写的移动路线；主体与位置有连续原文支持而未记时列missing，"
        "不能因已有能力条目地点留空就忽略单独地点条目的缺失。"
        "逐一核对本章实际出场人物，包括父母、教练等配角，不得只核对主角或delta已有姓名。"
        "配角地点有本章明确证据时也须单列，不能因其无新知识或能力变化而漏记；"
        "称谓须结合已验证关系消歧，不得猜认身份。"
        "已离开的瞬时站位不作章末地点；‘站在边线外’、‘场地另一端收球后转身追球’、"
        "‘走到电脑旁’等过程动作不能仅因是人物最后一次出场就写成章末SET。"
        "人物仍在同一场景时可记录有连续原文支持的较粗场景，"
        "不能通过NO_CHANGE继承已失效的旧地点，也不能猜测移动终点。"
        "影响后续行动的新通知、报名材料等实物的明确领取或收存应在resources；"
        "角色本章明确新得知的关键日期、出发安排或行动限制应在characters.knowledge_add，"
        "不能只用events代替；不要求登记全部闲聊、重复已知事实或旁白信息，不得补造具体时刻。"
        "普通赞同、简短评价或对已执行动作的重复反馈不强制记knowledge_add，不能仅因此列missing；"
        "新的指令、约定、限制或影响后续行动的具体信息仍须记录，不能因为说得简短就省略。"
        "人物评价不能无归因地当成客观事实；若登记评价，只记谁明确表达了什么。"
        + KNOWLEDGE_EVIDENCE_RULE + PROGRESSION_EVIDENCE_RULE +
        "missing与已有claim适用同一证据标准：先自检所要求补入的最小条目能否通过同一连续引文的逐字段审计。"
        "不能直接支持主体、获知内容或现实位置的，不得先要求补入、再以同一已知证据缺口拒绝。"
        "有证据的真正遗漏仍须报告；引文范围不足时扩大为正文中完整的连续范围，不得拼接或补写。"
        "增量路径的修复只要求改提取字段或扩大连续引文，不得为了让错误提取成立而要求正文新增告知。"
        "只有draft路径且正文确与经验证正史矛盾时，才要求改正文；不能把提取无证据等同于正文有错。"
        "同一物品的交接应体现章末最终保管状态，不得用别名留下交出者仍持有的并列现态；"
        "分条记录应有交接证据支持交出者已交出、接收者保管，不把保管转移当作所有权转移。"
        "检查time_anchor是否属于summary所述事件：未来活动日期不等于当前获知时间，"
        "也不能把材料历史生成时间当当前查看时间；引文未给当前事件日期就留空。"
        "预定结束时间不能证明已经完成；倒计时或日程段不能独自证明完成态，"
        "须引用交卷、收卷、终场或签署等实际完成动作，或收窄为引文支持的进行态。"
        "相对日期不得凭同章其他段落扩写：knowledge_add、status和summary遇到那天、明天时，"
        "不能额外补具体年月日；需要时扩大连续引文覆盖日期与动作，不得拼接。"
        "状态上下文中的‘事件冷却’仅是非阻断节奏提醒，不是正史事实或连续性禁令；"
        "不得因为相邻章节使用同类冲突、调查或揭示就拒绝正文。"
        "只报告会污染后续正史的明确问题，不把合理回顾、推测口吻或有正文过程的新变化误判为冲突。"
        "只输出 JSON，不要 Markdown。"
    )
    user_prompt = f"""审计下列状态变化。输出：
{{
  "pass": true,
  "missing": [],
  "reject": [{{
    "path": "characters[0] 或 draft",
    "reason": "证据没有说明该角色知道此事",
    "draft_quote": "必须从本章正文逐字复制的问题原句",
    "state_reference": "冲突的已有状态；仅证据蕴含问题可留空",
    "repair_instruction": "删除该增量中无证据的知识字段，保留其他有据字段；不修改正文"
  }}]
}}

规则：
1. 只有 reject 为空时 pass 才能为 true。
2. 增量问题的 path 必须原样使用输入中的 path；正文中未被增量捕获的问题用 draft。
3. 每个拒绝项必须提供能在本章正文连续找到的 draft_quote；找不到原句不得拒绝。
4. 已有状态记录“未知”不代表矛盾；正文明确写出取得、得知、移动或治疗过程时允许更新。
5. 关键变化漏记时，missing填对象：{{"category":"characters、resources或events", "evidence_quote":"正文连续原句", "reason":"漏记哪项已发生的变化"}}。
   只有reject和missing都为空才可pass=true；资源余额变化须在resources中登记，不能仅用事件摘要代替。
   人物新现实位置须有同时明确主体与地点的连续证据；事件摘要不能替代characters.location。
   计划、回忆、梦境或灰影不更新现实位置；无明确位置变化不要求补地点，禁止凭旧索引或大纲制造遗漏。

阶段词表（标签和正文自然说法必须语义等价，词表本身不证明变化）：
{_progression_context_prompt(progression_context)}

写作前已有的经验证正史：
{state_context[:8000] or '暂无结构化历史状态'}

历史正式原文（已按账本哈希核验，仅作历史核对；不是本章或待审稿）：
{verified_source_context or '暂无可附带的近期正式原文；不得据此推断某事未发生'}

待审计条目：
{json.dumps(claims, ensure_ascii=False, indent=2)}

本章完整草稿：
{chapter_text}
"""
    return system_prompt, user_prompt


def validate_delta_audit(
    payload: dict[str, Any],
    delta: dict[str, Any],
    chapter_text: str = "",
    *,
    verified_draft_rejections: list[dict[str, Any]] | None = None,
) -> list[str]:
    # Never let a caller infer rewrite authority from unvalidated raw rows.
    if verified_draft_rejections is not None:
        verified_draft_rejections.clear()
    if any(isinstance(payload.get(key), list) and len(payload[key]) > 40
           for key in ("missing", "reject")):
        return ["独立证据审计条目超过40条，必须重新返回限额内的完整结果；不授权正文重写"]
    missing_issues = []
    discarded_invalid_feedback = False
    missing = payload.get("missing", [])
    if not isinstance(missing, list):
        missing_issues.append("独立证据审计missing必须是数组")
    else:
        for item in missing[:40]:
            if (not isinstance(item, dict) or item.get("category") not in {"characters", "resources", "events"}
                    or not str(item.get("reason") or "").strip()
                    or not _valid_quote(item.get("evidence_quote", ""), chapter_text)):
                missing_issues.append("独立证据审计遗漏项没有可在正文定位的完整证据")
            else:
                reason = str(item.get("reason") or "")
                quote = str(item.get("evidence_quote") or "")
                target = _missing_knowledge_target(reason) if item["category"] == "characters" else ""
                if target and _knowledge_quote_is_send_only(target, quote):
                    discarded_invalid_feedback = True
                    continue
                if (item["category"] == "characters"
                        and _missing_location_is_transient(reason, quote, chapter_text)):
                    discarded_invalid_feedback = True
                    continue
                missing_issues.append(f"{item['category']}提取遗漏：{item['reason']}；补提取原句：{item['evidence_quote']}")
    allowed_paths = {
        f"{key}[{index}]"
        for key in MAX_ITEMS
        for index, _item in enumerate(delta.get(key, []))
    }
    rejects = payload.get("reject", [])
    if not isinstance(rejects, list):
        return ["独立证据审计的 reject 不是数组"]
    issues = list(missing_issues)
    for item in rejects[:40]:
        if not isinstance(item, dict):
            issues.append("独立证据审计返回了无效拒绝项")
            continue
        path = _compact_text(item.get("path"), 80)
        reason = _compact_text(item.get("reason"), 180) or "证据不足"
        if path not in allowed_paths and path != "draft":
            issues.append(f"独立证据审计返回未知路径 {path or '（空）'}")
        else:
            if path in allowed_paths and _audit_reject_misreads_sparse_location(
                path, reason, delta
            ):
                discarded_invalid_feedback = True
                continue
            draft_quote = _compact_text(item.get("draft_quote"), None)
            if not draft_quote and path in allowed_paths:
                key, raw_index = path[:-1].split("[")
                draft_quote = _compact_text(
                    delta.get(key, [])[int(raw_index)].get("evidence_quote"), None
                )
            if chapter_text and not _valid_quote(draft_quote, chapter_text):
                issues.append(f"{path} 审计拒绝项没有可在正文定位的 draft_quote")
                continue
            state_reference = _compact_text(item.get("state_reference"), 180)
            repair = _compact_text(item.get("repair_instruction"), 180)
            detail = reason
            if state_reference:
                detail += f"；正史依据：{state_reference}"
            if repair:
                detail += f"；修复要求：{repair}"
            issues.append(f"{path} 保存前连续性审计未通过：{detail}")
            if (path == "draft" and chapter_text and _valid_quote(draft_quote, chapter_text)
                    and payload.get("pass") is False and verified_draft_rejections is not None):
                verified_draft_rejections.append(dict(item))
    audit_pass = payload.get("pass") is True
    if not audit_pass and not issues and not discarded_invalid_feedback:
        issues.append("独立证据审计未通过但没有给出可定位原因")
    if audit_pass and issues:
        issues.append("独立证据审计结果自相矛盾")
    return issues


def _items(payload: dict[str, Any], key: str, issues: list[str]) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        issues.append(f"{key} 必须是数组")
        return []
    if len(value) > MAX_ITEMS[key]:
        issues.append(f"{key} 条目过多（{len(value)}>{MAX_ITEMS[key]}）")
    result: list[dict[str, Any]] = []
    for idx, item in enumerate(value[: MAX_ITEMS[key]]):
        if not isinstance(item, dict):
            issues.append(f"{key}[{idx}] 必须是对象")
            continue
        result.append(item)
    return result


def prune_overflow_items(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Remove exact duplicates, preserving unique overflow for validation.

    The historical name is retained for callers. Slicing here used to turn
    already-extracted facts into omissions before the independent auditor saw
    them. The deterministic validator, not this cleanup, enforces MAX_ITEMS.
    """
    sanitized = copy.deepcopy(payload)
    pruned: list[str] = []
    for key in MAX_ITEMS:
        value = sanitized.get(key, [])
        if not isinstance(value, list):
            continue
        unique = []
        seen = set()
        for index, item in enumerate(value):
            identity = json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity in seen:
                pruned.append(f"{key}[{index}]:duplicate")
                continue
            seen.add(identity)
            unique.append(item)
        sanitized[key] = unique
    return sanitized, pruned


def _entity_is_grounded(entity: str, quote: str, chapter_text: str, state: dict[str, Any]) -> bool:
    normalized = _normalized_evidence(entity)
    if not normalized:
        return False
    if normalized in _normalized_evidence(quote):
        return True
    if normalized in _normalized_evidence(chapter_text):
        return True
    return entity in state.get("characters", {})


def validate_delta(
    payload: dict[str, Any],
    chapter_number: int,
    chapter_text: str,
    state: dict[str, Any] | None = None,
    *,
    progression_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return a normalized delta and all deterministic validation issues."""
    state = state or initial_state()
    issues: list[str] = []
    try:
        actual_chapter = int(payload.get("chapter", -1))
    except (TypeError, ValueError):
        actual_chapter = -1
    if actual_chapter != int(chapter_number):
        issues.append(f"chapter 必须为 {chapter_number}，实际为 {payload.get('chapter')}")

    normalized: dict[str, Any] = {"chapter": int(chapter_number)}
    for key in MAX_ITEMS:
        normalized[key] = []

    def grounded_quote(item: dict[str, Any], path: str) -> str:
        # Evidence is not display text: truncating it may drop the supported
        # result or conceal a fabricated suffix. Audit the complete quotation.
        quote = _compact_text(item.get("evidence_quote"), None)
        if not _valid_quote(quote, chapter_text):
            issues.append(f"{path}.evidence_quote 无法在本章正文中找到")
        return quote

    for idx, item in enumerate(_items(payload, "characters", issues)):
        path = f"characters[{idx}]"
        quote = grounded_quote(item, path)
        name = _compact_text(item.get("name"), 40)
        if not name:
            issues.append(f"{path}.name 不能为空")
        elif not _entity_is_grounded(name, quote, chapter_text, state):
            issues.append(f"{path}.name 未在正文或已有正史中出现")
        knowledge = item.get("knowledge_add", [])
        abilities = item.get("abilities_add", [])
        if not isinstance(knowledge, list):
            issues.append(f"{path}.knowledge_add 必须是数组")
            knowledge = []
        if not isinstance(abilities, list):
            issues.append(f"{path}.abilities_add 必须是数组")
            abilities = []
        if knowledge and _knowledge_quote_is_send_only(name, quote):
            issues.append(
                f"{path}.knowledge_add 仅有发送动作证据，未明示{name}收到、读到或获知具体内容"
            )
        location = _compact_text(item.get("location"), 80)
        raw_location_state = item.get("location_state")
        if raw_location_state in (None, ""):
            # Backward compatibility for verified deltas produced before the
            # explicit three-state location contract existed.
            location_state = "SET" if location else "NO_CHANGE"
        else:
            location_state = _compact_text(raw_location_state, 16).upper()
        if location_state not in {"SET", "UNKNOWN", "NO_CHANGE"}:
            issues.append(f"{path}.location_state 非法")
        elif location_state == "SET" and not location:
            issues.append(f"{path}.location_state=SET 时 location 不能为空")
        elif location_state == "UNKNOWN" and location:
            issues.append(f"{path}.location_state=UNKNOWN 时 location 必须为空")
        elif location_state == "NO_CHANGE" and location:
            issues.append(f"{path}.location_state=NO_CHANGE 时 location 必须为空")
        status = _compact_text(item.get("status"), 16).lower()
        if status and status not in {"active", "missing", "dead", "unknown"}:
            issues.append(f"{path}.status 非法")
        normalized["characters"].append(
            {
                "name": name,
                "location": location,
                "location_state": location_state,
                "condition": _compact_text(item.get("condition"), 100),
                "emotion": _compact_text(item.get("emotion"), 100),
                "status": status,
                "knowledge_add": [_compact_text(v, 180) for v in knowledge[:12] if _compact_text(v, 180)],
                "abilities_add": [_compact_text(v, 120) for v in abilities[:8] if _compact_text(v, 120)],
                "evidence_quote": quote,
            }
        )
        progression = item.get("progression")
        if progression not in (None, ""):
            if not isinstance(progression, str) or len(progression.strip()) > 120:
                issues.append(f"{path}.progression 必须是120字符内的阶段标签")
            elif progression.strip():
                progression = progression.strip()
                normalized["characters"][-1]["progression"] = progression
                if progression_context is not None:
                    allowed = progression_context.get("realm_order") or []
                    protagonist = progression_context.get("protagonist") or ""
                    if name != protagonist or progression not in allowed:
                        issues.append(f"{path}.progression 不属于该主角的项目阶段表")

    for idx, item in enumerate(_items(payload, "resources", issues)):
        path = f"resources[{idx}]"
        quote = grounded_quote(item, path)
        owner = _compact_text(item.get("owner"), 40)
        resource = _compact_text(item.get("item"), 80)
        action = _compact_text(item.get("action"), 12).upper()
        if not owner or not resource:
            issues.append(f"{path} 缺少 owner 或 item")
        if action not in {"GAIN", "USE", "LOSE", "SET"}:
            issues.append(f"{path}.action 非法")
        quantity_change = item.get("quantity_change")
        quantity = item.get("quantity")
        if quantity_change is not None and not isinstance(quantity_change, (int, float)):
            issues.append(f"{path}.quantity_change 必须是数字或 null")
            quantity_change = None
        if quantity is not None and not isinstance(quantity, (int, float)):
            issues.append(f"{path}.quantity 必须是数字或 null")
            quantity = None
        if quantity is not None and quantity < 0:
            issues.append(f"{path}.quantity 不能为负数")
        normalized["resources"].append(
            {
                "owner": owner,
                "item": resource,
                "action": action,
                "quantity_change": quantity_change,
                "quantity": quantity,
                "status": _compact_text(item.get("status"), 80),
                "evidence_quote": quote,
            }
        )

    for idx, item in enumerate(_items(payload, "relationships", issues)):
        path = f"relationships[{idx}]"
        quote = grounded_quote(item, path)
        a = _compact_text(item.get("a"), 40)
        b = _compact_text(item.get("b"), 40)
        if not a or not b or a == b:
            issues.append(f"{path} 必须提供两个不同角色")
        for entity in (a, b):
            if entity and not _entity_is_grounded(entity, quote, chapter_text, state):
                issues.append(f"{path} 的角色 {entity} 未在正文或已有正史中出现")
        normalized["relationships"].append(
            {
                "a": a,
                "b": b,
                "type": _compact_text(item.get("type"), 60),
                "status": _compact_text(item.get("status"), 100),
                "evidence_quote": quote,
            }
        )

    for key, prefix in (("hooks", "hook"), ("subplots", "subplot")):
        existing = state.get(key, {})
        for idx, item in enumerate(_items(payload, key, issues)):
            path = f"{key}[{idx}]"
            quote = grounded_quote(item, path)
            label = _compact_text(item.get("label"), 100)
            action = _compact_text(item.get("action"), 12).upper()
            entity_id = _safe_id(item.get("id"), prefix, label)
            if not label:
                issues.append(f"{path}.label 不能为空")
            if action not in {"OPEN", "ADVANCE", "RESOLVE"}:
                issues.append(f"{path}.action 非法")
            old = existing.get(entity_id)
            if action in {"ADVANCE", "RESOLVE"} and not old:
                issues.append(f"{path} 引用了不存在的 {entity_id}；已有条目必须使用上下文中的 ID")
            if action == "OPEN" and old and old.get("status") == "RESOLVED":
                issues.append(f"{path} 试图重新打开已解决条目 {entity_id}")
            due_by = item.get("due_by")
            if due_by is not None:
                try:
                    due_by = int(due_by)
                    if due_by < chapter_number:
                        issues.append(f"{path}.due_by 不能早于当前章")
                except (TypeError, ValueError):
                    issues.append(f"{path}.due_by 必须是整数或 null")
                    due_by = None
            normalized[key].append(
                {
                    "id": entity_id,
                    "label": label,
                    "action": action,
                    "due_by": due_by,
                    "evidence_quote": quote,
                }
            )

    for idx, item in enumerate(_items(payload, "events", issues)):
        path = f"events[{idx}]"
        quote = grounded_quote(item, path)
        event_type = _compact_text(item.get("event_type"), 24).lower()
        summary = _compact_text(item.get("summary"), 180)
        if event_type not in ALLOWED_EVENT_TYPES:
            issues.append(f"{path}.event_type 非法")
        if not summary:
            issues.append(f"{path}.summary 不能为空")
        compact_summary = re.sub(r"\s+", "", summary).lower()
        if (
            re.fullmatch(r"chapter\d+evidenceevent\d+", compact_summary)
            or re.fullmatch(r"[?？!！.。…]+", compact_summary)
            or "placeholder" in compact_summary
            or "待补" in compact_summary
        ):
            issues.append(f"{path}.summary 是占位文本，不能进入证据正史")
        event = {
            "event_type": event_type,
            "summary": summary,
            "evidence_quote": quote,
        }
        normalized_quote = _normalized_evidence(quote)
        for field, limit in (
            ("time_anchor", 80),
            ("duration", 80),
            ("location", 100),
        ):
            # Legacy deltas remain replayable. Newly extracted deltas include
            # all three keys (possibly empty) because the prompt requires them.
            if field not in item:
                continue
            value = _compact_text(item.get(field), limit)
            if value and _normalized_evidence(value) not in normalized_quote:
                # Time and duration are optional indexing metadata.  Clearing
                # an ungrounded value is lossless for the evidenced event and
                # prevents a stubborn extractor from discarding the chapter.
                # Location remains fail-closed because it affects continuity.
                if field in {"time_anchor", "duration"}:
                    value = ""
                else:
                    issues.append(
                        f"{path}.{field} 无法在同一 evidence_quote 中逐字定位"
                    )
            event[field] = value
        normalized["events"].append(event)

    if not normalized["events"]:
        issues.append("events 至少要登记一条本章已发生的核心事件，不能用空增量跳过正史记忆")

    return normalized, issues


def prune_unlocatable_evidence_items(
    payload: dict[str, Any], issues: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Remove only rows whose quoted evidence cannot be found in the chapter.

    Any other validation issue disables this fallback.  This keeps the ledger
    fail-closed for schema, continuity, and entailment problems while allowing
    an otherwise grounded extraction to survive one hallucinated optional row.
    """
    if not isinstance(payload, dict) or not issues:
        return payload, []
    targets: dict[str, set[int]] = {}
    pattern = re.compile(
        r"^(characters|resources|relationships|hooks|subplots|events)"
        r"\[(\d+)\]\.evidence_quote 无法在本章正文中找到$"
    )
    for issue in issues:
        match = pattern.fullmatch(str(issue or "").strip())
        if not match:
            return payload, []
        targets.setdefault(match.group(1), set()).add(int(match.group(2)))

    sanitized = copy.deepcopy(payload)
    removed: list[str] = []
    for key, indexes in targets.items():
        rows = sanitized.get(key)
        if not isinstance(rows, list):
            return payload, []
        for index in sorted(indexes, reverse=True):
            if index < 0 or index >= len(rows):
                return payload, []
            rows.pop(index)
            removed.append(f"{key}[{index}]")
    return sanitized, sorted(removed)


def prune_audit_rejected_items(
    delta: dict[str, Any], audit_payload: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Drop only structured rows explicitly rejected by the independent audit.

    A ``draft`` rejection, an unknown path, or removal of every event disables
    this fallback. It is intended for the final extraction attempt: preserving
    fewer grounded facts is safer than committing an overclaimed row, while a
    prose/canon rejection must continue to block the chapter.
    """
    if not isinstance(delta, dict) or not isinstance(audit_payload, dict):
        return delta, []
    rejects = audit_payload.get("reject", [])
    if not isinstance(rejects, list) or not rejects:
        return delta, []
    pattern = re.compile(
        r"^(characters|resources|relationships|hooks|subplots|events)\[(\d+)\]$"
    )
    targets: dict[str, set[int]] = {}
    for item in rejects:
        if not isinstance(item, dict):
            return delta, []
        path = str(item.get("path") or "").strip()
        if path == "draft":
            return delta, []
        match = pattern.fullmatch(path)
        if not match:
            return delta, []
        targets.setdefault(match.group(1), set()).add(int(match.group(2)))

    sanitized = copy.deepcopy(delta)
    removed: list[str] = []
    for key, indexes in targets.items():
        rows = sanitized.get(key)
        if not isinstance(rows, list):
            return delta, []
        for index in sorted(indexes, reverse=True):
            if index < 0 or index >= len(rows):
                return delta, []
            rows.pop(index)
            removed.append(f"{key}[{index}]")
    if not sanitized.get("events"):
        return delta, []
    return sanitized, sorted(removed)


def _append_unique(values: list[str], additions: Iterable[str], limit: int = 80) -> list[str]:
    result = list(values)
    seen = {_normalized_evidence(v) for v in result}
    for addition in additions:
        key = _normalized_evidence(addition)
        if key and key not in seen:
            result.append(addition)
            seen.add(key)
    return result[-limit:]


def _add_evidence(record: dict[str, Any], chapter: int, quote: str) -> None:
    evidence = record.setdefault("evidence", [])
    evidence.append({"chapter": chapter, "quote": quote})
    record["evidence"] = evidence[-20:]


def _apply_delta_to_state(
    state: dict[str, Any], delta: dict[str, Any], chapter_hash: str
) -> dict[str, Any]:
    result = copy.deepcopy(state)
    chapter = int(delta["chapter"])
    applied = result.setdefault("applied_chapters", {})
    old_hash = applied.get(str(chapter))
    if old_hash:
        if old_hash != chapter_hash:
            raise StateLedgerError(f"第{chapter}章正文哈希已变化，拒绝覆盖既有正史")
        return result

    for item in delta.get("characters", []):
        name = item["name"]
        record = result.setdefault("characters", {}).setdefault(
            name,
            {
                "location": "",
                "condition": "",
                "emotion": "",
                "status": "unknown",
                "knowledge": [],
                "abilities": [],
                "last_seen_chapter": 0,
                "evidence": [],
            },
        )
        location_state = str(item.get("location_state") or "").upper()
        if not location_state:
            location_state = "SET" if item.get("location") else "NO_CHANGE"
        if location_state == "SET" and item.get("location"):
            record["location"] = item["location"]
        elif location_state == "UNKNOWN":
            record["location"] = ""
        for field in ("condition", "emotion", "status", "progression"):
            if item.get(field):
                record[field] = item[field]
        record["knowledge"] = _append_unique(record.get("knowledge", []), item.get("knowledge_add", []))
        record["abilities"] = _append_unique(record.get("abilities", []), item.get("abilities_add", []), 40)
        record["last_seen_chapter"] = chapter
        _add_evidence(record, chapter, item["evidence_quote"])

    for item in delta.get("resources", []):
        key = _stable_id("resource", item["owner"], item["item"])
        record = result.setdefault("resources", {}).setdefault(
            key,
            {
                "owner": item["owner"],
                "item": item["item"],
                "quantity": None,
                "status": "unknown",
                "last_changed_chapter": 0,
                "evidence": [],
            },
        )
        action = item["action"]
        current = record.get("quantity")
        # Extracted events may omit intermediate uses. A grounded post-event
        # balance is authoritative even when the local change is also present.
        if item.get("quantity") is not None:
            if item["quantity"] < 0:
                raise StateLedgerError(f"资源 {item['owner']}::{item['item']} 数量不能为负数")
            record["quantity"] = item["quantity"]
        elif action in {"GAIN", "USE", "LOSE"} and item.get("quantity_change") is not None:
            change = abs(item["quantity_change"])
            if action in {"USE", "LOSE"}:
                change = -change
            if current is not None:
                next_value = current + change
                if next_value < 0:
                    raise StateLedgerError(f"资源 {item['owner']}::{item['item']} 数量将变为负数")
                record["quantity"] = next_value
        if item.get("status"):
            record["status"] = item["status"]
        elif action == "LOSE":
            record["status"] = "lost"
        elif action == "USE" and record.get("quantity") == 0:
            record["status"] = "consumed"
        elif action == "GAIN":
            record["status"] = "available"
        record["last_changed_chapter"] = chapter
        _add_evidence(record, chapter, item["evidence_quote"])

    for item in delta.get("relationships", []):
        a, b = sorted((item["a"], item["b"]))
        key = _stable_id("relation", a, b)
        record = result.setdefault("relationships", {}).setdefault(
            key, {"a": a, "b": b, "type": "", "status": "", "evidence": []}
        )
        if item.get("type"):
            record["type"] = item["type"]
        if item.get("status"):
            record["status"] = item["status"]
        record["last_changed_chapter"] = chapter
        _add_evidence(record, chapter, item["evidence_quote"])

    for key in ("hooks", "subplots"):
        for item in delta.get(key, []):
            record = result.setdefault(key, {}).get(item["id"])
            if record is None:
                record = {
                    "id": item["id"],
                    "label": item["label"],
                    "status": "OPEN",
                    "opened_chapter": chapter,
                    "last_advanced_chapter": chapter,
                    "due_by": item.get("due_by"),
                    "evidence": [],
                }
                result[key][item["id"]] = record
            action = item["action"]
            if action == "ADVANCE":
                record["status"] = "ADVANCING"
                record["last_advanced_chapter"] = chapter
            elif action == "RESOLVE":
                record["status"] = "RESOLVED"
                record["resolved_chapter"] = chapter
                record["last_advanced_chapter"] = chapter
            elif action == "OPEN":
                record["status"] = "OPEN"
            if item.get("due_by") is not None:
                record["due_by"] = item["due_by"]
            _add_evidence(record, chapter, item["evidence_quote"])

    for event_order, item in enumerate(delta.get("events", []), start=1):
        event = {
            "chapter": chapter,
            "event_order": event_order,
            "event_type": item["event_type"],
            "summary": item["summary"],
            "time_anchor": item.get("time_anchor", ""),
            "duration": item.get("duration", ""),
            "location": item.get("location", ""),
            "evidence_quote": item["evidence_quote"],
        }
        result.setdefault("timeline", []).append(event)
        result.setdefault("event_history", []).append(dict(event))

    result["timeline"] = result.get("timeline", [])[-2000:]
    result["event_history"] = result.get("event_history", [])[-400:]
    applied[str(chapter)] = chapter_hash
    result["current_chapter"] = max(int(result.get("current_chapter", 0)), chapter)
    result["updated_at"] = _now_iso()
    result["schema_version"] = SCHEMA_VERSION
    return result


def _delta_dir(plot_dir: Path) -> Path:
    return plot_dir / "state_deltas"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _wrapper_digest(
    chapter: int,
    chapter_hash: str,
    delta: dict[str, Any],
    previous_delta_sha256: str,
) -> str:
    return _sha256_text(_canonical_json({
        "chapter": int(chapter),
        "chapter_sha256": str(chapter_hash),
        "delta": delta,
        "previous_delta_sha256": str(previous_delta_sha256),
    }))


def _numbered_delta_paths(plot_dir: Path) -> list[Path]:
    rows = []
    for path in _delta_dir(plot_dir).glob("chapter_*.json"):
        match = re.fullmatch(r"chapter_(\d+)\.json", path.name)
        if match:
            rows.append((int(match.group(1)), path))
    return [path for _number, path in sorted(rows, key=lambda row: row[0])]


def _load_verified_delta_wrappers(plot_dir: Path) -> list[dict[str, Any]]:
    """Verify continuous numbering and the v2 cryptographic hash chain."""
    wrappers = []
    previous_digest = DELTA_CHAIN_GENESIS
    for expected, path in enumerate(_numbered_delta_paths(plot_dir), start=1):
        filename_chapter = int(re.search(r"(\d+)", path.stem).group(1))
        if filename_chapter != expected:
            raise StateLedgerError(
                f"状态增量断号：应为第{expected}章，实际遇到第{filename_chapter}章"
            )
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            delta = wrapper["delta"]
            chapter_hash = str(wrapper["chapter_sha256"])
        except Exception as exc:
            raise StateLedgerError(f"状态增量 {path.name} 无法读取：{exc}") from exc
        if int(wrapper.get("chapter", -1)) != expected or int(delta.get("chapter", -1)) != expected:
            raise StateLedgerError(f"状态增量 {path.name} 的章节号不一致")
        calculated = _wrapper_digest(expected, chapter_hash, delta, previous_digest)
        if int(wrapper.get("schema_version", 1) or 1) >= DELTA_SCHEMA_VERSION:
            if str(wrapper.get("previous_delta_sha256") or "") != previous_digest:
                raise StateLedgerError(f"第{expected}章状态增量前向哈希链断裂")
            if str(wrapper.get("delta_sha256") or "") != calculated:
                raise StateLedgerError(f"第{expected}章状态增量内容哈希不一致")
        wrapper["_verified_delta_sha256"] = calculated
        wrappers.append(wrapper)
        previous_digest = calculated
    return wrappers


def _state_path(plot_dir: Path) -> Path:
    return plot_dir / "story_state.json"


def _state_hash_path(plot_dir: Path) -> Path:
    return plot_dir / "story_state.sha256"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _write_materialized_state(plot_dir: Path, state: dict[str, Any]) -> None:
    state_file = _state_path(plot_dir)
    _atomic_write_json(state_file, state)
    _atomic_write_text(_state_hash_path(plot_dir), _sha256_file(state_file) + "\n")


def rebuild_state_from_deltas(plot_dir: str | Path) -> dict[str, Any]:
    plot_path = Path(plot_dir)
    state = initial_state()
    wrappers = _load_verified_delta_wrappers(plot_path)
    for wrapper in wrappers:
        try:
            delta = wrapper["delta"]
            chapter_hash = str(wrapper["chapter_sha256"])
            state = _apply_delta_to_state(state, delta, chapter_hash)
        except Exception as exc:
            raise StateLedgerError(
                f"无法重放第{wrapper.get('chapter')}章状态增量：{exc}"
            ) from exc
    state["delta_count"] = len(wrappers)
    state["ledger_tip_sha256"] = (
        wrappers[-1]["_verified_delta_sha256"] if wrappers else DELTA_CHAIN_GENESIS
    )
    return state


def build_state_before_chapter(plot_dir: str | Path, chapter_number: int) -> dict[str, Any]:
    """Materialize canonical state strictly before ``chapter_number``."""
    plot_path = Path(plot_dir)
    state = initial_state()
    applied = []
    for wrapper in _load_verified_delta_wrappers(plot_path):
        delta = wrapper["delta"]
        chapter = int(delta.get("chapter", 0))
        if chapter >= int(chapter_number):
            continue
        state = _apply_delta_to_state(state, delta, str(wrapper["chapter_sha256"]))
        applied.append(wrapper)
    state["delta_count"] = len(applied)
    state["ledger_tip_sha256"] = (
        applied[-1]["_verified_delta_sha256"] if applied else DELTA_CHAIN_GENESIS
    )
    return state


def verify_materialized_state(plot_dir: str | Path) -> None:
    """Compare persisted semantic data to verified delta replay without writing."""
    plot_path = Path(plot_dir)
    state = json.loads(_state_path(plot_path).read_text(encoding="utf-8"))
    rebuilt = rebuild_state_from_deltas(plot_path)
    if not isinstance(state, dict):
        raise StateLedgerError("物化状态必须为对象")
    different = sorted(
        key for key in set(state) | set(rebuilt)
        if key != "updated_at" and state.get(key) != rebuilt.get(key)
    )
    if different:
        raise StateLedgerError("物化状态与已验证增量重放不一致：" + "、".join(different))


def backfill_legacy_character_knowledge(
    plot_dir: str | Path,
    chapter_number: int,
    character_name: str,
    knowledge: str,
    evidence_quote: str,
    chapter_text: str,
) -> dict[str, Any]:
    """Repair a legacy delta that omitted knowledge explicitly shown in formal prose.

    Old projects may have coarse state deltas created before character knowledge was
    extracted.  This repair is deliberately narrow: the formal chapter hash must
    still match, the evidence must be a continuous quote from that chapter, and the
    amended delta must pass the normal validator.  The forward hash chain is then
    resealed without changing any chapter-body hash.
    """
    plot_path = Path(plot_dir)
    chapter_number = int(chapter_number)
    character_name = _compact_text(character_name, 120)
    knowledge = _compact_text(knowledge, 180)
    evidence_quote = str(evidence_quote or "").strip()
    if chapter_number <= 0 or not character_name or not knowledge or not evidence_quote:
        raise StateLedgerError("旧账本知识回填参数不完整")

    wrappers = _load_verified_delta_wrappers(plot_path)
    if chapter_number > len(wrappers):
        raise StateLedgerError(f"第{chapter_number}章状态增量不存在")
    target = wrappers[chapter_number - 1]
    if str(target.get("chapter_sha256") or "") != chapter_sha256(chapter_text):
        raise StateLedgerError(f"第{chapter_number}章正式正文哈希与状态增量不一致")
    if not _valid_quote(evidence_quote, chapter_text):
        raise StateLedgerError("旧账本知识回填证据不是正式正文中的连续原句")

    for item in (target.get("delta") or {}).get("characters", []):
        if item.get("name") == character_name and knowledge in (item.get("knowledge_add") or []):
            return rebuild_state_from_deltas(plot_path)

    amended_delta = copy.deepcopy(target["delta"])
    amended_delta.setdefault("characters", []).append({
        "name": character_name,
        "location": "",
        "condition": "",
        "emotion": "",
        "status": "",
        "knowledge_add": [knowledge],
        "abilities_add": [],
        "evidence_quote": evidence_quote,
    })
    prior_state = build_state_before_chapter(plot_path, chapter_number)
    normalized, issues = validate_delta(
        amended_delta, chapter_number, chapter_text, prior_state
    )
    if issues:
        raise StateLedgerError("旧账本知识回填未通过证据校验：" + "；".join(issues[:8]))

    previous_digest = DELTA_CHAIN_GENESIS
    for index, original in enumerate(wrappers, start=1):
        wrapper = copy.deepcopy(original)
        wrapper.pop("_verified_delta_sha256", None)
        if index == chapter_number:
            wrapper["delta"] = normalized
            wrapper["validated_at"] = _now_iso()
        digest = _wrapper_digest(
            index,
            str(wrapper["chapter_sha256"]),
            wrapper["delta"],
            previous_digest,
        )
        if index >= chapter_number:
            wrapper["schema_version"] = DELTA_SCHEMA_VERSION
            wrapper["previous_delta_sha256"] = previous_digest
            wrapper["delta_sha256"] = digest
            _atomic_write_json(
                _delta_dir(plot_path) / f"chapter_{index:04d}.json", wrapper
            )
        previous_digest = digest

    state = rebuild_state_from_deltas(plot_path)
    _write_materialized_state(plot_path, state)
    return state


def supersede_last_chapter_delta(
    plot_dir: str | Path,
    chapter_number: int,
    expected_chapter_sha256: str = "",
) -> str:
    """Archive the latest delta so an explicitly amended last chapter can replace it."""
    plot_path = Path(plot_dir)
    chapter_number = int(chapter_number)
    source = _delta_dir(plot_path) / f"chapter_{chapter_number:04d}.json"
    archive_dir = _delta_dir(plot_path) / "superseded"
    if not source.exists():
        matching_archives = sorted(
            archive_dir.glob(f"chapter_{chapter_number:04d}_*.json")
        ) if archive_dir.exists() else []
        for archive in reversed(matching_archives):
            try:
                wrapper = json.loads(archive.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            archived_hash = str(wrapper.get("chapter_sha256") or "")
            if not expected_chapter_sha256 or archived_hash == expected_chapter_sha256:
                rebuilt = rebuild_state_from_deltas(plot_path)
                _write_materialized_state(plot_path, rebuilt)
                return str(archive)
        raise StateLedgerError(f"第{chapter_number}章状态增量不存在，且找不到可核验的归档")

    state = load_state(plot_path)
    if int(state.get("current_chapter", 0)) != chapter_number:
        raise StateLedgerError("只能修改正史中的最后一章；存在后续章节时禁止改写历史状态")
    later = [
        path for path in _numbered_delta_paths(plot_path)
        if int(re.search(r"(\d+)", path.stem).group(1)) > chapter_number
    ]
    if later:
        raise StateLedgerError("最后一章之后仍有状态增量，拒绝局部改写造成后续正史失效")
    if expected_chapter_sha256:
        wrapper = json.loads(source.read_text(encoding="utf-8"))
        if str(wrapper.get("chapter_sha256") or "") != expected_chapter_sha256:
            raise StateLedgerError("待替换章节正文哈希与状态增量不一致")
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / (
        f"chapter_{chapter_number:04d}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    )
    os.replace(source, archive)
    rebuilt = rebuild_state_from_deltas(plot_path)
    _write_materialized_state(plot_path, rebuilt)
    return str(archive)


def load_state(plot_dir: str | Path) -> dict[str, Any]:
    plot_path = Path(plot_dir)
    state_file = _state_path(plot_path)
    state: dict[str, Any]
    state_valid = True
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("schema mismatch")
        hash_file = _state_hash_path(plot_path)
        if hash_file.exists():
            expected_hash = hash_file.read_text(encoding="utf-8").strip()
            if expected_hash != _sha256_file(state_file):
                raise ValueError("materialized state hash mismatch")
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        state = initial_state()
        state_valid = False

    wrappers = _load_verified_delta_wrappers(plot_path)
    latest_delta = int(wrappers[-1]["chapter"]) if wrappers else 0
    state_chapter = int(state.get("current_chapter", 0) or 0)
    if state_valid and state_chapter > latest_delta:
        raise StateLedgerError(
            f"物化状态已到第{state_chapter}章，但增量链只到第{latest_delta}章；"
            "可能有状态增量被删除，拒绝静默降级"
        )
    expected_applied = {
        str(wrapper["chapter"]): str(wrapper["chapter_sha256"])
        for wrapper in wrappers
    }
    expected_tip = (
        wrappers[-1]["_verified_delta_sha256"] if wrappers else DELTA_CHAIN_GENESIS
    )
    needs_rebuild = (
        not state_valid
        or state_chapter < latest_delta
        or state.get("applied_chapters", {}) != expected_applied
        or int(state.get("delta_count", -1) or 0) != len(wrappers)
        or str(state.get("ledger_tip_sha256") or "") != expected_tip
    )
    if needs_rebuild:
        state = rebuild_state_from_deltas(plot_path)
        if wrappers or state_file.exists():
            _write_materialized_state(plot_path, state)
    return state


def commit_validated_delta(
    plot_dir: str | Path,
    delta: dict[str, Any],
    chapter_text: str,
) -> dict[str, Any]:
    plot_path = Path(plot_dir)
    chapter = int(delta["chapter"])
    chapter_hash = chapter_sha256(chapter_text)
    delta_path = _delta_dir(plot_path) / f"chapter_{chapter:04d}.json"
    state_before = load_state(plot_path)
    if delta_path.exists():
        existing = json.loads(delta_path.read_text(encoding="utf-8"))
        if existing.get("chapter_sha256") != chapter_hash:
            raise StateLedgerError(f"第{chapter}章已有不同正文对应的状态增量，拒绝覆盖")
        if existing.get("delta") != delta:
            raise StateLedgerError(f"第{chapter}章已存在不同的证据增量，拒绝静默替换")
        return state_before
    else:
        normalized_delta, validation_issues = validate_delta(
            delta, chapter, chapter_text, state_before
        )
        if validation_issues:
            raise StateLedgerError(
                f"第{chapter}章状态增量提交前复核失败："
                + "；".join(validation_issues[:12])
            )
        if normalized_delta != delta:
            raise StateLedgerError(
                f"第{chapter}章状态增量不是规范化验证结果，拒绝提交"
            )
        wrappers = _load_verified_delta_wrappers(plot_path)
        expected_chapter = len(wrappers) + 1
        if chapter != expected_chapter:
            raise StateLedgerError(
                f"状态增量必须连续提交：当前应写第{expected_chapter}章，收到第{chapter}章"
            )
        previous_digest = (
            wrappers[-1]["_verified_delta_sha256"] if wrappers else DELTA_CHAIN_GENESIS
        )
        delta_digest = _wrapper_digest(
            chapter, chapter_hash, delta, previous_digest
        )
        wrapper = {
            "schema_version": DELTA_SCHEMA_VERSION,
            "chapter": chapter,
            "chapter_sha256": chapter_hash,
            "previous_delta_sha256": previous_digest,
            "delta_sha256": delta_digest,
            "validated_at": _now_iso(),
            "delta": delta,
        }
        _atomic_write_json(delta_path, wrapper)

    updated = _apply_delta_to_state(state_before, delta, chapter_hash)
    wrappers = _load_verified_delta_wrappers(plot_path)
    updated["delta_count"] = len(wrappers)
    updated["ledger_tip_sha256"] = (
        wrappers[-1]["_verified_delta_sha256"] if wrappers else DELTA_CHAIN_GENESIS
    )
    _write_materialized_state(plot_path, updated)
    return updated


def _recent_event_cooldowns(
    state: dict[str, Any], chapter_number: int, cooldowns: dict[str, int]
) -> list[dict[str, Any]]:
    latest_by_type: dict[str, dict[str, Any]] = {}
    for event in reversed(state.get("event_history", [])):
        event_type = event.get("event_type")
        if event_type not in latest_by_type:
            latest_by_type[event_type] = event
    result = []
    for event_type, event in latest_by_type.items():
        cooldown = int(cooldowns.get(event_type, 0) or 0)
        distance = chapter_number - int(event.get("chapter", 0))
        if cooldown and distance <= cooldown:
            result.append(
                {
                    "event_type": event_type,
                    "last_chapter": event.get("chapter"),
                    "remaining": cooldown - distance + 1,
                    "summary": event.get("summary", ""),
                }
            )
    return result


def render_context(
    state: dict[str, Any],
    chapter_number: int,
    outline: str = "",
    previous_tail: str = "",
    max_chars: int = 5200,
    stale_warn_chapters: int = 8,
    cooldowns: dict[str, int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Compile a bounded, explicit truth context plus deterministic guardrails."""
    cooldowns = {**DEFAULT_EVENT_COOLDOWNS, **(cooldowns or {})}
    query = _normalized_evidence(f"{outline}\n{previous_tail}")
    characters = state.get("characters", {})
    ranked = sorted(
        characters.items(),
        key=lambda pair: (
            0 if _normalized_evidence(pair[0]) in query else 1,
            -int(pair[1].get("last_seen_chapter", 0)),
            pair[0],
        ),
    )[:12]
    selected_names = [name for name, _ in ranked]

    active_hooks = [v for v in state.get("hooks", {}).values() if v.get("status") != "RESOLVED"]
    active_subplots = [v for v in state.get("subplots", {}).values() if v.get("status") != "RESOLVED"]
    def open_item_priority(item: dict[str, Any]) -> tuple[Any, ...]:
        # Old undated items must not permanently crowd out the current arc.
        # Keep explicitly named items and due obligations, then prefer recent
        # progress. Selection changes visibility, never the item's truth status.
        label = _normalized_evidence(item.get("label", ""))
        mentioned = bool(label and label in query)
        due = int(item.get("due_by") or 0)
        is_due = 0 < due <= chapter_number
        opened = int(item.get("opened_chapter") or 0)
        advanced = int(item.get("last_advanced_chapter") or opened)
        return (
            0 if mentioned else 1,
            0 if is_due else 1,
            due if is_due else -advanced,
            -opened,
            str(item.get("id", "")),
        )

    active_hooks.sort(key=open_item_priority)
    active_subplots.sort(key=open_item_priority)
    stale = []
    for item in active_subplots:
        age = chapter_number - int(item.get("last_advanced_chapter", item.get("opened_chapter", 0)))
        if age >= stale_warn_chapters:
            stale.append({"id": item.get("id"), "label": item.get("label"), "age": age})
    cooling = _recent_event_cooldowns(state, chapter_number, cooldowns)

    lines = [
        "【经证据校验的长期正史账本】",
        f"账本已登记到第{int(state.get('current_chapter', 0))}章。摘要不穷尽历史事实；未收录不等于未发生，需查核验后的正式原文，不得自行补全。",
        "【角色当前状态与知情边界】",
    ]
    if not ranked:
        lines.append("- 尚无已验证角色状态；只能使用本章大纲和正文承接中明确提供的信息。")
    for name, record in ranked:
        parts = [f"地点={record.get('location') or '未知'}", f"身体={record.get('condition') or '未知'}"]
        if record.get("status"):
            parts.append(f"状态={record.get('status')}")
        if record.get("progression"):
            parts.append(f"阶段={record['progression']}")
        lines.append(f"- {name}：" + "；".join(parts))
        knowledge = record.get("knowledge", [])[-8:]
        lines.append("  已知：" + ("；".join(knowledge) if knowledge else "没有可验证的秘密知识"))
        abilities = record.get("abilities", [])[-6:]
        if abilities:
            lines.append("  已验证能力：" + "；".join(abilities))

    lines.append("【关系与资源】")
    relation_count = 0
    relation_rows = sorted(
        state.get("relationships", {}).values(),
        key=lambda record: (
            0 if record.get("a") in selected_names or record.get("b") in selected_names else 1,
            -int(record.get("last_changed_chapter", 0)),
        ),
    )
    for record in relation_rows:
        if record.get("a") in selected_names or record.get("b") in selected_names:
            lines.append(
                f"- {record.get('a')} ↔ {record.get('b')}：{record.get('type') or '关系未知'}；{record.get('status') or '状态未知'}"
            )
            relation_count += 1
            if relation_count >= 10:
                break
    resource_count = 0
    resource_rows = sorted(
        state.get("resources", {}).values(),
        key=lambda record: (
            0 if _normalized_evidence(record.get("item")) in query else 1,
            0 if record.get("owner") in selected_names else 1,
            -int(record.get("last_changed_chapter", 0)),
        ),
    )
    for record in resource_rows:
        if record.get("owner") in selected_names or _normalized_evidence(record.get("item")) in query:
            quantity = record.get("quantity")
            quantity_text = "数量未知" if quantity is None else f"数量={quantity:g}" if isinstance(quantity, float) else f"数量={quantity}"
            lines.append(
                f"- {record.get('owner')}持有「{record.get('item')}」：{quantity_text}；{record.get('status') or '状态未知'}"
                f"；最后更新第{int(record.get('last_changed_chapter', 0))}章"
            )
            resource_count += 1
            if resource_count >= 12:
                break
    if relation_count == 0 and resource_count == 0:
        lines.append("- 无与本章相关且已验证的关系/资源记录。")

    lines.append("【未结伏笔与支线】")
    hook_rows = []
    for item in active_hooks[:10]:
        row = (
            f"- 伏笔ID={item.get('id')}｜{item.get('label')}｜{item.get('status')}｜上次推进第{item.get('last_advanced_chapter')}章"
        )
        hook_rows.append((item.get("id"), row))
        lines.append(row)
    subplot_rows = []
    for item in active_subplots[:8]:
        row = (
            f"- 支线ID={item.get('id')}｜{item.get('label')}｜{item.get('status')}｜上次推进第{item.get('last_advanced_chapter')}章"
        )
        subplot_rows.append((item.get("id"), row))
        lines.append(row)
    if not active_hooks and not active_subplots:
        lines.append("- 暂无经过验证的未结伏笔或支线。")

    lines.append("【本章硬性禁区】")
    lines.append("- 角色不得知道其“已知”列表之外的隐藏事实，除非核验后的历史正式原文已明确其亲历或获知，或本章通过可见行动/对话新获得信息；仅旁白披露不能充当角色知识。")
    lines.append("- 不得凭空恢复已消耗、遗失或数量不足的资源；不得把计划、传闻、梦境写成已发生正史。")
    resolved = [v.get("label") for v in state.get("hooks", {}).values() if v.get("status") == "RESOLVED"][-8:]
    if resolved:
        lines.append("- 已解决伏笔不得原样重开：" + "；".join(resolved))
    resolved_subplots = [
        v.get("label") for v in state.get("subplots", {}).values() if v.get("status") == "RESOLVED"
    ][-8:]
    if resolved_subplots:
        lines.append("- 已收束支线不得当作新支线重开：" + "；".join(resolved_subplots))
    for item in stale[:6]:
        lines.append(f"- 停滞支线：{item['label']}已{item['age']}章未推进，本章若相关应有实质变化，不能只重复提醒。")

    if cooling:
        lines.append("【节奏提醒（非正史硬约束，不得据此拒绝正文）】")
        for item in cooling:
            lines.append(
                f"- 事件冷却：{EVENT_TYPE_LABELS.get(item['event_type'], item['event_type'])}刚在第{item['last_chapter']}章发生；若本章细纲仍要求同类事件，以细纲为准，只需避免机械重复。"
            )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 22].rstrip() + "\n【上下文已按预算截断】"
    visible_lines = set(text.splitlines())
    manifest = {
        "selected_characters": selected_names,
        "active_hook_ids": [item_id for item_id, row in hook_rows if row in visible_lines],
        "active_subplot_ids": [item_id for item_id, row in subplot_rows if row in visible_lines],
        "stale_subplots": stale,
        "cooldown_warnings": cooling,
        "context_chars": len(text),
    }
    return text, manifest


def compile_chapter_context(
    plot_dir: str | Path,
    chapter_number: int,
    outline: str,
    previous_tail: str,
    source_paths: dict[str, str | Path] | None = None,
    max_chars: int = 5200,
    stale_warn_chapters: int = 8,
    cooldowns: dict[str, int] | None = None,
    write_trace: bool = True,
    temporal_context: str = "",
    temporal_retrieval: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plot_path = Path(plot_dir)
    state = load_state(plot_path)
    context_text, selection = render_context(
        state,
        chapter_number,
        outline,
        previous_tail,
        max_chars=max_chars,
        stale_warn_chapters=stale_warn_chapters,
        cooldowns=cooldowns,
    )
    if temporal_context:
        context_text = context_text.rstrip() + "\n\n" + temporal_context.strip()
    selection["context_chars"] = len(context_text)
    selection["temporal_hits"] = len(temporal_retrieval or [])
    source_hashes: dict[str, Any] = {}
    for label, raw_path in (source_paths or {}).items():
        path = Path(raw_path)
        if path.exists() and path.is_file():
            source_hashes[label] = {
                "path": str(path),
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "chapter": int(chapter_number),
        "compiled_at": _now_iso(),
        "state_chapter": int(state.get("current_chapter", 0)),
        "state_sha256": _sha256_text(json.dumps(state, ensure_ascii=False, sort_keys=True)),
        "outline_sha256": _sha256_text(outline),
        "previous_tail_sha256": _sha256_text(previous_tail),
        "source_hashes": source_hashes,
        "selection": selection,
        "temporal_retrieval": temporal_retrieval or [],
        "context_text": context_text,
    }
    if write_trace:
        trace_path = plot_path / "runtime" / f"chapter_{int(chapter_number):04d}.context.json"
        _atomic_write_json(trace_path, manifest)
        manifest["trace_path"] = str(trace_path)
    return manifest


def audit_state(plot_dir: str | Path, latest_official_chapter: int) -> dict[str, Any]:
    try:
        state = load_state(plot_dir)
    except StateLedgerError as exc:
        return {"status": "FAIL", "message": str(exc), "state_chapter": -1}
    state_chapter = int(state.get("current_chapter", 0))
    if state_chapter > latest_official_chapter:
        return {
            "status": "FAIL",
            "message": "长期正史账本领先于正式章节，可能存在被撤回正文的残留状态",
            "state_chapter": state_chapter,
        }
    if state_chapter < latest_official_chapter:
        return {
            "status": "WARN",
            "message": f"长期正史账本落后 {latest_official_chapter - state_chapter} 章，需要补齐后再续写",
            "state_chapter": state_chapter,
        }
    return {"status": "PASS", "message": "长期正史账本与正式章节同步", "state_chapter": state_chapter}
