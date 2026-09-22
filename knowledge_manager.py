# -*- coding: utf-8 -*-
"""Persistent knowledge helpers for long-form unattended writing.

This is intentionally deterministic: it does not call the LLM. The GUI and
skill pipeline already produce markdown memory files; this module snapshots
their current state into a JSON fact database and builds a compact prompt block
for the next chapter.
"""

import json
import os
import re
import shutil
from datetime import datetime

import story_architect


FACT_DB_FILENAME = "关键事实库.json"
SOURCE_FILES = (
    "全局备忘录.txt",
    "实体状态表.txt",
    "世界编年史.txt",
    "伏笔与因果追踪表.txt",
    "时间线锚点.txt",
)

STYLE_PHRASE_LIMITS = {
    "深吸一口气": 4,
    "猛地": 8,
    "忽然": 8,
    "就在这时": 4,
    "瞳孔猛地一缩": 2,
    "没有回答": 4,
    "没有说话": 3,
    "沉默了几秒": 3,
    "死死": 4,
    "疯狂": 4,
    "微微": 8,
    "心里一沉": 2,
    "心中一凛": 2,
}


def _read_text(path):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip() + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def _fact_db_path(plot_dir):
    return os.path.join(plot_dir, FACT_DB_FILENAME)


def sanitize_memory_sources(plot_dir):
    """Remove known extraction hallucinations before they enter the fact DB."""
    canon_path = os.path.join(plot_dir, "canon_state.json")
    try:
        with open(canon_path, "r", encoding="utf-8") as f:
            canon = json.load(f)
        replacements = canon.get("memory_replacements") or {}
    except Exception:
        replacements = {}
    if not replacements:
        return []

    changed = []
    for filename in SOURCE_FILES:
        path = os.path.join(plot_dir, filename)
        text = _read_text(path)
        if not text:
            continue
        updated_lines = []
        for line in text.splitlines():
            updated_line = line
            for wrong, correct in replacements.items():
                wrong = str(wrong)
                if wrong not in updated_line:
                    continue
                # A negative guard names the bad fact on purpose. Replacing it
                # reverses the instruction (for example, "no old name" becomes
                # "no current name") and poisons every later fact snapshot.
                if re.search(r"禁止|不得|不能|错误事实|不存在|并非|不是|没有", updated_line):
                    continue
                updated_line = updated_line.replace(wrong, str(correct))
            updated_lines.append(updated_line)
        updated = "\n".join(updated_lines)
        if updated != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
            changed.append(path)
    return changed


