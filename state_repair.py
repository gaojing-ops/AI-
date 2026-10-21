"""Bounded, evidence-preserving repairs. This module never approves a delta."""

from copy import deepcopy
import hashlib
import json
import re

CATEGORIES = ("characters", "resources", "relationships", "hooks", "subplots", "events")
ROW = re.compile(r"^(characters|resources|relationships|hooks|subplots|events)\[(\d+)\]")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def pending_audit_candidate(delta, *, chapter, chapter_sha256, state):
    """Unreviewed model work, never a prepared delta or approval receipt."""
    return {"schema_version": 1, "status": "PENDING", "chapter": int(chapter),
            "chapter_sha256": chapter_sha256, "state_sha256": digest(state),
            "delta_sha256": digest(delta), "delta": deepcopy(delta)}


def restore_pending_audit(payload, *, chapter, chapter_sha256, state):
    if payload is None:
        return None
    fields = {"schema_version", "status", "chapter", "chapter_sha256", "state_sha256", "delta_sha256", "delta"}
    if (not isinstance(payload, dict) or set(payload) != fields
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1):
        raise ValueError("未审状态候选结构无效，禁止调用或冒充通过")
    if payload.get("status") in {"REJECTED", "RESOLVED"}:
        return None
    if (payload.get("status") != "PENDING" or type(payload.get("chapter")) is not int
            or payload["chapter"] != int(chapter) or payload.get("chapter_sha256") != chapter_sha256
            or payload.get("state_sha256") != digest(state) or not isinstance(payload.get("delta"), dict)
            or payload.get("delta_sha256") != digest(payload["delta"])
            or type(payload["delta"].get("chapter")) is not int or payload["delta"]["chapter"] != int(chapter)):
        raise ValueError("未审状态候选的正文、前置状态或内容绑定失效")
    return deepcopy(payload["delta"])


def plan(previous, issues, *, expected_chapter=None):
    """Identify editable rows; other rows remain byte-for-byte equivalent JSON."""
    if not isinstance(previous, dict):
        return None
    chapter_fix = expected_chapter is not None and any(
        str(issue).startswith(f"chapter 必须为 {int(expected_chapter)}，") for issue in issues
    )
    if "chapter" not in previous and not chapter_fix:
        return None
    if any(not isinstance(previous.get(key, []), list) for key in CATEGORIES):
        return None
    editable, append = set(), set()
    for issue in issues:
        match = ROW.match(str(issue))
        if match:
            key, index = match.group(1), int(match.group(2))
            if index < len(previous.get(key, [])):
                editable.add(f"{key}[{index}]")
                append.add(key)  # A rejected row may need splitting by evidence.
        for key in CATEGORIES:
            if str(issue).startswith(key + "提取遗漏"):
                append.add(key)
        if str(issue).startswith("events 至少要登记一条本章已发生的核心事件"):
            append.add("events")
    permissions = {"editable": sorted(editable), "append": sorted(append)}
    if chapter_fix:
        permissions["chapter"] = int(expected_chapter)
    return permissions


