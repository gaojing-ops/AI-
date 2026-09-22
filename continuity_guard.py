# -*- coding: utf-8 -*-
"""Deterministic canon checks for unattended chapter generation."""

import json
import os
import re
from datetime import datetime

import story_architect


CANON_FILENAME = "canon_state.json"
CANON_SNAPSHOT_DIRNAME = "canon_snapshots"


def _canon_path(plot_dir):
    return os.path.join(plot_dir, CANON_FILENAME)


def load_canon(plot_dir):
    path = _canon_path(plot_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_canon(plot_dir, data):
    path = _canon_path(plot_dir)
    os.makedirs(plot_dir, exist_ok=True)
    payload = dict(data or {})
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)
    return path


def _canon_snapshot_path(plot_dir, chapter):
    return os.path.join(
        plot_dir,
        CANON_SNAPSHOT_DIRNAME,
        f"chapter_{int(chapter):04d}.json",
    )


def save_canon_snapshot(plot_dir, data=None):
    """Save a durable full-canon checkpoint for exact last-chapter rollback."""
    payload = dict(data if isinstance(data, dict) else load_canon(plot_dir))
    if not payload:
        return ""
    chapter = int(payload.get("current_chapter") or 0)
    path = _canon_snapshot_path(plot_dir, chapter)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)
    return path


def has_canon_snapshot_before(plot_dir, chapter):
    return os.path.isfile(_canon_snapshot_path(plot_dir, int(chapter) - 1))


def restore_before_chapter(plot_dir, chapter):
    """Restore the exact canon state that existed before ``chapter``."""
    chapter = int(chapter)
    previous_path = _canon_snapshot_path(plot_dir, chapter - 1)
    if not os.path.isfile(previous_path):
        raise RuntimeError(
            f"缺少第{chapter - 1}章正史快照，不能安全覆盖第{chapter}章"
        )
    with open(previous_path, "r", encoding="utf-8-sig") as f:
        previous = json.load(f)
    if not isinstance(previous, dict):
        raise RuntimeError(f"第{chapter - 1}章正史快照格式无效")

    current_snapshot = _canon_snapshot_path(plot_dir, chapter)
    if os.path.isfile(current_snapshot):
        superseded_dir = os.path.join(
            plot_dir, CANON_SNAPSHOT_DIRNAME, "superseded"
        )
        os.makedirs(superseded_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        archived = os.path.join(
            superseded_dir, f"chapter_{chapter:04d}_{stamp}.json"
        )
        os.replace(current_snapshot, archived)

    save_canon(plot_dir, previous)
    return previous


def _realm_rank(canon, realm):
    order = canon.get("realm_order") or []
    try:
        return order.index(realm)
    except ValueError:
        return -1


_CONTINUITY_TERM_ALIASES = (
    (r"审讯|问询|正式询问", "询问"),
    (r"签字|署名|落款", "签名"),
    (r"出处|来处|源头", "来源"),
    (r"死亡窗口|死亡时点|死亡时刻", "死亡时间"),
)


def _normalize_continuity_term(value):
    """Normalize small wording differences without asking an LLM to judge canon."""
    text = str(value or "").lower()
    for pattern, replacement in _CONTINUITY_TERM_ALIASES:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff-]+", "", text)


def _continuity_term_fragments(term):
    """Return conservative anchors for a compound carry-forward requirement.

    Outline contracts naturally use labels such as ``许衡正式询问`` while
    readable prose says ``唐砺开始询问许衡``.  Requiring the whole label as a
    contiguous substring causes valid chapters to loop through rewrites.  A
    long Chinese label is therefore represented by its leading and trailing
    semantic anchors; mixed labels also keep every ASCII id (for example
    ``B-17``).  Very short labels remain exact.
    """
    normalized = _normalize_continuity_term(term)
    if not normalized:
        return []
    fragments = re.findall(r"[a-z0-9][a-z0-9-]*|[\u4e00-\u9fff]+", normalized)
    result = []
    for fragment in fragments:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]*", fragment):
            result.append(fragment)
            continue
        # An ordinal/classifier in an outline title is not the semantic core:
        # ``第二份死亡时间`` should be carried by ``死亡`` + ``时间``.
        core = re.sub(r"^第[一二三四五六七八九十百两0-9]+份?", "", fragment)
        if not core:
            core = fragment
        if len(core) <= 2:
            result.append(core)
        elif len(core) == 3:
            result.append(core[:2])
        else:
            result.extend((core[:2], core[-2:]))
    return list(dict.fromkeys(item for item in result if item))


