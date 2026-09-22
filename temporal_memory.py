"""Local SQLite temporal memory built from validated story-state deltas.

The database is a disposable search index, never the source of truth.  It can be
rebuilt at any time from ``plot/state_deltas/chapter_XXXX.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DB_FILENAME = "story_memory.db"


class TemporalMemoryError(RuntimeError):
    pass


def database_path(plot_dir: str | Path) -> Path:
    return Path(plot_dir) / DB_FILENAME


def _connect(plot_dir: str | Path) -> sqlite3.Connection:
    path = database_path(plot_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=20000")
        connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chapters (
            chapter INTEGER PRIMARY KEY,
            chapter_sha256 TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_items (
            item_id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            category TEXT NOT NULL,
            entity TEXT NOT NULL DEFAULT '',
            text_value TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            evidence_quote TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(chapter) REFERENCES chapters(chapter) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_memory_items_chapter
            ON memory_items(chapter DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_items_category
            ON memory_items(category, chapter DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_items_entity
            ON memory_items(entity, chapter DESC);
        """
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def _compact(value: Any, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _item_id(chapter: int, category: str, entity: str, text: str, evidence: str) -> str:
    raw = f"{chapter}|{category}|{entity}|{text}|{evidence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows_from_delta(delta: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(category: str, entity: Any, text: Any, status: Any, evidence: Any, payload: dict[str, Any]):
        clean_text = _compact(text, 300)
        clean_evidence = _compact(evidence, 360)
        if not clean_text or not clean_evidence:
            return
        rows.append(
            {
                "category": category,
                "entity": _compact(entity, 100),
                "text": clean_text,
                "status": _compact(status, 60),
                "evidence": clean_evidence,
                "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        )

    for item in delta.get("characters", []):
        name = item.get("name", "")
        for field, label in (
            ("location", "位置"),
            ("condition", "身体状态"),
            ("emotion", "情绪"),
            ("status", "角色状态"),
        ):
            if item.get(field):
                add("character_state", name, f"{label}：{item[field]}", item.get("status"), item.get("evidence_quote"), item)
        for knowledge in item.get("knowledge_add", []):
            add("character_knowledge", name, f"新获知：{knowledge}", "known", item.get("evidence_quote"), item)
        for ability in item.get("abilities_add", []):
            add("character_ability", name, f"获得或明确展现能力：{ability}", "available", item.get("evidence_quote"), item)

    for item in delta.get("resources", []):
        entity = f"{item.get('owner', '')}::{item.get('item', '')}"
        quantity = item.get("quantity")
        change = item.get("quantity_change")
        quantity_text = f"；数量={quantity}" if quantity is not None else ""
        change_text = f"；变动={change}" if change is not None else ""
        add(
            "resource",
            entity,
            f"{item.get('owner')}对「{item.get('item')}」执行{item.get('action')}{quantity_text}{change_text}",
            item.get("status"),
            item.get("evidence_quote"),
            item,
        )

    for item in delta.get("relationships", []):
        entity = f"{item.get('a', '')}|{item.get('b', '')}"
        add(
            "relationship",
            entity,
            f"{item.get('a')}与{item.get('b')}：{item.get('type') or '关系'}；{item.get('status') or '状态变化'}",
            item.get("status"),
            item.get("evidence_quote"),
            item,
        )

    for key, category in (("hooks", "hook"), ("subplots", "subplot")):
        for item in delta.get(key, []):
            add(
                category,
                item.get("id") or item.get("label"),
                f"{item.get('action')}：{item.get('label')}",
                item.get("action"),
                item.get("evidence_quote"),
                item,
            )

    for item in delta.get("events", []):
        add(
            "event",
            item.get("event_type"),
            item.get("summary"),
            item.get("event_type"),
            item.get("evidence_quote"),
            item,
        )
    return rows


def _sync_delta_with_connection(
    connection: sqlite3.Connection,
    delta: dict[str, Any],
    chapter_sha256: str,
) -> bool:
    chapter = int(delta.get("chapter", 0))
    if chapter <= 0 or len(str(chapter_sha256)) != 64:
        raise TemporalMemoryError("时序记忆增量缺少有效章节号或正文哈希")
    existing = connection.execute(
        "SELECT chapter_sha256 FROM chapters WHERE chapter = ?", (chapter,)
    ).fetchone()
    if existing:
        if existing["chapter_sha256"] != chapter_sha256:
            raise TemporalMemoryError(f"第{chapter}章时序记忆已对应其他正文哈希")
        return False

    with connection:
        connection.execute(
            "INSERT INTO chapters(chapter, chapter_sha256, indexed_at) VALUES(?, ?, ?)",
            (chapter, chapter_sha256, datetime.now().isoformat(timespec="seconds")),
        )
        for row in _rows_from_delta(delta):
            item_id = _item_id(
                chapter, row["category"], row["entity"], row["text"], row["evidence"]
            )
            connection.execute(
                """
                INSERT INTO memory_items(
                    item_id, chapter, category, entity, text_value,
                    status, evidence_quote, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    chapter,
                    row["category"],
                    row["entity"],
                    row["text"],
                    row["status"],
                    row["evidence"],
                    row["payload"],
                ),
            )
    return True


def sync_validated_delta(
    plot_dir: str | Path,
    delta: dict[str, Any],
    chapter_sha256: str,
) -> bool:
    connection = _connect(plot_dir)
    try:
        return _sync_delta_with_connection(connection, delta, chapter_sha256)
    finally:
        connection.close()


def _delta_paths(plot_dir: Path) -> list[Path]:
    return sorted((plot_dir / "state_deltas").glob("chapter_*.json"))


def _rebuild_database(plot_dir: Path, archive_label: str = "corrupt") -> int:
    path = database_path(plot_dir)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            backup = candidate.with_name(
                candidate.name + f".{archive_label}-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
            )
            os.replace(candidate, backup)
    connection = _connect(plot_dir)
    count = 0
    try:
        for delta_path in _delta_paths(plot_dir):
            wrapper = json.loads(delta_path.read_text(encoding="utf-8"))
            if _sync_delta_with_connection(
                connection, wrapper["delta"], str(wrapper["chapter_sha256"])
            ):
                count += 1
    finally:
        connection.close()
    return count


def rebuild_from_deltas(plot_dir: str | Path) -> int:
    """Intentionally rebuild the disposable index after a last-chapter amendment."""
    return _rebuild_database(Path(plot_dir), archive_label="superseded")


def ensure_synced(plot_dir: str | Path) -> int:
    plot_path = Path(plot_dir)
    try:
        connection = _connect(plot_path)
        count = 0
        try:
            for delta_path in _delta_paths(plot_path):
                wrapper = json.loads(delta_path.read_text(encoding="utf-8"))
                if _sync_delta_with_connection(
                    connection, wrapper["delta"], str(wrapper["chapter_sha256"])
                ):
                    count += 1
        finally:
            connection.close()
        return count
    except (sqlite3.DatabaseError, json.JSONDecodeError, KeyError, ValueError) as exc:
        try:
            return _rebuild_database(plot_path)
        except Exception as rebuild_exc:
            raise TemporalMemoryError(
                f"时序记忆库损坏且自动重建失败：{rebuild_exc}"
            ) from exc


def _normalized(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def _ngrams(value: str, size: int = 2) -> set[str]:
    if len(value) <= size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def retrieve(
    plot_dir: str | Path,
    query: str,
    before_chapter: int,
    max_hits: int = 24,
    max_chars: int = 2800,
) -> list[dict[str, Any]]:
    ensure_synced(plot_dir)
    query_norm = _normalized(query)
    if not query_norm or int(before_chapter) <= 1:
        return []
    query_grams = _ngrams(query_norm)
    connection = _connect(plot_dir)
    try:
        rows = connection.execute(
            """
            SELECT item_id, chapter, category, entity, text_value, status, evidence_quote
            FROM memory_items
            WHERE chapter < ?
            ORDER BY chapter DESC
            LIMIT 8000
            """,
            (int(before_chapter),),
        ).fetchall()
    finally:
        connection.close()

    scored = []
    for row in rows:
        entity_norm = _normalized(row["entity"])
        text_norm = _normalized(row["text_value"])
        evidence_norm = _normalized(row["evidence_quote"])
        row_grams = _ngrams(entity_norm + text_norm)
        overlap = len(query_grams & row_grams) / max(1, len(query_grams))
        entity_hit = bool(entity_norm and (entity_norm in query_norm or query_norm in entity_norm))
        text_hit = bool(text_norm and text_norm[: min(10, len(text_norm))] in query_norm)
        category_bonus = 2.5 if row["category"] in {"hook", "subplot", "character_knowledge"} else 0.0
        recency = 1.5 / (1.0 + math.log1p(max(0, before_chapter - int(row["chapter"]))))
        score = overlap * 20.0 + (14.0 if entity_hit else 0.0) + (5.0 if text_hit else 0.0) + category_bonus + recency
        # Very old facts survive only when they actually overlap the current
        # outline/entity query; recent unrelated noise is not injected.
        if score < 2.2 or (not entity_hit and overlap < 0.035):
            continue
        scored.append((score, row, evidence_norm))
    scored.sort(key=lambda item: (-item[0], -int(item[1]["chapter"]), item[1]["item_id"]))

    results = []
    used_chars = 0
    seen = set()
    for score, row, _evidence_norm in scored:
        dedupe = (_normalized(row["entity"]), _normalized(row["text_value"]))
        if dedupe in seen:
            continue
        item = {
            "item_id": row["item_id"],
            "chapter": int(row["chapter"]),
            "category": row["category"],
            "entity": row["entity"],
            "text": row["text_value"],
            "status": row["status"],
            "evidence_quote": row["evidence_quote"],
            "score": round(score, 4),
        }
        estimated = len(item["text"]) + len(item["evidence_quote"]) + 50
        if results and used_chars + estimated > max_chars:
            break
        results.append(item)
        seen.add(dedupe)
        used_chars += estimated
        if len(results) >= max(1, int(max_hits)):
            break
    return results


def render_retrieval(items: list[dict[str, Any]], max_chars: int = 2800) -> str:
    if not items:
        return ""
    lines = [
        "【按本章相关性召回的历史时序记忆】",
        "以下内容只证明相应章节当时发生过什么；若与当前状态不同，以结构化长期正史账本为准。",
    ]
    for item in items:
        entity = f"｜{item.get('entity')}" if item.get("entity") else ""
        lines.append(
            f"- 第{item.get('chapter')}章｜{item.get('category')}{entity}：{item.get('text')}"
            f"｜原文证据：{item.get('evidence_quote')}"
        )
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 16].rstrip() + "\n【召回已截断】"


def audit(plot_dir: str | Path, expected_chapter: int) -> dict[str, Any]:
    try:
        ensure_synced(plot_dir)
        connection = _connect(plot_dir)
        try:
            row = connection.execute("SELECT MAX(chapter) AS latest FROM chapters").fetchone()
            latest = int(row["latest"] or 0)
            count = int(connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0])
        finally:
            connection.close()
    except Exception as exc:
        return {"status": "FAIL", "message": str(exc), "chapter": -1, "items": 0}
    if latest != int(expected_chapter):
        return {
            "status": "FAIL",
            "message": f"时序记忆索引第{latest}章 / 正史第{expected_chapter}章",
            "chapter": latest,
            "items": count,
        }
    return {
        "status": "PASS",
        "message": f"SQLite 时序记忆已同步到第{latest}章，共{count}条",
        "chapter": latest,
        "items": count,
    }