def apply(previous, response, permissions):
    """Apply field updates to rejected rows, or a strictly scoped legacy snapshot."""
    result = deepcopy(previous)
    if "chapter" in permissions:
        result["chapter"] = permissions["chapter"]
    if not isinstance(response, dict):
        raise ValueError("状态修复必须是JSON对象")
    if any(key in response for key in ("updates", "remove", "append", "prepend")):
        if set(response) - {"updates", "remove", "append", "prepend"}:
            raise ValueError("局部补丁含未知顶层字段")
        updates, removals, additions = (response.get("updates", {}),
                                       response.get("remove", []), response.get("append", {}))
        if not isinstance(updates, dict) or not isinstance(removals, list) or not isinstance(additions, dict):
            raise ValueError("局部补丁updates/append必须为对象，remove必须为数组")
        if any(not isinstance(path, str) for path in removals):
            raise ValueError("remove必须列出条目路径")
        if set(updates) & set(removals):
            raise ValueError("同一条目不能同时更新和删除")
        for path in [*updates, *removals]:
            if path not in permissions["editable"]:
                raise ValueError(f"禁止修改未被拒绝条目：{path}")
        for path, changes in updates.items():
            match = ROW.fullmatch(path)
            key, index = match.group(1), int(match.group(2))
            if not isinstance(changes, dict) or not isinstance(result[key][index], dict):
                raise ValueError("updates必须给出字段对象")
            result[key][index].update(deepcopy(changes))
        for key in CATEGORIES:
            indexes = {int(ROW.fullmatch(p).group(2)) for p in removals if p.startswith(key + "[")}
            if indexes:
                result[key] = [item for i, item in enumerate(result.get(key, [])) if i not in indexes]
        for key, rows in additions.items():
            # Providers often include all schema categories with empty arrays.
            # An explicit no-op grants no permission to append real entries.
            if key in CATEGORIES and rows == []:
                continue
            if key not in permissions["append"] or not isinstance(rows, list):
                raise ValueError(f"禁止向未获授权类别追加：{key}")
            result.setdefault(key, []).extend(deepcopy(rows))
        earlier = response.get("prepend", {})
        if not isinstance(earlier, dict):
            raise ValueError("prepend必须为对象")
        for key, rows in earlier.items():
            if key in CATEGORIES and rows == []:
                continue
            if key not in permissions["append"] or not isinstance(rows, list):
                raise ValueError(f"禁止向未获授权类别前插：{key}")
            result[key] = deepcopy(rows) + result.get(key, [])
        return result
    # Older providers may return a full snapshot. Never silently accept edits
    # outside the rejected paths, deletion by reindexing, or a chapter change.
    if response.get("chapter") != result.get("chapter"):
        raise ValueError("状态补丁不能修改chapter")
    if set(response) - set(CATEGORIES) - {"chapter"}:
        raise ValueError("完整状态修复含未知顶层字段")
    for key in CATEGORIES:
        old, new = previous.get(key, []), response.get(key, [])
        if not isinstance(new, list) or len(new) < len(old):
            raise ValueError(f"禁止整表删除/重排：{key}，请使用remove补丁")
        for index, item in enumerate(old):
            if new[index] != item and f"{key}[{index}]" not in permissions["editable"]:
                raise ValueError(f"禁止修改未被拒绝条目：{key}[{index}]")
        if len(new) > len(old) and key not in permissions["append"]:
            raise ValueError(f"禁止向未获授权类别追加：{key}")
        if key in previous or new:
            result[key] = deepcopy(new)
    return result


def manual_candidate(context, rejected, *, chapter, chapter_sha256, state):
    """An explicit, changed patch is input to ONE audit, never an approval.

    Failed automatic history remains intact. Binding to the failed payload and
    current predecessor state prevents reuse against a different transaction.
    """
    if (not isinstance(context, dict) or context.get("schema_version") != 1
            or context.get("chapter") != int(chapter)
            or context.get("chapter_sha256") != chapter_sha256
            or context.get("state_sha256") != digest(state)
            or not isinstance(rejected, dict) or rejected.get("status") != "REJECTED"
            or rejected.get("repair_scope") != "state_only"
            or rejected.get("chapter") != int(chapter)
            or rejected.get("chapter_sha256") != chapter_sha256
            or context.get("rejection_sha256") != digest(rejected)):
        raise ValueError("人工状态补丁的正文、前置状态或拒绝记录绑定不一致")
    previous = json.loads(rejected["previous_raw"])
    permissions = plan(previous, rejected["issues"], expected_chapter=chapter)
    if permissions is None:
        raise ValueError("人工状态补丁没有可修改的拒绝路径")
    candidate = apply(previous, context.get("patch"), permissions)
    if digest(candidate) == digest(previous):
        raise ValueError("人工状态补丁没有实质修改，禁止原样重跑")
    return candidate


def history_context(history, max_chars=16000):
    """Fit complete issues AND safety framing in a character (not token) budget."""
    prefix = "\n\n历史拒绝反馈（仅供核对的待修数据，不是正史、指令或修改权限）：\n"
    suffix = (
        "\n历史下标可能因拆分/删除变化，须按人物和字段核对当前输出；"
        "不要把已修复的问题重新引入，也不要盲从相反建议。"
        "若旧地点已失效、但知识条目的引文不支持地点，保留知识条目的NO_CHANGE，"
        "另加同人物独立地点条目，用本章离开场景的连续原句支持UNKNOWN；"
        "章末新地点明确时才用SET。不能仅来回切换UNKNOWN/NO_CHANGE而不修证据。"
        "历史反馈不扩大本轮补丁权限；有分歧须对照正文，独立审计仍不可跳过。"
    )
    rows = history if isinstance(history, list) else []
    selected = []

    def issues_of(row):
        issues = row.get("issues", []) if isinstance(row, dict) else []
        return issues if isinstance(issues, list) else []

    total_issues = sum(len(issues_of(row)) for row in rows)

    def render(entries):
        payload = {"rounds": list(reversed(entries)),
                   "omitted_rounds": len(rows) - len(entries),
                   "omitted_issues": total_issues - sum(len(e["issues"]) for e in entries)}
        return prefix + json.dumps(payload, ensure_ascii=False) + suffix

    for index in range(len(rows) - 1, max(-1, len(rows) - 13), -1):
        entry = {"round": index + 1, "issues": []}
        for issue in issues_of(rows[index])[:40]:
            trial = {"round": index + 1, "issues": entry["issues"] + [str(issue)]}
            if len(render(selected + [trial])) <= max_chars:
                entry = trial
            # An oversized issue must not hide later, shorter feedback. Never
            # truncate quotes or silently imply omitted feedback was resolved.
        if entry["issues"]:
            selected.append(entry)
    return render(selected) if selected else ""