def _continuity_requirement_present(text, term):
    normalized_text = _normalize_continuity_term(text)
    normalized_term = _normalize_continuity_term(term)
    if not normalized_term:
        return False
    if normalized_term in normalized_text:
        return True
    fragments = _continuity_term_fragments(term)
    return bool(fragments) and all(fragment in normalized_text for fragment in fragments)


def _any_continuity_requirement_present(text, required):
    return any(_continuity_requirement_present(text, term) for term in (required or []))


def _extract_declared_realms(text, canon=None):
    """Extract protagonist progression states using this project's own rank list."""
    text = text or ""
    canon = canon if isinstance(canon, dict) else {}
    order = [str(item).strip() for item in canon.get("realm_order") or [] if str(item).strip()]
    if not order:
        return []

    protagonist = str(canon.get("protagonist") or "").strip()
    subject_terms = ["主角", "主人公", "我"]
    if protagonist:
        subject_terms.insert(0, protagonist)
    subject_pattern = "(?:" + "|".join(re.escape(item) for item in subject_terms) + ")"
    realm_pattern = "(?:" + "|".join(
        re.escape(item) for item in sorted(order, key=len, reverse=True)
    ) + ")"
    explicit_patterns = (
        rf"{subject_pattern}(?:的)?(?:当前)?(?:修为|境界|等级|阶段|职级|实力)"
        rf"\s*(?:仍是|仍为|是|为|处于|停在|稳定在|达到|突破|突破到|突破至|晋入|晋升|晋升到|晋升至)"
        rf"\s*(?P<realm>{realm_pattern})",
        rf"{subject_pattern}\s*(?:现在|目前|仍然|已经|已)?"
        rf"(?:是|为|处于|停在|稳定在|达到|突破|突破到|突破至|晋入|晋升|晋升到|晋升至)"
        rf"\s*(?P<realm>{realm_pattern})",
        rf"{subject_pattern}[^。！？\n]{{0,24}}"
        rf"(?:突破|突破到|突破至|晋入|晋升|晋升到|晋升至)"
        rf"\s*(?P<realm>{realm_pattern})",
    )

    found = []
    previous_nonempty_named_protagonist = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 规则文本常在否定词后引用错误状态；它们不是对主人公当前
        # 状态的声明。否定词出现在声明之后，通常只是能力限制，
        # 不能因此跳过前面的真实境界坐标。
        negative_realm_reference = re.search(
            rf"(?:不得|禁止|不能|不再|回档|写回|不是|并非)"
            rf"[^。！？\n]{{0,30}}{realm_pattern}",
            line,
        )
        if negative_realm_reference:
            previous_nonempty_named_protagonist = bool(
                protagonist and protagonist in line
            )
            continue

        names_protagonist = bool(protagonist and protagonist in line)
        pronoun_continuation = bool(
            previous_nonempty_named_protagonist
            and re.match(r"^[“”\"'‘’]*他(?:的|现在|目前|当前|仍|已|已经|没有)", line)
        )
        explicit_realm_panel_transition = bool(re.search(
            rf"(?:境界|修为)栏[^。！？\n]{{0,18}}"
            rf"(?:改成(?:了)?|变为|升为)"
            rf"\s*{realm_pattern}",
            line,
        ))
        protagonist_scoped = bool(
            re.search(subject_pattern, line)
            or pronoun_continuation
            or explicit_realm_panel_transition
        )
        if not protagonist_scoped:
            previous_nonempty_named_protagonist = names_protagonist
            continue

        for pattern in explicit_patterns:
            match = re.search(pattern, line)
            if match:
                realm = str(match.group("realm") or "").strip()
                if realm and realm not in found:
                    found.append(realm)

        # Readable third-person prose often names the protagonist in the first
        # sentence and uses "他" for the state declaration in the same line,
        # or starts the next paragraph with that pronoun.  The older extractor
        # missed these forms and allowed a promoted protagonist to fall back to
        # an earlier realm for hundreds of chapters.
        scoped_state_match = re.search(
            rf"(?:他|本人|{re.escape(protagonist) if protagonist else '主角'})"
            rf"(?:的)?(?:当前|现在|目前)?(?:仍然|仍)?"
            rf"(?:是|为|处于|停在|停留在|仍在|只有)"
            rf"\s*(?P<realm>{realm_pattern})",
            line,
        )
        if scoped_state_match:
            realm = str(scoped_state_match.group("realm") or "").strip()
            if realm and realm not in found:
                found.append(realm)

        entry_match = re.search(
            rf"(?:踏进(?:了)?|进入|晋入|晋升(?:到|至)?|突破(?:到|至)?|稳住)"
            rf"\s*(?P<realm>{realm_pattern})(?:初段|中段|后段|圆满)?(?:后|层次|境界)?",
            line,
        )
        if entry_match:
            realm = str(entry_match.group("realm") or "").strip()
            if realm and realm not in found:
                found.append(realm)

        transition_match = re.search(
            rf"(?:境界|修为)(?:栏|记录|结果)?"
            rf"[^。！？\n]{{0,18}}(?:从\s*{realm_pattern}\s*)?"
            rf"(?:改成(?:了)?|变为|升为|稳定在|达到)"
            rf"\s*(?P<realm>{realm_pattern})(?:初段|中段|后段|圆满)?",
            line,
        )
        if transition_match:
            realm = str(transition_match.group("realm") or "").strip()
            if realm and realm not in found:
                found.append(realm)

        # A report row may name the protagonist earlier in the same line and
        # then state "境界仍是X".  Keep this narrow form, while rejecting
        # comparisons such as "沈砚清楚定架与合劲的差距".
        report_match = re.search(
            rf"(?:当前)?(?:修为|境界|等级|阶段|职级)"
            rf"\s*(?:仍是|仍为|仍处于|仍停在|是|为|处于|停在|稳定在)"
            rf"\s*(?P<realm>{realm_pattern})",
            line,
        )
        if report_match:
            realm = str(report_match.group("realm") or "").strip()
            if realm and realm not in found:
                found.append(realm)

        realm_capability_match = re.search(
            rf"(?P<realm>{realm_pattern})(?:初段|中段|后段|圆满)?"
            rf"(?:的)?境界(?:能|使|让|只能|不足以)",
            line,
        )
        if realm_capability_match:
            realm = str(realm_capability_match.group("realm") or "").strip()
            if realm and realm not in found:
                found.append(realm)
        previous_nonempty_named_protagonist = names_protagonist
    return found


