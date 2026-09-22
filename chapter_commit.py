# -*- coding: utf-8 -*-
"""Crash-recovery journal for committing one official chapter.

The chapter text, canon ledger, evidence ledger, fact database, commercial
review, and publish copy live in separate files.  This small write-ahead
journal makes those writes replayable after a power loss or process crash.
It deliberately contains only already-reviewed data and never calls a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import chapter_validator
import commercial_reviewer
import narrative_guard

SCHEMA_VERSION = 3
JOURNAL_FILENAME = "pending_chapter_commit.json"


class ChapterCommitError(RuntimeError):
    pass


def _verify_commercial_review(
    result: dict[str, Any] | None,
    *,
    chapter: int,
    chapter_digest: str,
    required: bool,
    hard_gate: bool,
    expected_chapters: list[int] | None,
    model_name: str,
) -> None:
    required = bool(required or hard_gate)
    if required and not result:
        raise ChapterCommitError(
            f"第{int(chapter)}章缺少必须执行的商业审稿，拒绝正式提交"
        )
    if not result:
        return
    receipt_model = str(
        model_name
        or ((result.get("review_receipt") or {}).get("model") or "")
    ).strip()
    try:
        commercial_reviewer.verify_review_receipt(
            result,
            model_name=receipt_model,
        )
    except Exception as exc:
        raise ChapterCommitError(
            f"第{int(chapter)}章商业审稿调用收据无效：{exc}"
        ) from exc
    reviewed = [int(item) for item in (result.get("reviewed_chapters") or [])]
    if int(chapter) not in reviewed:
        raise ChapterCommitError("商业审稿没有覆盖当前候选章")
    hashes = {
        str(key): str(value)
        for key, value in dict(result.get("chapter_hashes") or {}).items()
    }
    if hashes.get(str(int(chapter))) != str(chapter_digest):
        raise ChapterCommitError("商业审稿与当前候选正文哈希不一致")
    expected = [int(item) for item in (expected_chapters or [])]
    if expected and reviewed != expected:
        raise ChapterCommitError("商业审稿未完整覆盖本次闸门区间")
    if hard_gate and (
        result.get("malformed")
        or result.get("gate_blocked")
        or str(result.get("status") or "").upper() != "PASS"
        or str(result.get("current") or "").upper() != "PASS"
        or str(result.get("action") or "").upper() != "CONTINUE"
    ):
        raise ChapterCommitError("商业硬闸门没有取得明确 PASS/CONTINUE")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def chapter_sha256(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def journal_path(plot_dir: str | Path) -> Path:
    return Path(plot_dir) / "runtime" / JOURNAL_FILENAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load(plot_dir: str | Path) -> dict[str, Any]:
    path = journal_path(plot_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChapterCommitError(f"章节提交日志损坏：{exc}") from exc
    if not isinstance(payload, dict):
        raise ChapterCommitError("章节提交日志版本无效")
    if int(payload.get("schema_version") or 0) == 2:
        steps = payload.get("steps") or {}
        chapter_path = str(payload.get("chapter_path") or "")
        safe_unwritten = not bool(steps.get("chapter_saved"))
        if safe_unwritten and chapter_path and os.path.exists(chapter_path):
            previous_hash = str(payload.get("previous_chapter_sha256") or "")
            safe_unwritten = bool(payload.get("replacement") and previous_hash)
            if safe_unwritten:
                try:
                    current_text = Path(chapter_path).read_text(encoding="utf-8-sig")
                    safe_unwritten = chapter_sha256(current_text) == previous_hash
                except OSError:
                    safe_unwritten = False
        if safe_unwritten:
            archive = path.with_name(
                "pending_chapter_commit_v2_unwritten_"
                + datetime.now().strftime("%Y%m%d_%H%M%S")
                + ".json"
            )
            os.replace(path, archive)
            return {}
        raise ChapterCommitError(
            "检测到旧版v2未完成提交；因其缺少新审稿证据绑定，"
            "已禁止自动补成正式稿。请从该章重新执行完整审查。"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ChapterCommitError("章节提交日志版本无效")
    steps = payload.setdefault("steps", {})
    replacement = bool(payload.get("replacement"))
    payload.setdefault("narrative_audit_required", True)
    steps.setdefault("state_superseded", not replacement)
    steps.setdefault("canon_restored", not replacement)
    steps.setdefault("narrative_audit_superseded", not replacement)
    steps.setdefault("derived_memory_rebuilt", False)
    steps.setdefault(
        "narrative_audit_persisted",
        not bool(payload.get("narrative_audit_result")),
    )
    return payload


def begin(
    plot_dir: str | Path,
    *,
    chapter: int,
    chapter_path: str,
    chapter_text: str,
    chapter_outline: str = "",
    prepared_state_delta: dict[str, Any] | None = None,
    narrative_audit_result: dict[str, Any] | None = None,
    narrative_audit_required: bool = True,
    commercial_review_result: dict[str, Any] | None = None,
    commercial_review_required: bool = False,
    commercial_hard_gate: bool = False,
    commercial_expected_chapters: list[int] | None = None,
    commercial_review_model: str = "",
    validation_receipt: dict[str, Any] | None = None,
    char_limits: dict[str, Any] | None = None,
    chapter_status: str = "正式可用",
    chapter_review_notes: list[str] | None = None,
    replacement: bool = False,
    previous_chapter_sha256: str = "",
) -> dict[str, Any]:
    """Durably record everything needed to finish a chapter without an LLM."""
    chapter = int(chapter)
    digest = chapter_sha256(chapter_text)
    if str(chapter_status or "") != "正式可用":
        raise ChapterCommitError(
            f"第{chapter}章状态为{chapter_status or '未知'}，只有正式可用稿才能建立正式提交"
        )
    validation_report = chapter_validator.verify_validation_receipt(
        validation_receipt or {},
        plot_dir,
        chapter,
        chapter_text,
        char_limits or {},
    )
    if not validation_report.get("passed"):
        detail = "；".join(
            str(item.get("message") or item.get("code") or "")
            for item in (validation_report.get("issues") or [])[:8]
        )
        raise ChapterCommitError(
            f"第{chapter}章最终正文验收收据无效：{detail}"
        )
    narrative_audit_required = bool(narrative_audit_required)
    if narrative_audit_required and not narrative_audit_result:
        raise ChapterCommitError(
            f"第{chapter}章缺少逐章主线与可读性 PASS 证据，拒绝建立正式提交"
        )
    if narrative_audit_result:
        if str(narrative_audit_result.get("verdict") or "").upper() != "PASS":
            raise ChapterCommitError(f"第{chapter}章叙事审计结论不是 PASS")
        if str(narrative_audit_result.get("chapter_sha256") or "") != digest:
            raise ChapterCommitError(f"第{chapter}章叙事审计与候选正文哈希不一致")
        try:
            narrative_guard.verify_review_receipt(
                narrative_audit_result,
                chapter_text,
                expected_review_model=str(
                    narrative_audit_result.get("review_model") or ""
                ),
            )
        except Exception as exc:
            raise ChapterCommitError(f"第{chapter}章叙事审计调用收据无效：{exc}") from exc
    _verify_commercial_review(
        commercial_review_result,
        chapter=chapter,
        chapter_digest=digest,
        required=bool(commercial_review_required),
        hard_gate=bool(commercial_hard_gate),
        expected_chapters=commercial_expected_chapters,
        model_name=commercial_review_model,
    )
    normalized_chapter_path = os.path.abspath(chapter_path)
    existing = load(plot_dir)
    if existing:
        if (
            int(existing.get("chapter") or 0) == chapter
            and existing.get("chapter_sha256") == digest
            and os.path.normcase(str(existing.get("chapter_path") or ""))
            == os.path.normcase(normalized_chapter_path)
        ):
            return existing
        raise ChapterCommitError(
            f"第{existing.get('chapter')}章仍有未完成提交，拒绝开始第{chapter}章"
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chapter": chapter,
        "plot_dir": os.path.abspath(str(plot_dir)),
        "chapter_path": normalized_chapter_path,
        "chapter_sha256": digest,
        "chapter_outline": chapter_outline or "",
        "prepared_state_delta": prepared_state_delta,
        "narrative_audit_result": narrative_audit_result,
        "narrative_audit_required": narrative_audit_required,
        "commercial_review_result": commercial_review_result,
        "commercial_review_required": bool(commercial_review_required),
        "commercial_hard_gate": bool(commercial_hard_gate),
        "commercial_expected_chapters": [
            int(item) for item in (commercial_expected_chapters or [])
        ],
        "commercial_review_model": str(commercial_review_model or ""),
        "validation_receipt": validation_receipt,
        "char_limits": dict(char_limits or {}),
        "chapter_status": chapter_status or "正式可用",
        "chapter_review_notes": list(chapter_review_notes or []),
        "replacement": bool(replacement),
        "previous_chapter_sha256": str(previous_chapter_sha256 or ""),
        "steps": {
            "chapter_saved": False,
            "state_superseded": not bool(replacement),
            "canon_restored": not bool(replacement),
            "narrative_audit_superseded": not bool(replacement),
            "canon_updated": False,
            "state_committed": False,
            "derived_memory_rebuilt": False,
            "facts_updated": False,
            "status_recorded": False,
            "narrative_audit_persisted": not bool(narrative_audit_result),
            "commercial_review_persisted": not bool(commercial_review_result),
            "publish_resolved": False,
        },
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _atomic_write_json(journal_path(plot_dir), payload)
    return payload


def mark_step(plot_dir: str | Path, step: str, **updates: Any) -> dict[str, Any]:
    payload = load(plot_dir)
    if not payload:
        raise ChapterCommitError("没有待完成的章节提交日志")
    steps = payload.setdefault("steps", {})
    if step not in steps:
        raise ChapterCommitError(f"未知章节提交步骤：{step}")
    steps[step] = True
    payload.update(updates)
    payload["updated_at"] = _now_iso()
    _atomic_write_json(journal_path(plot_dir), payload)
    return payload


def verify_saved_chapter(
    payload: dict[str, Any],
    *,
    chapter_path_override: str = "",
    plot_dir_override: str = "",
) -> str:
    path = str(chapter_path_override or payload.get("chapter_path") or "")
    plot_dir = str(plot_dir_override or payload.get("plot_dir") or "")
    if payload.get("paths_relative_to_project") and not (
        chapter_path_override and plot_dir_override
    ):
        raise ChapterCommitError("归档提交收据需要由当前项目路径重新定位后复核")
    if not path or not os.path.isfile(path):
        raise ChapterCommitError("待恢复的正式章节文件不存在")
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ChapterCommitError(f"无法读取待恢复章节：{exc}") from exc
    if chapter_sha256(text) != str(payload.get("chapter_sha256") or ""):
        raise ChapterCommitError("正式章节正文与提交日志哈希不一致，拒绝自动恢复")
    validation = chapter_validator.verify_validation_receipt(
        payload.get("validation_receipt") or {},
        plot_dir,
        int(payload.get("chapter") or 0),
        text,
        payload.get("char_limits") or {},
    )
    if not validation.get("passed"):
        detail = "；".join(
            str(item.get("message") or item.get("code") or "")
            for item in (validation.get("issues") or [])[:8]
        )
        raise ChapterCommitError("正式章节确定性验收收据失效：" + detail)
    narrative_required = bool(payload.get("narrative_audit_required", True))
    narrative_result = payload.get("narrative_audit_result") or {}
    if narrative_required or narrative_result:
        try:
            narrative_guard.verify_review_receipt(
                narrative_result,
                text,
                expected_review_model=str(
                    narrative_result.get("review_model") or ""
                ),
            )
        except Exception as exc:
            raise ChapterCommitError(f"正式章节叙事审计收据失效：{exc}") from exc
    _verify_commercial_review(
        payload.get("commercial_review_result"),
        chapter=int(payload.get("chapter") or 0),
        chapter_digest=str(payload.get("chapter_sha256") or ""),
        required=bool(payload.get("commercial_review_required")),
        hard_gate=bool(payload.get("commercial_hard_gate")),
        expected_chapters=payload.get("commercial_expected_chapters") or [],
        model_name=str(payload.get("commercial_review_model") or ""),
    )
    return text


def discard_unwritten(plot_dir: str | Path) -> bool:
    """Drop a prepared-only journal when no official chapter was ever written."""
    payload = load(plot_dir)
    if not payload:
        return False
    if (payload.get("steps") or {}).get("chapter_saved"):
        raise ChapterCommitError("章节已标记保存，不能当作未写入事务丢弃")
    chapter_path = str(payload.get("chapter_path") or "")
    if chapter_path and os.path.exists(chapter_path):
        previous_hash = str(payload.get("previous_chapter_sha256") or "")
        if not (payload.get("replacement") and previous_hash):
            raise ChapterCommitError("正式章节文件已经存在，不能丢弃提交日志")
        try:
            previous_text = Path(chapter_path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ChapterCommitError(f"无法核验原正式章节：{exc}") from exc
        if chapter_sha256(previous_text) != previous_hash:
            raise ChapterCommitError("正式章节既不是原稿也不是待提交新稿，不能丢弃日志")
    journal_path(plot_dir).unlink(missing_ok=True)
    return True


def finalize(plot_dir: str | Path) -> str:
    payload = load(plot_dir)
    if not payload:
        return ""
    incomplete = [
        name for name, done in (payload.get("steps") or {}).items() if not done
    ]
    if incomplete:
        raise ChapterCommitError("章节提交仍未完成：" + "、".join(incomplete))
    source = journal_path(plot_dir)
    receipt_dir = source.parent / "commit_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / (
        f"chapter_{int(payload.get('chapter') or 0):04d}_"
        f"{str(payload.get('chapter_sha256') or '')[:12]}.json"
    )
    archived_payload = dict(payload)
    project_root = Path(plot_dir).resolve().parent
    try:
        archived_payload["chapter_path"] = os.path.relpath(
            str(payload.get("chapter_path") or ""),
            str(project_root),
        )
        archived_payload["plot_dir"] = os.path.relpath(
            str(plot_dir),
            str(project_root),
        )
        archived_payload["paths_relative_to_project"] = True
    except (OSError, ValueError):
        archived_payload["paths_relative_to_project"] = False
    archived_payload["completed_at"] = _now_iso()
    _atomic_write_json(receipt, archived_payload)
    source.unlink(missing_ok=True)
    return str(receipt)
