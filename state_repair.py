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
    if any(key in response for key in ("updates", "remove", "append")):
        if set(response) - {"updates", "remove", "append"}:
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


def record(history, snapshot, issues):
    """Persist failed attempts across invocations, retaining issue resurfacing."""
    rows = deepcopy(history) if isinstance(history, list) else []
    issue_keys = sorted({digest(re.sub(r"\s+", "", str(issue))) for issue in issues})
    rows.append({"snapshot_sha256": digest(snapshot), "issue_keys": issue_keys,
                 "issues": list(issues)[:40]})
    return rows[-12:]


def stop_reason(history, max_rejections=6):
    if not history:
        return ""
    limit = max(2, min(12, int(max_rejections)))
    if len(history) >= limit:
        return f"同一正文/上下文累计{len(history)}轮状态修复仍未通过"
    last = history[-1]
    if sum(row.get("snapshot_sha256") == last.get("snapshot_sha256") and
           row.get("issue_keys") == last.get("issue_keys") for row in history) >= 2:
        return "相同状态输出和拒绝项重复出现，或已回到此前失败状态"
    return ""