def _protagonist_state_from_delta(canon, prepared_state_delta, chap_num):
    """Read evidence-backed protagonist coordinates from a validated delta.

    ``prepared_state_delta`` may be either the durable GUI wrapper or the bare
    delta used by tests and repair utilities.  The caller is responsible for
    passing a delta that has already cleared ``state_ledger.validate_delta``.
    Chapter matching keeps an accidentally stale prepared delta from changing
    the current canon.
    """
    if not isinstance(prepared_state_delta, dict):
        return {}
    payload = prepared_state_delta.get("delta")
    if not isinstance(payload, dict):
        payload = prepared_state_delta
    try:
        delta_chapter = int(payload.get("chapter") or 0)
    except (TypeError, ValueError):
        return {}
    if delta_chapter != int(chap_num or 0):
        return {}

    protagonist = str(canon.get("protagonist") or "").strip()
    if not protagonist:
        return {}
    latest: dict[str, str] = {}
    for item in reversed(payload.get("characters") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() != protagonist:
            continue
        for key in ("location", "condition", "progression"):
            value = str(item.get(key) or "").strip()
            if value and key not in latest:
                latest[key] = value
        if len(latest) == 3:
            break
    return latest


def _outline_injury_can_replace(current_injury, outline_injury):
    """Do not let an outline's generic 'no injury' erase proven live damage."""
    current = str(current_injury or "").strip()
    proposed = str(outline_injury or "").strip()
    generic_clear = {"无", "无伤", "正常", "健康", "未受伤"}
    return not (
        proposed in generic_clear
        and current
        and current not in generic_clear
    )


def build_canon_context(plot_dir, max_chars=2200):
    canon = load_canon(plot_dir)
    if not canon:
        return ""
    lines = ["【不可覆盖的正史状态】"]
    for label, key in (
        ("正史截止章", "current_chapter"),
        ("主角", "protagonist"),
        ("当前境界", "current_realm"),
        ("当前地点", "current_location"),
        ("当前伤势", "current_injury"),
        ("下一章必须承接", "open_hook"),
    ):
        value = canon.get(key)
        if value not in (None, "", []):
            lines.append(f"- {label}：{value}")
    # Identity and item lifecycle rules must stay ahead of growing event history.
    # The final truncation may discard old events, but never these hard facts.
    identity_facts = canon.get("identity_facts") or []
    if identity_facts:
        lines.append("- 人物身份硬事实：" + "；".join(str(x) for x in identity_facts))
    consumed = canon.get("consumed_items") or []
    if consumed:
        lines.append("- 已耗尽物品：" + "；".join(
            f"{item.get('label')}（第{item.get('chapter')}章耗尽）"
            for item in consumed if isinstance(item, dict)
        ))
    false_facts = canon.get("forbidden_false_facts") or []
    if false_facts:
        lines.append("- 禁止写入的错误事实：" + "；".join(str(x) for x in false_facts))
    completed = canon.get("completed_events") or []
    if completed:
        lines.append("- 已完成事件，禁止重演：" + "；".join(str(x) for x in completed[-10:]))
    text = "\n".join(lines)
    return text[:max_chars]


def _append_structured_violations(issues, text, chap_num, canon):
    for item in canon.get("forbidden_false_patterns") or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "")
        if pattern and re.search(pattern, text or "", re.IGNORECASE):
            issues.append(f"人物身份错误：{item.get('label', pattern)}")
    for item in canon.get("consumed_items") or []:
        if not isinstance(item, dict):
            continue
        completed = int(item.get("chapter", 0) or 0)
        if int(chap_num or 0) <= completed:
            continue
        for pattern in item.get("patterns") or []:
            if pattern and re.search(pattern, text or "", re.IGNORECASE):
                issues.append(f"已耗尽物品被再次使用：{item.get('label', pattern)}")
                break