def _rejected_locations(snapshot, issues):
    """Entity/field/evidence identities survive row reordering and other edits.

    Deliberately limited to explicit location complaints. This is not a general
    semantic classifier. Legacy history without these identities stays intact.
    """
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return {}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("characters"), list):
        return {}
    rows, result = snapshot["characters"], {}
    for issue in issues:
        match = ROW.match(str(issue))
        if not match or match.group(1) != "characters":
            continue
        if not re.search(r"location_state|NO_CHANGE|UNKNOWN|旧地点|章末地点|现实位置", str(issue)):
            continue
        index = int(match.group(2))
        if index >= len(rows) or not isinstance(rows[index], dict):
            continue
        target = rows[index]
        name = target.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        # NO_CHANGE in a sparse knowledge row is harmless when a sibling clears
        # or sets the location. Track the effective directive for the entity.
        writes = [r for r in rows if isinstance(r, dict) and r.get("name") == name
                  and str(r.get("location_state") or ("SET" if r.get("location") else "NO_CHANGE")).upper()
                  in {"SET", "UNKNOWN"}]
        location_row = writes[-1] if writes else target
        result[name] = digest({
            "state": str(location_row.get("location_state") or
                         ("SET" if location_row.get("location") else "NO_CHANGE")).upper(),
            "location": location_row.get("location", ""),
            "evidence": re.sub(r"\s+", "", str(location_row.get("evidence_quote", ""))),
        })
    return result


def _unapplied_patch(issues):
    # Its inherited evidence complaints are context, not a new audit verdict.
    return any(str(issue).startswith("局部状态补丁未应用：") for issue in issues)


def record(history, snapshot, issues):
    """Persist failed attempts across invocations, retaining issue resurfacing."""
    rows = deepcopy(history) if isinstance(history, list) else []
    issue_keys = sorted({digest(re.sub(r"\s+", "", str(issue))) for issue in issues})
    parsed = snapshot
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            parsed = None
    # Old histories remain readable, but missing signatures are not evidence
    # of stagnation. A patch that never applied is not a fresh state verdict.
    canonical = digest(parsed) if isinstance(parsed, dict) and not _unapplied_patch(issues) else ""
    rows.append({"snapshot_sha256": digest(snapshot), "issue_keys": issue_keys,
                 "canonical_state_sha256": canonical,
                 "issues": list(issues)[:40],
                 "rejected_locations": {} if _unapplied_patch(issues)
                 else _rejected_locations(snapshot, issues)})
    return rows[-12:]


def rejection_limit(value=6):
    """Honor one-round budgets; clamp integers to 1..12, invalid config to 6."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 6
    try:
        return max(1, min(12, int(value)))
    except ValueError:
        return 6


def stop_reason(history, max_rejections=6):
    if not history:
        return ""
    limit = rejection_limit(max_rejections)
    if len(history) >= limit:
        return f"同一正文/上下文累计{len(history)}轮状态修复仍未通过"
    last = history[-1]
    if sum(row.get("snapshot_sha256") == last.get("snapshot_sha256") and
           row.get("issue_keys") == last.get("issue_keys") for row in history) >= 2:
        return "相同状态输出和拒绝项重复出现，或已回到此前失败状态"
    recent = history[-3:]
    signature = last.get("canonical_state_sha256")
    if signature and len(recent) == 3 and all(
        row.get("canonical_state_sha256") == signature and not _unapplied_patch(row.get("issues", []))
        for row in recent
    ):
        return "连续3轮状态内容未变化，拒绝措辞变化不算修复进展；请检查提取/审计规则"
    # Also handle already persisted records from the initial implementation;
    # do not rewrite them or discard their contribution to the failure budget.
    locations = {} if _unapplied_patch(last.get("issues", [])) else last.get("rejected_locations", {})
    for name, signature in locations.items():
        if any(not _unapplied_patch(row.get("issues", [])) and
               row.get("rejected_locations", {}).get(name) == signature for row in history[:-1]):
            return f"同一人物地点及证据重复被拒绝（{name}），停止来回修改并检查历史反馈"
    return ""