def load_fact_db(plot_dir):
    path = _fact_db_path(plot_dir)
    if not os.path.exists(path):
        return {
            "schema_version": 1,
            "updated_at": "",
            "latest_chapter": 0,
            "chapters": {},
            "recent_chapters": [],
            "sources": {},
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("fact db must be object")
        data.setdefault("schema_version", 1)
        data.setdefault("chapters", {})
        data.setdefault("recent_chapters", [])
        data.setdefault("sources", {})
        return data
    except Exception:
        return {
            "schema_version": 1,
            "updated_at": "",
            "latest_chapter": 0,
            "chapters": {},
            "recent_chapters": [],
            "sources": {},
            "load_error": "旧事实库无法解析，已重建",
        }


def _chapter_title(chapter_content, chap_num):
    first = (chapter_content or "").strip().split("\n", 1)[0].strip().lstrip("#").strip()
    if first:
        return first[:60]
    return f"第{chap_num}章"


def _tail_hook(chapter_content):
    text = re.sub(r"\s+", "", chapter_content or "")
    if not text:
        return ""
    return text[-180:]


def _source_snapshot(plot_dir, filename, max_chars=1800):
    path = os.path.join(plot_dir, filename)
    text = _read_text(path)
    if not text:
        return None
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except Exception:
        mtime = ""
    return {
        "updated_at": mtime,
        "chars": len(text),
        "excerpt": _clip_keep_ends(text, max_chars),
    }


def update_fact_db(plot_dir, chap_num, chapter_content, chapter_path="", chapter_outline=""):
    sanitize_memory_sources(plot_dir)
    db = load_fact_db(plot_dir)
    now = datetime.now().isoformat(timespec="seconds")
    chars = len(re.findall(r"[\u4e00-\u9fff]", chapter_content or ""))
    key = str(chap_num)
    chapter_record = {
        "chapter": chap_num,
        "title": _chapter_title(chapter_content, chap_num),
        "chars": chars,
        "path": chapter_path,
        "tail_hook": _tail_hook(chapter_content),
        "updated_at": now,
        "style_phrase_counts": {
            phrase: (chapter_content or "").count(phrase)
            for phrase in STYLE_PHRASE_LIMITS
            if phrase in (chapter_content or "")
        },
    }
    outline_fields = story_architect.parse_outline_fields(chapter_outline)
    if outline_fields:
        chapter_record["outline_contract"] = outline_fields
    db["chapters"][key] = chapter_record
    db["latest_chapter"] = max(int(db.get("latest_chapter") or 0), int(chap_num or 0))
    # Older writers stored this alias too. Keep it synchronized on mutations,
    # without repairing files as a side effect of a read-only load.
    if "current_chapter" in db:
        db["current_chapter"] = db["latest_chapter"]
    db["updated_at"] = now

    recent = [item for item in db.get("recent_chapters", []) if item.get("chapter") != chap_num]
    recent.append(chapter_record)
    recent.sort(key=lambda item: int(item.get("chapter") or 0))
    db["recent_chapters"] = recent[-30:]

    sources = {}
    for filename in SOURCE_FILES:
        snapshot = _source_snapshot(plot_dir, filename)
        if snapshot:
            sources[filename] = snapshot
    db["sources"] = sources

    canon_path = os.path.join(plot_dir, "canon_state.json")
    try:
        with open(canon_path, "r", encoding="utf-8") as f:
            canon = json.load(f)
        if isinstance(canon, dict):
            db["canon"] = canon
    except Exception:
        pass

    path = _fact_db_path(plot_dir)
    _write_json(path, db)
    return path


def truncate_fact_db_after(plot_dir, chapter):
    """Remove fact records newer than ``chapter`` before a formal tail rebuild.

    The caller is responsible for wrapping this mutation in the project-level
    suffix transaction.  This helper only rewinds deterministic fact records;
    it never invents replacements for the removed chapters.
    """
    chapter = int(chapter)
    if chapter < 0:
        raise ValueError("事实库回退章节不能小于0")
    db = load_fact_db(plot_dir)
    chapters = {}
    for key, value in dict(db.get("chapters") or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        if number <= chapter:
            chapters[str(number)] = value
    recent = [
        item for item in list(db.get("recent_chapters") or [])
        if int((item or {}).get("chapter") or 0) <= chapter
    ]
    db["chapters"] = chapters
    db["recent_chapters"] = sorted(
        recent, key=lambda item: int((item or {}).get("chapter") or 0)
    )[-30:]
    db["latest_chapter"] = max([0, *[int(key) for key in chapters]])
    if "current_chapter" in db:
        db["current_chapter"] = db["latest_chapter"]
    db["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(os.path.join(plot_dir, "canon_state.json"), "r", encoding="utf-8") as f:
            canon = json.load(f)
        if isinstance(canon, dict):
            db["canon"] = canon
    except Exception:
        db.pop("canon", None)
    path = _fact_db_path(plot_dir)
    _write_json(path, db)
    return path


def rebuild_memory_sources_from_structured_state(plot_dir, archive_tag=""):
    """Replace stale skill-written memory after an official chapter amendment.

    The old files are retained for audit, while every active prompt source is
    rebuilt only from the verified state ledger and current canon.
    """
    import state_ledger

    if archive_tag:
        safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(archive_tag))
        archive_dir = os.path.join(plot_dir, "superseded_memory", safe_tag)
        marker = os.path.join(archive_dir, ".archived")
        os.makedirs(archive_dir, exist_ok=True)
        if not os.path.exists(marker):
            for filename in SOURCE_FILES:
                source = os.path.join(plot_dir, filename)
                if os.path.isfile(source):
                    shutil.copy2(source, os.path.join(archive_dir, filename))
            _write_text(marker, datetime.now().isoformat(timespec="seconds"))

    state = state_ledger.load_state(plot_dir)
    try:
        with open(os.path.join(plot_dir, "canon_state.json"), "r", encoding="utf-8") as f:
            canon = json.load(f)
        if not isinstance(canon, dict):
            canon = {}
    except Exception:
        canon = {}

    chapter = int(state.get("current_chapter") or 0)
    characters = state.get("characters") or {}
    resources = state.get("resources") or {}
    hooks = state.get("hooks") or {}
    subplots = state.get("subplots") or {}
    events = state.get("event_history") or []
    timeline_events = state.get("timeline") or events

    entity_lines = [f"# 实体状态表（第{chapter}章后，证据账本重建）", ""]
    for name, item in sorted(characters.items()):
        entity_lines.append(
            f"- {name}：地点={item.get('location') or '未知'}；"
            f"状态={item.get('status') or '未知'}；"
            f"身体={item.get('condition') or '未知'}；"
            f"已知={ '、'.join(item.get('knowledge') or []) or '无已登记信息' }"
        )
    for item in resources.values():
        entity_lines.append(
            f"- 资源：{item.get('owner') or '未知'} / {item.get('item') or '未命名'} / "
            f"{item.get('status') or '状态未知'} / 数量={item.get('quantity') if item.get('quantity') is not None else '未知'}"
        )
    if len(entity_lines) == 2:
        entity_lines.append("- 暂无经证据确认的实体状态。")

    event_lines = [f"# 世界编年史（截至第{chapter}章，证据账本重建）", ""]
    for item in events:
        event_lines.append(
            f"- 第{item.get('chapter')}章：{item.get('summary') or '未命名事件'}"
        )
    if len(event_lines) == 2:
        event_lines.append("- 暂无经证据确认的历史事件。")

    timeline_lines = [f"# 时间线锚点（截至第{chapter}章，证据账本重建）", ""]
    for item in timeline_events:
        chapter_number = item.get("chapter") or "?"
        event_order = item.get("event_order") or 1
        time_anchor = item.get("time_anchor") or "本章未明确时间"
        duration = item.get("duration") or "未明确时长"
        location = item.get("location") or "本事件原句未明确地点"
        summary = item.get("summary") or "未命名事件"
        timeline_lines.append(
            f"- 第{chapter_number}章·事件{event_order}："
            f"时间={time_anchor}；时长={duration}；地点={location}；事件={summary}"
        )
    if len(timeline_lines) == 2:
        timeline_lines.append("- 暂无经证据确认的时间、时长或地点锚点。")

    hook_lines = [f"# 伏笔与因果追踪表（第{chapter}章后，证据账本重建）", ""]
    for item in hooks.values():
        hook_lines.append(
            f"- 伏笔 {item.get('id') or '未编号'}：{item.get('label') or '未命名'}；"
            f"状态={item.get('status') or '未知'}"
        )
    for item in subplots.values():
        hook_lines.append(
            f"- 支线 {item.get('id') or '未编号'}：{item.get('label') or '未命名'}；"
            f"状态={item.get('status') or '未知'}"
        )
    if len(hook_lines) == 2:
        hook_lines.append("- 暂无经证据确认的开放伏笔或支线。")

    canon_lines = [f"# 全局备忘录（第{chapter}章后，证据账本重建）", ""]
    canon_lines.extend([
        f"- 当前地点：{canon.get('current_location') or '未知'}",
        f"- 当前阶段：{canon.get('current_realm') or '未知'}",
        f"- 当前伤势：{canon.get('current_injury') or '未知'}",
        f"- 当前章末钩子：{canon.get('open_hook') or '无'}",
        "",
        "## 经验证实体",
        *entity_lines[2:],
        "",
        "## 最近经验证事件",
        *event_lines[-30:],
        "",
        "## 开放伏笔与支线",
        *hook_lines[2:],
    ])

    _write_text(os.path.join(plot_dir, "实体状态表.txt"), "\n".join(entity_lines))
    _write_text(os.path.join(plot_dir, "世界编年史.txt"), "\n".join(event_lines))
    _write_text(os.path.join(plot_dir, "时间线锚点.txt"), "\n".join(timeline_lines))
    _write_text(os.path.join(plot_dir, "伏笔与因果追踪表.txt"), "\n".join(hook_lines))
    _write_text(os.path.join(plot_dir, "全局备忘录.txt"), "\n".join(canon_lines))
    return [os.path.join(plot_dir, filename) for filename in SOURCE_FILES]


def _clip(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[已截断]"


def _clip_keep_ends(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = max(200, limit * 2 // 5)
    tail = max(200, limit - head)
    return text[:head].rstrip() + "\n[中段已截断，保留最新尾部]\n" + text[-tail:].lstrip()


def build_generation_context(plot_dir, chap_num, max_chars=3200, include_sources=True):
    db = load_fact_db(plot_dir)
    parts = []

    recent = db.get("recent_chapters") or []
    if recent:
        lines = []
        for item in recent[-8:]:
            lines.append(
                f"- 第{item.get('chapter')}章：{item.get('title', '')}；"
                f"章尾钩子：{_clip(item.get('tail_hook', ''), 90)}"
            )
        parts.append("【近期章节事实】\n" + "\n".join(lines))

        aggregate = {phrase: 0 for phrase in STYLE_PHRASE_LIMITS}
        for item in recent[-8:]:
            for phrase, count in (item.get("style_phrase_counts") or {}).items():
                if phrase in aggregate:
                    aggregate[phrase] += int(count or 0)
        overused = [
            phrase for phrase, limit in STYLE_PHRASE_LIMITS.items()
            if aggregate.get(phrase, 0) >= limit
        ]
        if overused:
            parts.append(
                "【近期高频表达禁用】\n下一章不要再使用：" + "、".join(overused)
                + "。改用具体动作、现场声音或直接叙事。"
            )

    if include_sources:
        source_budget = max(1200, max_chars - sum(len(p) for p in parts))
        per_source = max(350, source_budget // max(1, len(SOURCE_FILES)))
        for filename in SOURCE_FILES:
            snapshot = (db.get("sources") or {}).get(filename)
            if snapshot and snapshot.get("excerpt"):
                parts.append(f"【{filename}】\n{_clip(snapshot.get('excerpt', ''), per_source)}")
            else:
                raw = _read_text(os.path.join(plot_dir, filename))
                if raw:
                    parts.append(f"【{filename}】\n{_clip(raw, per_source)}")

    text = "\n\n".join(part for part in parts if part.strip())
    if not text:
        return ""
    return _clip(text, max_chars)


def _managed_header(chap_num):
    return (
        "<!-- AUTO-MANAGED: generated by knowledge_manager.py. "
        "Manual edits below this file may be overwritten. -->\n\n"
        f"更新时间：第{chap_num}章后\n\n"
    )


def write_knowledge_snapshots(plot_dir, chars_dir, world_dir, chap_num):
    """Copy durable plot memory into the character/world folders.

    This keeps the folders from staying empty README shells and lets existing
    prompt loaders pick up current entity and world-state context.
    """
    os.makedirs(chars_dir, exist_ok=True)
    os.makedirs(world_dir, exist_ok=True)

    entity_state = _read_text(os.path.join(plot_dir, "实体状态表.txt"))
    memo = _read_text(os.path.join(plot_dir, "全局备忘录.txt"))
    chronicle = _read_text(os.path.join(plot_dir, "世界编年史.txt"))
    foreshadow = _read_text(os.path.join(plot_dir, "伏笔与因果追踪表.txt"))
    timeline = _read_text(os.path.join(plot_dir, "时间线锚点.txt"))

    written = []
    if entity_state or memo:
        path = os.path.join(chars_dir, "自动角色状态快照.md")
        content = (
            _managed_header(chap_num)
            + "# 自动角色状态快照\n\n"
            + "## 实体状态表\n\n"
            + (entity_state or "暂无实体状态表。")
            + "\n\n## 全局备忘录摘录\n\n"
            + _clip(memo, 2200)
            + "\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(path)

    if chronicle or timeline or foreshadow or memo:
        path = os.path.join(world_dir, "自动世界观状态快照.md")
        content = (
            _managed_header(chap_num)
            + "# 自动世界观状态快照\n\n"
            + "## 世界编年史\n\n"
            + (chronicle or "暂无世界编年史。")
            + "\n\n## 时间线锚点\n\n"
            + (timeline or "暂无时间线锚点。")
            + "\n\n## 伏笔与因果\n\n"
            + _clip(foreshadow, 2600)
            + "\n\n## 全局备忘录摘录\n\n"
            + _clip(memo, 1800)
            + "\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(path)

    return written