def validate_outline(
    outline_text, start_chap, end_chap, plot_dir, *, require_extended=False
):
    canon = load_canon(plot_dir)
    if not canon:
        return {
            "status": "FAIL",
            "issues": ["正史状态缺失或损坏，拒绝校验细纲"],
            "summary": "正史状态缺失或损坏，拒绝校验细纲",
        }
    issues = []
    text = outline_text or ""
    found = [int(x) for x in re.findall(r"(?m)^第\s*(\d+)\s*章", text)]
    expected = list(range(int(start_chap), int(end_chap) + 1))
    # GUI 的单章提取器只返回标题下面的正文。这里接受这种合法的
    # 单章片段，并补回章节标题后再交给后续契约校验；多章输入仍须
    # 显式包含完整且连续的章节标题，不能被这个兼容分支放宽。
    if not found and int(start_chap) == int(end_chap) and text.strip():
        text = f"第{int(start_chap)}章\n{text.strip()}"
        found = [int(start_chap)]
    if found != expected:
        issues.append(f"章节覆盖错误：期望{expected}，实际{found}")

    for item in canon.get("forbidden_false_facts") or []:
        if item and item in text:
            issues.append(f"写入错误事实：{item}")
    _append_structured_violations(issues, text, start_chap, canon)

    for rule in canon.get("completed_event_patterns") or []:
        pattern = rule.get("pattern") if isinstance(rule, dict) else ""
        if pattern and re.search(pattern, text, re.IGNORECASE):
            issues.append(f"重复已完成事件：{rule.get('label', pattern)}")

    current_chapter = int(canon.get("current_chapter") or 0)
    if int(start_chap) == current_chapter + 1:
        required = canon.get("next_chapter_required_any") or []
        if required and not _any_continuity_requirement_present(text, required):
            issues.append("首章没有承接当前章末冲突：" + " / ".join(required))

    current_realm = str(canon.get("current_realm") or "")
    current_rank = _realm_rank(canon, current_realm)
    if current_rank >= 0:
        for realm in _extract_declared_realms(text, canon):
            rank = _realm_rank(canon, realm)
            if 0 <= rank < current_rank:
                issues.append(f"主角境界回档：当前{current_realm}，细纲写成{realm}")

    contract_check = story_architect.validate_outline_contract(
        text,
        start_chap,
        end_chap,
        plot_dir=plot_dir,
        strict=bool(story_architect.load_story_bible(plot_dir)),
        require_extended=bool(require_extended),
    )
    for issue in contract_check.get("issues") or []:
        if issue not in issues:
            issues.append(issue)

    return {"status": "FAIL" if issues else "PASS", "issues": issues, "summary": "；".join(issues[:4])}


