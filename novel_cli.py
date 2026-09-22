# -*- coding: utf-8 -*-
"""Command-line entrypoint for status inspection and bounded headless runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHAPTER_FILE_RE = re.compile(r"^第(\d{1,6})章\.txt$")
STATE_DELTA_FILE_RE = re.compile(r"^chapter_(\d{1,6})\.json$")
CANON_SNAPSHOT_FILE_RE = re.compile(r"^chapter_(\d{1,6})\.json$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chapter_text_sha256(path: Path) -> str:
    """Match the formal chapter-status ledger and validator's text-hash convention."""
    text = path.read_text(encoding="utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "顶层必须是对象"
    return value, ""


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_summary(items: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def _item(name: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, **extra}


def _commercial_review_due(chapter: int, config: dict[str, Any]) -> bool:
    """Mirror the lightweight commercial checkpoint schedule without importing the GUI."""

    def int_set(values: Any, fallback: tuple[int, ...] = ()) -> set[int]:
        result: set[int] = set()
        for value in values or fallback:
            number = _safe_int(value)
            if number and number > 0:
                result.add(number)
        return result

    number = int(chapter)
    if number in int_set(config.get("commercial_review_milestones"), (3, 10, 30)):
        return True
    hard_gates = int_set(config.get("commercial_hard_gate_chapters"), (3, 30))
    if config.get("golden_three_require_explicit_continue", True):
        hard_gates.add(max(1, int(_safe_int(config.get("golden_three_gate_chapter")) or 3)))
    volume_ends = {
        end
        for item in (config.get("volume_ranges") or [])
        if isinstance(item, (list, tuple)) and len(item) >= 2
        for end in [_safe_int(item[1])]
        if end and end > 0
    }
    if config.get("commercial_review_volume_end", True):
        hard_gates.update(volume_ends)
    if number in hard_gates:
        return True
    interval = _safe_int(config.get("commercial_review_interval")) or 0
    return bool(interval > 0 and number % interval == 0)


def _has_current_formal_commit_receipt(
    project: Path,
    chapter: int,
    expected_hash: str,
) -> bool:
    receipt_dir = project / "plot" / "runtime" / "commit_receipts"
    required_steps = (
        "chapter_saved",
        "state_committed",
        "status_recorded",
        "narrative_audit_persisted",
        "publish_resolved",
    )
    for path in sorted(receipt_dir.glob(f"chapter_{int(chapter):04d}_*.json"), reverse=True):
        payload, error = _load_json(path)
        if payload is None or error:
            continue
        steps = dict(payload.get("steps") or {})
        narrative = dict(payload.get("narrative_audit_result") or {})
        validation = dict(payload.get("validation_receipt") or {})
        if (
            _safe_int(payload.get("chapter")) == int(chapter)
            and str(payload.get("chapter_sha256") or "") == expected_hash
            and str(payload.get("chapter_status") or "") == "正式可用"
            and bool(payload.get("completed_at"))
            and all(steps.get(step) is True for step in required_steps)
            and str(narrative.get("verdict") or "").upper() == "PASS"
            and str(narrative.get("chapter_sha256") or "") == expected_hash
            and validation.get("passed") is True
            and str(validation.get("chapter_sha256") or "") == expected_hash
            and not payload.get("commercial_review_required")
        ):
            return True
    return False


def _historical_commercial_hold_receipt(
    project: Path, chapter: int, formal_paths: dict[int, Path], before_chapter: int,
) -> str | None:
    """Use a completed current-body receipt, never an unbound old report."""
    import commercial_reviewer

    expected_hash = _chapter_text_sha256(formal_paths[chapter])
    receipt_dir = project / "plot" / "runtime" / "commit_receipts"
    for path in sorted(receipt_dir.glob("chapter_*.json"), reverse=True):
        payload, error = _load_json(path)
        if error or not isinstance(payload, dict):
            continue
        source_chapter = _safe_int(payload.get("chapter"))
        if (source_chapter is None or not chapter <= source_chapter < before_chapter
                or source_chapter not in formal_paths):
            continue
        try:
            source_hash = _chapter_text_sha256(formal_paths[source_chapter])
            steps = payload.get("steps") or {}
            review = payload.get("commercial_review_result") or {}
            if (payload.get("chapter_sha256") != source_hash
                    or payload.get("chapter_status") != "正式可用"
                    or not payload.get("completed_at")
                    or not all(steps.get(key) is True for key in (
                        "chapter_saved", "state_committed", "status_recorded",
                        "narrative_audit_persisted", "commercial_review_persisted",
                        "publish_resolved"))
                    or review.get("status") != "WARN"
                    or review.get("current") != "PASS"
                    or review.get("action") != "ADJUST"):
                continue
            commercial_reviewer.verify_review_receipt(
                review, model_name=payload.get("commercial_review_model") or ""
            )
            if (review.get("chapter_hashes", {}).get(str(chapter)) != expected_hash
                    or review.get("chapter_hashes", {}).get(str(source_chapter)) != source_hash):
                continue
        except (TypeError, ValueError, AttributeError, RuntimeError):
            continue
        return str(path)
    return None


def _intentional_commercial_publish_hold(
    project: Path,
    config: dict[str, Any],
    formal_paths: dict[int, Path],
    publish_paths: dict[int, Path],
    missing_publish: list[int],
    extra_publish: list[int],
) -> dict[str, Any] | None:
    """Recognize a hash-bound commercial hold without masking mirror damage."""
    if (
        str(config.get("release_mode") or "").lower() != "strict"
        or not config.get("hold_publish_until_commercial_pass", False)
        or not config.get("commercial_review_enabled", False)
        or not missing_publish
        or extra_publish
    ):
        return None
    latest_formal = max(formal_paths, default=0)
    hold_start = min(missing_publish)
    if missing_publish != list(range(hold_start, latest_formal + 1)):
        return None
    expected_published = set(range(1, hold_start))
    if set(publish_paths) != expected_published:
        return None
    if any(
        _sha256_file(formal_paths[chapter]) != _sha256_file(publish_paths[chapter])
        for chapter in sorted(publish_paths)
    ):
        return None

    review_path = project / "review_reports" / "latest_commercial_review.json"
    review, error = _load_json(review_path)
    review_chapter = _safe_int((review or {}).get("chapter"))
    if (
        review is None
        or error
        or review_chapter is None
        or review_chapter < hold_start
        or review_chapter > latest_formal
    ):
        return None
    status = str(review.get("status") or "").upper()
    current = str(review.get("current") or "").upper()
    action = str(review.get("action") or "").upper()
    if current != "PASS" or (status == "PASS" and action == "CONTINUE"):
        return None
    reviewed = {_safe_int(value) for value in (review.get("reviewed_chapters") or [])}
    reviewed_held = [chapter for chapter in missing_publish if chapter in reviewed]
    historical = [chapter for chapter in missing_publish if chapter <= review_chapter and chapter not in reviewed]
    inherited = [chapter for chapter in missing_publish if chapter > review_chapter]
    if None in reviewed or review_chapter not in reviewed:
        return None
    hashes = dict(review.get("chapter_hashes") or {})
    receipt = dict(review.get("review_receipt") or {})
    receipt_hashes = dict(receipt.get("chapter_hashes") or {})
    if (
        receipt.get("origin") != "automatic_model_review"
        or {_safe_int(value) for value in (receipt.get("reviewed_chapters") or [])} != reviewed
    ):
        return None
    for field in (
        "system_prompt_sha256",
        "user_prompt_sha256",
        "response_sha256",
        "receipt_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field) or "")):
            return None
    for chapter in reviewed_held:
        expected_hash = _chapter_text_sha256(formal_paths[chapter])
        if (
            str(hashes.get(str(chapter)) or "") != expected_hash
            or str(receipt_hashes.get(str(chapter)) or "") != expected_hash
        ):
            return None
    historical_receipts = {}
    for chapter in historical:
        path = _historical_commercial_hold_receipt(project, chapter, formal_paths, review_chapter)
        if path is None:
            return None
        historical_receipts[str(chapter)] = path
    if inherited:
        if any(_commercial_review_due(chapter, config) for chapter in inherited):
            return None
        status_rows, status_error = _latest_status_rows(project / "logs" / "chapter_status.jsonl")
        if status_error:
            return None
        for chapter in inherited:
            expected_hash = _chapter_text_sha256(formal_paths[chapter])
            row = status_rows.get(chapter) or {}
            if (
                row.get("status") != "正式可用"
                or str(row.get("chapter_sha256") or "") != expected_hash
                or not _has_current_formal_commit_receipt(project, chapter, expected_hash)
            ):
                return None
    return _item(
        "发布镜像",
        "PASS",
        (
            f"第{hold_start}-{latest_formal}章延续第{review_chapter}章商业审稿"
            f"{status}/{action}主动暂缓；第1-{hold_start - 1}章镜像一致"
        ),
        mode="commercial_hold",
        held_chapters=missing_publish,
        review_chapter=review_chapter,
        inherited_chapters=inherited,
        historical_held_chapters=historical,
        historical_hold_receipts=historical_receipts,
        review_path=str(review_path),
        review_sha256=_sha256_file(review_path),
    )


def scan_official_chapters(project_dir: str | Path) -> dict[int, Path]:
    project = Path(project_dir).resolve()
    found: dict[int, Path] = {}
    output = project / "output"
    if not output.is_dir():
        return found
    for path in output.rglob("*.txt"):
        if ".backup" in path.parts:
            continue
        match = CHAPTER_FILE_RE.fullmatch(path.name)
        if not match:
            continue
        number = int(match.group(1))
        if number in found:
            raise RuntimeError(f"正式正文存在重复章节号：第{number}章")
        found[number] = path
    return found


def _annotate_batch_state(state: dict, latest_formal_chapter: int) -> dict:
    """Keep the batch receipt intact while exposing when later formal work supersedes it."""
    annotated = dict(state or {})
    raw_status = str(annotated.get("status") or "").strip().lower()
    target_end = int(annotated.get("target_end") or 0)
    latest = int(latest_formal_chapter or 0)
    if raw_status in {"paused", "failed", "blocked", "cancelled"} and target_end > 0 and latest >= target_end:
        annotated["effective_status"] = "superseded"
        annotated["superseded_by_formal_progress"] = True
        annotated["superseded_at_chapter"] = latest
        annotated["effective_message"] = (
            f"历史批次状态为{raw_status}，但正式正文已推进至第{latest}章；"
            "该批次仅作历史记录，不代表当前正式进度。"
        )
    else:
        annotated["effective_status"] = raw_status
        annotated["superseded_by_formal_progress"] = False
    return annotated


def inspect_project(project_dir: str | Path) -> dict:
    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{project}")
    chapters = scan_official_chapters(project)
    numbers = sorted(chapters)
    latest = max(numbers, default=0)
    expected = list(range(1, latest + 1))
    missing = sorted(set(expected) - set(numbers))
    state_path = project / "logs" / "batch_state.json"
    state = {}
    if state_path.is_file():
        with state_path.open("r", encoding="utf-8-sig") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict):
            state = loaded
    state = _annotate_batch_state(state, latest)
    return {
        "project": str(project),
        "latest_formal_chapter": latest,
        "next_chapter": latest + 1,
        "formal_chapter_count": len(numbers),
        "missing_chapters": missing,
        "batch_state": state,
    }


def _scan_chapters(directory: Path) -> tuple[dict[int, Path], list[int]]:
    found: dict[int, Path] = {}
    duplicates: list[int] = []
    if not directory.is_dir():
        return found, duplicates
    for path in directory.rglob("*.txt"):
        if ".backup" in path.parts:
            continue
        match = CHAPTER_FILE_RE.fullmatch(path.name)
        if not match:
            continue
        chapter = int(match.group(1))
        if chapter in found:
            duplicates.append(chapter)
            continue
        found[chapter] = path
    return found, sorted(duplicates)


def _projection_json_item(
    name: str,
    path: Path,
    formal_latest: int,
    chapter_fields: tuple[str, ...],
    *,
    missing_status: str = "WARN",
) -> dict[str, Any]:
    """Inspect a derived JSON artifact without invoking any repair path."""
    relative = str(path)
    if not path.is_file():
        return _item(name, missing_status, "派生产物不存在", path=relative)
    payload, error = _load_json(path)
    if payload is None:
        return _item(name, "FAIL", "JSON 无法读取：" + error, path=relative)
    reported_fields = {
        field: _safe_int(payload.get(field))
        for field in chapter_fields if field in payload
    }
    chapter = next(iter(reported_fields.values()), None)
    common = {
        "path": relative,
        "sha256": _sha256_file(path),
        "reported_chapter": chapter,
    }
    # A preferred up-to-date field must not hide a stale, invalid or future
    # legacy alias. Inspection stays read-only; writers repair aliases later.
    if len(reported_fields) > 1:
        if any(value is not None and value > formal_latest
               for value in reported_fields.values()):
            return _item(name, "FAIL", "章节进度字段存在领先正式正文的值",
                         reported_fields=reported_fields, **common)
        if None in reported_fields.values() or len(set(reported_fields.values())) > 1:
            return _item(name, "WARN", "章节进度字段不一致或无法识别",
                         reported_fields=reported_fields, **common)
    if chapter is None:
        return _item(name, "WARN", "缺少可识别的章节进度字段", **common)
    if chapter > formal_latest:
        return _item(name, "FAIL", f"派生状态领先正式正文：{chapter}>{formal_latest}", **common)
    if chapter < formal_latest:
        return _item(name, "WARN", f"派生状态落后正式正文：{chapter}<{formal_latest}", **common)
    return _item(name, "PASS", f"与正式正文同步至第{formal_latest}章", **common)


def _latest_status_rows(path: Path) -> tuple[dict[int, dict[str, Any]], str]:
    """Read the append-only chapter-status ledger without importing mutating helpers."""
    rows: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return rows, "章节状态账本不存在"
    try:
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not raw_line.strip():
                continue
            value = json.loads(raw_line)
            if not isinstance(value, dict):
                return {}, f"第{line_number}行不是对象"
            chapter = _safe_int(value.get("chapter"))
            if chapter is None or chapter <= 0:
                return {}, f"第{line_number}行缺少有效章节号"
            rows[chapter] = value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    return rows, ""


def _formal_evidence_integrity_items(
    project: Path,
    formal_paths: dict[int, Path],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Verify hash-bound state and strict commit evidence after an explicit boundary."""
    if not bool(config.get("doctor_evidence_enforcement", False)):
        return []
    boundary = max(0, int(_safe_int(config.get("legacy_evidence_exempt_through_chapter")) or 0))
    enforced = [chapter for chapter in sorted(formal_paths) if chapter > boundary]
    if not enforced:
        return [
            _item(
                "正式证据链",
                "PASS",
                f"当前正式章节均位于显式封存迁移边界第{boundary}章以内",
                legacy_exempt_through=boundary,
                enforced_chapters=[],
            )
        ]

    plot = project / "plot"
    state, state_error = _load_json(plot / "story_state.json")
    applied = dict((state or {}).get("applied_chapters") or {})
    issues: list[str] = []
    affected: set[int] = set()
    for chapter in enforced:
        expected = _chapter_text_sha256(formal_paths[chapter])
        if state is None:
            issues.append("物化状态无法读取：" + state_error)
            affected.add(chapter)
            break
        if str(applied.get(str(chapter)) or "") != expected:
            issues.append(f"第{chapter}章物化状态哈希与正文不一致")
            affected.add(chapter)
        delta_path = plot / "state_deltas" / f"chapter_{chapter:04d}.json"
        delta, delta_error = _load_json(delta_path)
        if delta is None:
            issues.append(f"第{chapter}章状态增量无效：{delta_error or '不存在'}")
            affected.add(chapter)
        elif str(delta.get("chapter_sha256") or "") != expected:
            issues.append(f"第{chapter}章状态增量哈希与正文不一致")
            affected.add(chapter)

    try:
        import state_ledger

        state_ledger.verify_materialized_state(plot)
    except Exception as exc:
        issues.append(f"物化状态重放复核失败：{exc}")
        affected.update(enforced)

    release_mode = str(config.get("release_mode") or "strict").strip().lower()
    if release_mode != "strict":
        issues.append("启用正式证据强校验时 release_mode 必须为 strict")
        affected.update(enforced)
    else:
        import chapter_validator
        import narrative_guard

        receipt_dir = plot / "runtime" / "commit_receipts"
        min_chars = int(_safe_int(config.get("chapter_char_min")) or 3000)
        target_max = int(_safe_int(config.get("chapter_char_target_max")) or 4300)
        char_limits = {"min": min_chars, "target_max": target_max}
        review_model = str(config.get("review_model") or "").strip()
        narrative_min_score = int(
            _safe_int(config.get("narrative_guard_min_score")) or 78
        )
        required_steps = (
            "chapter_saved",
            "state_committed",
            "status_recorded",
            "narrative_audit_persisted",
            "publish_resolved",
        )
        for chapter in enforced:
            expected = _chapter_text_sha256(formal_paths[chapter])
            chapter_text = formal_paths[chapter].read_text(encoding="utf-8-sig")
            chinese_count = len(re.findall(r"[\u4e00-\u9fff]", chapter_text))
            if chinese_count < min_chars:
                issues.append(f"第{chapter}章汉字数不足（{chinese_count} < {min_chars}）")
                affected.add(chapter)

            matching: list[tuple[Path, dict[str, Any]]] = []
            for path in sorted(receipt_dir.glob(f"chapter_{chapter:04d}_*.json"), reverse=True):
                payload, _error = _load_json(path)
                if payload and str(payload.get("chapter_sha256") or "") == expected:
                    matching.append((path, payload))
            if not matching:
                issues.append(f"第{chapter}章缺少与当前正文绑定的正式提交收据")
                affected.add(chapter)
                continue

            receipt_errors: list[str] = []
            valid_receipt = False
            for _path, receipt in matching:
                current_errors: list[str] = []
                if type(receipt.get("schema_version")) is not int or int(
                    receipt.get("schema_version") or 0
                ) < 3:
                    current_errors.append("提交收据版本无效")
                if type(receipt.get("chapter")) is not int or receipt.get("chapter") != chapter:
                    current_errors.append("提交收据章号不匹配")
                if receipt.get("chapter_status") != "正式可用":
                    current_errors.append("提交收据未标记正式可用")
                if not str(receipt.get("completed_at") or "").strip():
                    current_errors.append("提交收据缺少完成时间")

                steps = receipt.get("steps")
                if not isinstance(steps, dict):
                    current_errors.append("提交收据步骤无效")
                else:
                    incomplete = [
                        name for name in required_steps if steps.get(name) is not True
                    ]
                    if incomplete:
                        current_errors.append("提交步骤未完成：" + ",".join(incomplete))

                validation = receipt.get("validation_receipt")
                if not isinstance(validation, dict):
                    current_errors.append("缺少确定性验收收据")
                else:
                    try:
                        validation_report = chapter_validator.verify_validation_receipt(
                            validation,
                            project,
                            chapter,
                            chapter_text,
                            char_limits,
                        )
                        if not validation_report.get("receipt_valid"):
                            current_errors.append("确定性验收收据复核失败")
                    except Exception as exc:
                        current_errors.append(f"确定性验收收据无法复核：{exc}")

                narrative = receipt.get("narrative_audit_result")
                if receipt.get("narrative_audit_required") is not True:
                    current_errors.append("提交收据未要求叙事审计")
                if not isinstance(narrative, dict):
                    current_errors.append("缺少叙事审计结果")
                else:
                    if (
                        str(narrative.get("verdict") or "").upper() != "PASS"
                        or str(narrative.get("chapter_sha256") or "") != expected
                    ):
                        current_errors.append("叙事审计结论或正文哈希无效")
                    try:
                        narrative_guard.verify_review_receipt(
                            narrative,
                            chapter_text,
                            expected_review_model=review_model,
                        )
                    except Exception as exc:
                        current_errors.append(f"叙事调用收据无法复核：{exc}")
                    try:
                        persisted = narrative_guard.verify_persisted_audit(
                            plot,
                            chapter,
                            chapter_text,
                            expected_review_model=review_model,
                            min_score=narrative_min_score,
                        )
                        if (
                            str(persisted.get("verdict") or "").upper() != "PASS"
                            or str(persisted.get("chapter_sha256") or "") != expected
                        ):
                            current_errors.append("已落盘叙事审计结论无效")
                    except Exception as exc:
                        current_errors.append(f"已落盘叙事审计无法复核：{exc}")

                if not current_errors:
                    valid_receipt = True
                    break
                receipt_errors.extend(current_errors)

            if not valid_receipt:
                detail = "、".join(list(dict.fromkeys(receipt_errors))[:4])
                issues.append(f"第{chapter}章正式提交收据无效：{detail}")
                affected.add(chapter)

    if issues:
        return [
            _item(
                "正式证据链",
                "FAIL",
                "；".join(issues[:16]),
                issue_count=len(issues),
                affected_chapters=sorted(affected),
                legacy_exempt_through=boundary,
                enforced_chapters=enforced,
            )
        ]
    return [
        _item(
            "正式证据链",
            "PASS",
            f"第{enforced[0]}-{enforced[-1]}章正文、状态增量、物化状态与提交收据哈希一致",
            legacy_exempt_through=boundary,
            enforced_chapters=enforced,
        )
    ]


def build_doctor_report(project_dir: str | Path) -> dict[str, Any]:
    """Build a strict read-only project health report.

    This deliberately does not construct ``HeadlessNovelGenerator``: its GUI
    compatibility setup can create directories, migrate config defaults, or
    recover a pending commit.  The doctor command must remain safe to run on a
    damaged project before deciding whether any recovery is allowed.
    """
    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{project}")
    formal_paths, formal_duplicates = _scan_chapters(project / "output")
    publish_paths, publish_duplicates = _scan_chapters(project / "publish")
    numbers = sorted(formal_paths)
    formal_latest = max(numbers, default=0)
    missing_chapters = sorted(set(range(1, formal_latest + 1)) - set(numbers))
    state_path = project / "logs" / "batch_state.json"
    batch_state, batch_error = _load_json(state_path) if state_path.is_file() else ({}, "")
    batch_state = _annotate_batch_state(batch_state or {}, formal_latest)
    status = {
        "project": str(project),
        "latest_formal_chapter": formal_latest,
        "next_chapter": formal_latest + 1,
        "formal_chapter_count": len(numbers),
        "missing_chapters": missing_chapters,
        "batch_state": batch_state,
    }
    checks: list[dict[str, Any]] = []

    config_path = project / "project_config.json"
    config, config_error = _load_json(config_path)
    if config is None:
        checks.append(_item("project_config.json", "FAIL", "配置无法读取：" + config_error))
    else:
        checks.append(_item("project_config.json", "PASS", "JSON 可读取", sha256=_sha256_file(config_path)))
    if batch_error:
        checks.append(_item("batch_state.json", "WARN", "批次状态无法读取：" + batch_error))

    if formal_duplicates:
        checks.append(_item("正式正文布局", "FAIL", "重复章节号：" + "、".join(map(str, formal_duplicates))))
    elif status["missing_chapters"]:
        checks.append(_item("正式正文布局", "FAIL", "缺章：" + "、".join(map(str, status["missing_chapters"]))))
    else:
        checks.append(_item("正式正文布局", "PASS", f"第1-{formal_latest}章连续" if formal_latest else "暂无正式章节"))

    if publish_duplicates:
        checks.append(_item("发布镜像布局", "FAIL", "重复章节号：" + "、".join(map(str, publish_duplicates))))
    elif set(formal_paths) != set(publish_paths):
        missing_publish = sorted(set(formal_paths) - set(publish_paths))
        extra_publish = sorted(set(publish_paths) - set(formal_paths))
        held = _intentional_commercial_publish_hold(
            project,
            config or {},
            formal_paths,
            publish_paths,
            missing_publish,
            extra_publish,
        )
        if held:
            checks.append(held)
        else:
            details = []
            if missing_publish:
                details.append("缺发布稿：" + "、".join(map(str, missing_publish[:12])))
            if extra_publish:
                details.append("多发布稿：" + "、".join(map(str, extra_publish[:12])))
            checks.append(_item("发布镜像", "FAIL", "；".join(details)))
    else:
        different = [
            chapter for chapter in sorted(formal_paths)
            if _sha256_file(formal_paths[chapter]) != _sha256_file(publish_paths[chapter])
        ]
        if different:
            checks.append(_item("发布镜像", "FAIL", "正文哈希不一致：第" + "、".join(map(str, different[:12])) + "章"))
        else:
            checks.append(_item("发布镜像", "PASS", f"{len(formal_paths)}章与正式正文一致"))

    plot = project / "plot"
    import commercial_reviewer

    debt_path = plot / "revision_debt.json"
    try:
        debt = commercial_reviewer.load_revision_debt(debt_path)
    except commercial_reviewer.RevisionDebtError as exc:
        checks.append(_item("修订债务账本", "FAIL", str(exc)))
    else:
        open_count = sum(item["status"] == "OPEN" for item in debt["items"])
        checks.append(_item(
            "修订债务账本", "PASS",
            f"结构有效，{open_count}项未结；不代表剧情债务已兑现"
            if debt_path.exists() else "尚无债务账本，首次审稿时建立",
            open_count=open_count,
        ))
    derived = [
        _projection_json_item("正史状态", plot / "canon_state.json", formal_latest, ("current_chapter",)),
        _projection_json_item("关键事实库", plot / "关键事实库.json", formal_latest, ("latest_chapter", "current_chapter")),
        _projection_json_item("物化状态账本", plot / "story_state.json", formal_latest, ("current_chapter",)),
    ]

    delta_dir = plot / "state_deltas"
    delta_numbers: list[int] = []
    delta_errors: list[str] = []
    if delta_dir.is_dir():
        for path in delta_dir.glob("chapter_*.json"):
            match = STATE_DELTA_FILE_RE.fullmatch(path.name)
            if not match:
                continue
            payload, error = _load_json(path)
            chapter = int(match.group(1))
            if payload is None:
                delta_errors.append(f"第{chapter}章：{error}")
            else:
                reported = _safe_int(payload.get("chapter"))
                if reported is not None and reported != chapter:
                    delta_errors.append(f"第{chapter}章文件与内容章节号不一致")
                delta_numbers.append(chapter)
    if delta_errors:
        derived.append(_item("状态增量链", "FAIL", "；".join(delta_errors[:6])))
    elif formal_latest and not delta_numbers:
        derived.append(_item("状态增量链", "WARN", "尚无状态增量文件"))
    else:
        delta_latest = max(delta_numbers, default=0)
        delta_status = "PASS" if delta_latest == formal_latest else "WARN"
        derived.append(_item("状态增量链", delta_status, f"已登记至第{delta_latest}章", count=len(delta_numbers), latest_chapter=delta_latest))

    snapshot_dir = plot / "canon_snapshots"
    snapshots = [
        int(match.group(1)) for path in snapshot_dir.glob("chapter_*.json")
        if (match := CANON_SNAPSHOT_FILE_RE.fullmatch(path.name))
    ] if snapshot_dir.is_dir() else []
    snapshot_latest = max(snapshots, default=0)
    canon_item = next((row for row in derived if row["name"] == "正史状态"), {})
    canon_chapter = canon_item.get("reported_chapter")
    if formal_latest and not snapshots:
        derived.append(_item("正史快照", "WARN", "尚无正史快照"))
    elif canon_chapter is not None and snapshot_latest != canon_chapter:
        derived.append(_item("正史快照", "WARN", f"快照至第{snapshot_latest}章，正史至第{canon_chapter}章", count=len(snapshots)))
    else:
        derived.append(_item("正史快照", "PASS", f"快照至第{snapshot_latest}章", count=len(snapshots)))

    status_rows, status_error = _latest_status_rows(project / "logs" / "chapter_status.jsonl")
    status_issues: list[str] = []
    status_problem_chapters: list[int] = []
    for chapter, chapter_path in sorted(formal_paths.items()):
        row = status_rows.get(chapter)
        if row is None:
            status_issues.append(f"第{chapter}章缺状态")
            status_problem_chapters.append(chapter)
            continue
        if row.get("status") != "正式可用":
            status_issues.append(f"第{chapter}章状态为{row.get('status') or '未知'}")
            status_problem_chapters.append(chapter)
        if str(row.get("chapter_sha256") or "") != _chapter_text_sha256(chapter_path):
            status_issues.append(f"第{chapter}章正文哈希不匹配")
            status_problem_chapters.append(chapter)
    if status_error:
        derived.append(_item("章节状态账本", "FAIL" if formal_paths else "WARN", status_error))
    elif status_issues:
        affected = sorted(set(status_problem_chapters))
        derived.append(_item(
            "章节状态账本",
            "FAIL",
            "；".join(status_issues[:12]),
            issue_count=len(status_issues),
            affected_chapters=affected,
        ))
    else:
        derived.append(_item("章节状态账本", "PASS", f"{len(formal_paths)}章均有当前正式状态"))

    if config is not None:
        derived.extend(_formal_evidence_integrity_items(project, formal_paths, config))

    journal = plot / "runtime" / "pending_chapter_commit.json"
    if not journal.exists():
        derived.append(_item("待完成提交", "PASS", "不存在待恢复事务"))
    else:
        pending, error = _load_json(journal)
        if pending is None:
            derived.append(_item("待完成提交", "FAIL", "事务日志无法读取：" + error))
        else:
            derived.append(_item("待完成提交", "WARN", f"检测到第{pending.get('chapter') or '未知'}章待恢复事务", path=str(journal), sha256=_sha256_file(journal)))

    derived_status = _status_summary(derived)
    overall = _status_summary(checks + derived)
    return {
        "schema_version": 1,
        "read_only": True,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_status": overall,
        "project_status": status,
        "checks": checks,
        "derived_state": {
            "overall_status": derived_status,
            "items": derived,
        },
        "limitations": [
            "未调用模型、未执行提供方登录/额度预检。",
            "未恢复待完成提交，未迁移配置，未重建任何派生状态。",
            "报告可用 stdout 重定向保存；doctor 本身不会写报告文件。",
        ],
    }


def run_bounded_batch(args) -> int:
    # Keep read-only status inspection lightweight.  The production adapter
    # imports model and NLP dependencies only after explicit write consent.
    from headless_app import HeadlessNovelGenerator

    status = inspect_project(args.project)
    if status["missing_chapters"]:
        raise RuntimeError("正式正文存在缺章，禁止继续生成")
    latest = int(status["latest_formal_chapter"])
    if args.expected_latest is not None and latest != int(args.expected_latest):
        raise RuntimeError(
            f"磁盘正式进度为第{latest}章，与 --expected-latest={args.expected_latest} 不一致"
        )
    target_end = int(args.to)
    if target_end <= latest:
        raise RuntimeError(f"目标第{target_end}章没有超过当前第{latest}章")
    if not args.execute:
        raise RuntimeError("这是写入操作；请明确添加 --execute 后再运行")

    app = HeadlessNovelGenerator(args.project, echo_progress=not args.quiet)
    if app.latest_chap != latest or app.next_chap != latest + 1:
        raise RuntimeError(
            f"运行时进度漂移：latest={app.latest_chap}, next={app.next_chap}"
        )
    count = target_end - latest
    issues = app._run_health_check_internal(future_window=count)
    failures = [desc for desc, state in issues if state == "FAIL"]
    if failures:
        raise RuntimeError("健康检查失败：" + "；".join(failures[:8]))
    if not app._start_batch_job(
        count,
        label=args.label,
        target_end=target_end,
        mode=args.mode,
    ):
        raise RuntimeError("批量入口拒绝启动")
    runner = app._book_runner
    runner.wait()
    state = app._load_batch_state()
    final = inspect_project(args.project)
    result = {"batch_state": state, "project_status": final}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if state.get("status") != "completed":
        raise RuntimeError(f"批量任务未完成：{state.get('status') or 'unknown'}")
    if int(final["latest_formal_chapter"]) != target_end:
        raise RuntimeError("批量状态与正式正文终点不一致")
    return 0


def run_micro_edit(args) -> int:
    """Validate or execute one bounded, explicitly authorized prose micro-edit."""
    import formal_suffix_rewrite

    project = Path(args.project).resolve()
    candidate_dir = Path(args.candidates_dir).resolve()
    if not candidate_dir.is_dir():
        raise RuntimeError(f"微调候选目录不存在：{candidate_dir}")
    overrides = {}
    for path in sorted(candidate_dir.glob("第*章.txt")):
        match = CHAPTER_FILE_RE.fullmatch(path.name)
        if match:
            overrides[int(match.group(1))] = path
    if not overrides:
        raise RuntimeError("候选目录中没有符合“第0001章.txt”格式的微调文件")

    status = inspect_project(project)
    start = int(args.start) if args.start is not None else min(overrides)
    end = (
        int(args.end)
        if args.end is not None
        else int(status.get("latest_formal_chapter") or 0)
    )
    if start < 1 or end < start:
        raise RuntimeError("微调章节范围无效")
    candidates = formal_suffix_rewrite.resolve_sparse_micro_edit_candidates(
        project,
        overrides,
        start_chapter=start,
        end_chapter=end,
    )
    report = formal_suffix_rewrite.validate_micro_edit_candidates(
        project,
        candidates,
        max_changed_chars=int(args.max_changed_chars),
        max_changed_ratio=float(args.max_changed_ratio),
    )
    config, _config_error = _load_json(project / "project_config.json")
    changed_count = len(report.get("changed_chapters") or [])
    hard_limit = min(
        int((config or {}).get("formal_micro_edit_model_call_limit", 40) or 40),
        max(8, changed_count * 6 + 8),
    )
    plan = {
        "range": [start, end],
        "explicit_candidates": sorted(overrides),
        "changed_chapters": report.get("changed_chapters") or [],
        "reused_chapters": report.get("unchanged_chapters") or [],
        "commercial_review_due_at_end": _commercial_review_due(end, config or {}),
        "model_call_hard_limit_per_phase": hard_limit,
    }
    if not args.execute:
        print(
            json.dumps(
                {"read_only": True, "execution_plan": plan, "micro_edit": report},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    doctor = build_doctor_report(project)
    if doctor.get("overall_status") != "PASS":
        raise RuntimeError("正式项目前置体检未通过，拒绝微调提交")
    from headless_app import HeadlessNovelGenerator

    app = HeadlessNovelGenerator(project, echo_progress=not args.quiet)
    result = app.rewrite_formal_suffix_from_drafts(
        start,
        end,
        draft_paths=overrides,
        micro_edit=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小说工具无界面入口")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="只读查看正式进度和批次状态")
    status.add_argument("--project", required=True)

    doctor = sub.add_parser(
        "doctor",
        help="严格只读检查正式正文与派生状态，可将 JSON 重定向保存",
    )
    doctor.add_argument("--project", required=True)

    run = sub.add_parser("run", help="运行有明确终点的正式批次")
    run.add_argument("--project", required=True)
    run.add_argument("--to", required=True, type=int, help="本批最后一章")
    run.add_argument("--expected-latest", type=int)
    run.add_argument("--mode", choices=("batch", "trial", "full_book"), default="batch")
    run.add_argument("--label", default="命令行批量生成")
    run.add_argument("--execute", action="store_true", help="确认允许写入正式章节")
    run.add_argument("--quiet", action="store_true")

    micro = sub.add_parser(
        "micro-edit",
        help="预检或提交正式尾段的小幅文字精修；默认只读",
    )
    micro.add_argument("--project", required=True)
    micro.add_argument("--start", type=int, help="默认取候选目录中的最小章号")
    micro.add_argument("--end", type=int, help="默认取当前正式末章")
    micro.add_argument("--candidates-dir", required=True)
    micro.add_argument("--max-changed-chars", type=int, default=800)
    micro.add_argument("--max-changed-ratio", type=float, default=0.08)
    micro.add_argument("--execute", action="store_true", help="确认允许写入正式章节")
    micro.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(inspect_project(args.project), ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            report = build_doctor_report(args.project)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2 if report["overall_status"] == "FAIL" else 0
        if args.command == "micro-edit":
            return run_micro_edit(args)
        return run_bounded_batch(args)
    except Exception as exc:
        print(f"[BLOCKED] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