def validate_chapter(chapter_text, chap_num, plot_dir):
    canon = load_canon(plot_dir)
    if not canon:
        return {
            "status": "FAIL",
            "issues": ["正史状态缺失或损坏，拒绝形成正式章节"],
            "summary": "正史状态缺失或损坏，拒绝形成正式章节",
        }
    issues = []
    text = chapter_text or ""

    for item in canon.get("forbidden_false_facts") or []:
        if item and item in text:
            issues.append(f"写入错误事实：{item}")
    _append_structured_violations(issues, text, chap_num, canon)

    current_chapter = int(canon.get("current_chapter") or 0)
    if int(chap_num) > current_chapter:
        for rule in canon.get("completed_event_patterns") or []:
            pattern = rule.get("pattern") if isinstance(rule, dict) else ""
            if pattern and re.search(pattern, text, re.IGNORECASE):
                issues.append(f"重演已完成事件：{rule.get('label', pattern)}")

        current_realm = str(canon.get("current_realm") or "")
        current_rank = _realm_rank(canon, current_realm)
        if current_rank >= 0:
            for realm in _extract_declared_realms(text, canon):
                rank = _realm_rank(canon, realm)
                if 0 <= rank < current_rank:
                    issues.append(f"主角境界回档：当前{current_realm}，正文声明为{realm}")

        if int(chap_num) == current_chapter + 1:
            required = canon.get("next_chapter_required_any") or []
            if required and not _any_continuity_requirement_present(text, required):
                issues.append("未承接上一章冲突：" + " / ".join(required))

    return {"status": "FAIL" if issues else "PASS", "issues": issues, "summary": "；".join(issues[:4])}


def validate_memory_snapshot(memory_text, plot_dir):
    """Reject an LLM memory rewrite that contradicts the current canon."""
    canon = load_canon(plot_dir)
    if not canon:
        return {"status": "PASS", "issues": [], "summary": ""}

    text = memory_text or ""
    issues = []
    protagonist = str(canon.get("protagonist") or "")
    current_realm = str(canon.get("current_realm") or "")
    if protagonist and protagonist not in text:
        issues.append(f"备忘录丢失主角坐标：{protagonist}")
    if current_realm and current_realm not in text:
        issues.append(f"备忘录丢失当前境界：{current_realm}")

    required_hook_terms = [str(x) for x in (canon.get("next_chapter_required_any") or []) if str(x)]
    if required_hook_terms and not _any_continuity_requirement_present(text, required_hook_terms):
        issues.append("备忘录丢失最新章末冲突：" + " / ".join(required_hook_terms[:5]))

    guard_pattern = re.compile(r"禁止|不得|不能|错误事实|不存在|并非|不是|没有|防止|避免")
    history_pattern = re.compile(r"已完成|完成事件|此前|曾经|过去|第\s*\d+(?:\s*[-至到]\s*\d+)?\s*章")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        is_guard = bool(guard_pattern.search(stripped))
        is_history = bool(history_pattern.search(stripped))

        if not is_guard:
            for false_fact in canon.get("forbidden_false_facts") or []:
                if false_fact and str(false_fact) in stripped:
                    issues.append(f"备忘录写入错误事实：{false_fact}")

            for item in canon.get("forbidden_false_patterns") or []:
                if not isinstance(item, dict):
                    continue
                pattern = str(item.get("pattern") or "")
                if pattern and re.search(pattern, stripped, re.IGNORECASE):
                    issues.append(f"备忘录人物身份错误：{item.get('label', pattern)}")

        if not is_guard and not is_history:
            for item in canon.get("consumed_items") or []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "")
                lifecycle_safe = bool(re.search(
                    r"耗尽|用尽|消耗完|已经销毁|已销毁|不再持有|已不存在",
                    stripped,
                ))
                if label and label in stripped and not lifecycle_safe:
                    issues.append(f"备忘录复活已耗尽物品：{label}")
                    continue
                for pattern in item.get("patterns") or []:
                    if pattern and re.search(pattern, stripped, re.IGNORECASE):
                        issues.append(f"备忘录复活已耗尽物品：{item.get('label', pattern)}")
                        break

    # Only present-state lines count as a realm declaration. Historical summaries
    # are allowed to mention the protagonist's earlier levels.
    current_rank = _realm_rank(canon, current_realm)
    if current_rank >= 0:
        for line in text.splitlines():
            protagonist = str(canon.get("protagonist") or "").strip()
            protagonist_marker = rf"|{re.escape(protagonist)}[：:]" if protagonist else ""
            if not re.search(rf"当前|即时状态|核心坐标|主角[：:]|主人公[：:]{protagonist_marker}", line):
                continue
            for realm in canon.get("realm_order") or []:
                realm = str(realm)
                if not realm or realm not in line:
                    continue
                rank = _realm_rank(canon, realm)
                if 0 <= rank < current_rank:
                    issues.append(f"备忘录境界回档：当前{current_realm}，却写成{realm}")

    issues = list(dict.fromkeys(issues))
    return {
        "status": "FAIL" if issues else "PASS",
        "issues": issues,
        "summary": "；".join(issues[:4]),
    }


def update_after_chapter(
    plot_dir,
    chap_num,
    chapter_text,
    chapter_outline="",
    prepared_state_delta=None,
):
    canon = load_canon(plot_dir)
    if not canon:
        raise RuntimeError("正史状态缺失或损坏，拒绝更新")
    save_canon_snapshot(plot_dir, canon)
    canon["current_chapter"] = max(int(canon.get("current_chapter") or 0), int(chap_num or 0))
    realms = _extract_declared_realms(chapter_text, canon)
    if realms:
        current = str(canon.get("current_realm") or "")
        best = current
        best_rank = _realm_rank(canon, current)
        for realm in realms:
            rank = _realm_rank(canon, realm)
            if rank > best_rank:
                best = realm
                best_rank = rank
        if best:
            canon["current_realm"] = best
    contract = story_architect.outline_state_update(chapter_outline)
    # The outline is a plan, never evidence.  Realm, location, injury and
    # completed events may only advance from the independently validated delta.
    protagonist_state = _protagonist_state_from_delta(
        canon, prepared_state_delta, chap_num
    )
    if protagonist_state.get("progression"):
        progression = protagonist_state["progression"]
        if progression not in (canon.get("realm_order") or []):
            raise RuntimeError("证据增量中的主角阶段不在项目阶段表内，拒绝更新")
        # The independent delta audit checks natural-language entailment.  A
        # literal label need not occur in prose, and a real demotion is possible.
        canon["current_realm"] = progression
        canon["current_realm_source_chapter"] = int(chap_num)
    if protagonist_state.get("location"):
        canon["current_location"] = protagonist_state["location"]
    elif prepared_state_delta:
        # A character's location is volatile.  If this chapter has a validated
        # structured delta but no explicit protagonist location, retaining an
        # older place (for example "home") can falsely contradict a chapter
        # that visibly continues on the training ground.
        canon["current_location"] = "未结构化，以最新正式章节正文为准"
    if protagonist_state.get("condition"):
        canon["current_injury"] = protagonist_state["condition"]
    elif not prepared_state_delta:
        # Lightweight release intentionally skips model-extracted structured
        # state.  "Unknown" is safer than carrying the starter value "无" into
        # later prompts and falsely contradicting an injury shown in the prose.
        canon["current_injury"] = "未结构化，以最新正式章节正文为准"

    compact_text = re.sub(r"\s+", "", chapter_text or "")
    canon["open_hook"] = compact_text[-220:] or "必须承接最新正式章节结尾。"
    # Future outline targets are planning context, not facts established by the
    # chapter just committed.  Keeping them here would let the outline certify
    # its own completion on the next pass.
    canon["next_chapter_required_any"] = []

    delta = (
        prepared_state_delta.get("delta")
        if isinstance(prepared_state_delta, dict) else None
    ) or {}
    delta_events = delta.get("events") if isinstance(delta, dict) else []
    completed_event = ""
    if isinstance(delta_events, list) and delta_events:
        completed_event = str((delta_events[0] or {}).get("summary") or "").strip()
    if completed_event:
        event = f"第{chap_num}章：{completed_event}"
        completed = list(canon.get("completed_events") or [])
        if event not in completed:
            completed.append(event)
        # One compact event per chapter is cheap to retain and provides a
        # full-book audit trail. Prompt construction still injects only the
        # recent slice, so this does not cause context growth.
        canon["completed_events"] = completed[-2000:]

    recent = [
        item for item in (canon.get("recent_chapter_contracts") or [])
        if item.get("chapter") != chap_num
    ]
    recent.append({
        "chapter": chap_num,
        "title": (chapter_text or "").strip().split("\n", 1)[0][:60],
        "state": contract,
    })
    canon["recent_chapter_contracts"] = recent[-20:]
    path = save_canon(plot_dir, canon)
    save_canon_snapshot(plot_dir)
    return path
