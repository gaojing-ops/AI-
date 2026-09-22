# -*- coding: utf-8 -*-
"""Fail-closed deterministic validation for an official chapter candidate.

This module is intentionally model-free.  It combines the existing narrative
and semantic guards with final-file checks that must hold immediately before a
chapter is committed or published.  Validation receipts are evidence of one
specific text/configuration pair; they are not trusted without recomputation.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

import narrative_guard
import semantic_consistency_guard


VALIDATION_VERSION = 1
RECEIPT_TYPE = "chapter_candidate_validation"

_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_TITLE_RE = re.compile(
    r"^\s*第\s*(?P<number>[0-9零〇一二三四五六七八九十百千万两]+)\s*章"
    r"(?:(?:\s+|[：:、.-])(?P<title>.*)|(?P<bare>$))"
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]+|…{2,})?")
_VISIBLE_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_MOJIBAKE_TOKENS = ("�", "锛", "銆", "鈥", "馃", "鍑", "鏂")
_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』"}
_SYMMETRIC_QUOTES = ('"', "＂")


def _normalized_text(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _semantic_config_path(project_dir: str | Path) -> Path:
    root = Path(project_dir)
    if root.name.lower() == "plot":
        return root / semantic_consistency_guard.CONFIG_FILENAME
    return root / "plot" / semantic_consistency_guard.CONFIG_FILENAME


def _semantic_file_digest(project_dir: str | Path) -> str:
    path = _semantic_config_path(project_dir)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    source: str = "chapter_validator",
    blocking: bool = True,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "blocking": bool(blocking),
        "message": str(message),
        "source": source,
        "evidence": list(evidence or []),
    }


def _normalize_limits(char_limits: object) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if not isinstance(char_limits, dict):
        return {}, [_issue("char_limits_invalid", "字数限制必须是对象", severity="critical")]
    errors: list[dict[str, Any]] = []
    try:
        minimum = int(char_limits.get("min"))
    except (TypeError, ValueError):
        minimum = 0
        errors.append(_issue("char_limits_invalid", "字数限制缺少有效的 min", severity="critical"))

    raw_maximum = char_limits.get("max")
    if raw_maximum is None:
        raw_maximum = char_limits.get("target_max")
    try:
        maximum = int(raw_maximum)
    except (TypeError, ValueError):
        maximum = 0
        errors.append(
            _issue(
                "char_limits_invalid",
                "字数限制缺少有效的 max 或 target_max",
                severity="critical",
            )
        )
    if minimum <= 0 and not any(item["code"] == "char_limits_invalid" for item in errors):
        errors.append(_issue("char_limits_invalid", "min 必须大于 0", severity="critical"))
    if maximum <= 0:
        if not errors:
            errors.append(_issue("char_limits_invalid", "最大字数必须大于 0", severity="critical"))
    elif minimum > maximum:
        errors.append(
            _issue(
                "char_limits_invalid",
                f"字数限制冲突：min={minimum} 大于 max={maximum}",
                severity="critical",
            )
        )
    return {"min": minimum, "max": maximum}, errors


def _validation_config_digest(
    project_dir: str | Path,
    limits: dict[str, int],
    expected_title_required: bool,
    semantic_digest: str | None = None,
) -> str:
    return _canonical_digest(
        {
            "semantic_config_sha256": (
                semantic_digest
                if semantic_digest is not None
                else _semantic_file_digest(project_dir)
            ),
            "char_limits": limits,
            "expected_title_required": bool(expected_title_required),
            "validation_version": VALIDATION_VERSION,
        }
    )


def _chinese_number(value: str) -> int | None:
    value = str(value or "").strip()
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if not value or any(char not in digits and char not in units for char in value):
        return None
    # Strings without a unit are ordinary digit sequences, e.g. 二〇二.
    if not any(char in units for char in value):
        result = 0
        for char in value:
            result = result * 10 + digits[char]
        return result
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units[char]
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


def _title_issues(text: str, chapter_number: int, required: bool) -> list[dict[str, Any]]:
    lines = text.splitlines()
    matches: list[tuple[int, re.Match[str]]] = []
    for line_number, line in enumerate(lines, start=1):
        match = _TITLE_RE.match(line)
        if match:
            matches.append((line_number, match))
    if not required and not matches:
        return []
    issues: list[dict[str, Any]] = []
    if not matches:
        return [_issue("chapter_title_missing", "正文缺少标准章节标题")]
    if len(matches) != 1:
        issues.append(
            _issue(
                "chapter_title_count_invalid",
                f"正文中检出 {len(matches)} 个章节标题，必须且只能有一个",
                evidence=[{"line": line_number} for line_number, _match in matches],
            )
        )
    first_nonempty = next((index for index, line in enumerate(lines, 1) if line.strip()), 0)
    title_line, title_match = matches[0]
    if title_line != first_nonempty:
        issues.append(
            _issue(
                "chapter_title_not_first",
                "章节标题必须是第一个非空行",
                evidence=[{"line": title_line, "first_nonempty_line": first_nonempty}],
            )
        )
    actual_number = _chinese_number(title_match.group("number"))
    if actual_number != chapter_number:
        issues.append(
            _issue(
                "chapter_number_mismatch",
                f"标题章号为 {title_match.group('number')}，期望第 {chapter_number} 章",
                evidence=[{"line": title_line, "actual": actual_number, "expected": chapter_number}],
            )
        )
    title = (title_match.group("title") or "").strip().lstrip("：: 、.-").strip()
    if required and not title:
        issues.append(
            _issue(
                "chapter_title_text_missing",
                "章节标题缺少章名",
                evidence=[{"line": title_line}],
            )
        )
    return issues


def _pair_issues(text: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for opening, closing in _PAIRS.items():
        stack: list[int] = []
        for offset, char in enumerate(text):
            if char == opening:
                stack.append(offset)
            elif char == closing:
                if stack:
                    stack.pop()
                else:
                    issues.append(
                        _issue(
                            "unpaired_quote",
                            f"存在没有左引号的 {closing}",
                            evidence=[{"offset": offset, "character": closing}],
                        )
                    )
        if stack:
            issues.append(
                _issue(
                    "unpaired_quote",
                    f"存在 {len(stack)} 个未闭合的 {opening}",
                    evidence=[{"offset": offset, "character": opening} for offset in stack[:10]],
                )
            )
    for quote in _SYMMETRIC_QUOTES:
        positions = [index for index, char in enumerate(text) if char == quote]
        if len(positions) % 2:
            issues.append(
                _issue(
                    "unpaired_quote",
                    f"引号 {quote} 的数量不成对",
                    evidence=[{"offset": offset, "character": quote} for offset in positions[-3:]],
                )
            )
    return issues


def _normalized_unit(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value.strip()))


def _visible_length(value: str) -> int:
    return len(_VISIBLE_RE.findall(value))


def _duplicate_issues(text: str) -> list[dict[str, Any]]:
    body = text.split("\n", 1)[1] if "\n" in text else text
    issues: list[dict[str, Any]] = []

    paragraphs: defaultdict[str, list[int]] = defaultdict(list)
    for index, raw in enumerate(re.split(r"\n[ \t\u3000]*\n+", body), start=1):
        normalized = _normalized_unit(raw)
        if _visible_length(normalized) >= 16:
            paragraphs[normalized].append(index)
    for rows in paragraphs.values():
        if len(rows) >= 2:
            issues.append(
                _issue(
                    "duplicate_paragraph",
                    f"正文存在 {len(rows)} 次完全重复的段落",
                    evidence=[{"paragraph": item} for item in rows],
                )
            )

    sentences: defaultdict[str, list[int]] = defaultdict(list)
    for index, match in enumerate(_SENTENCE_RE.finditer(body), start=1):
        normalized = _normalized_unit(match.group(0))
        if _visible_length(normalized) >= 10:
            sentences[normalized].append(index)
    for rows in sentences.values():
        if len(rows) >= 2:
            issues.append(
                _issue(
                    "duplicate_sentence",
                    f"正文存在 {len(rows)} 次完全重复的句子",
                    evidence=[{"sentence": item} for item in rows],
                )
            )
    return issues


def _deduplicate_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for issue in issues:
        key = (str(issue.get("code") or ""), str(issue.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def validate_candidate(
    project_dir: str | Path,
    chapter_number: int,
    text: str,
    char_limits: dict[str, Any],
    expected_title_required: bool = True,
) -> dict[str, Any]:
    """Validate one candidate and return a stable, structured report."""
    issues: list[dict[str, Any]] = []
    try:
        chapter_number = int(chapter_number)
    except (TypeError, ValueError):
        chapter_number = 0
    if chapter_number <= 0:
        issues.append(_issue("chapter_number_invalid", "章号必须是正整数", severity="critical"))

    limits, limit_issues = _normalize_limits(char_limits)
    issues.extend(limit_issues)
    normalized = _normalized_text(text if isinstance(text, str) else "")
    chapter_digest = _sha256_text(normalized)
    semantic_digest_before = _semantic_file_digest(project_dir)

    if not normalized.strip():
        issues.append(_issue("chapter_empty", "正文为空", severity="critical"))
    else:
        issues.extend(_title_issues(normalized, chapter_number, bool(expected_title_required)))
        chinese_chars = len(_CHINESE_CHAR_RE.findall(normalized))
        if limits and not limit_issues:
            if chinese_chars < limits["min"]:
                issues.append(
                    _issue(
                        "chapter_too_short",
                        f"正文中文字符数 {chinese_chars} 低于硬下限 {limits['min']}",
                        evidence=[{"actual": chinese_chars, "minimum": limits["min"]}],
                    )
                )
            if chinese_chars > limits["max"]:
                issues.append(
                    _issue(
                        "chapter_too_long",
                        f"正文中文字符数 {chinese_chars} 高于硬上限 {limits['max']}",
                        evidence=[{"actual": chinese_chars, "maximum": limits["max"]}],
                    )
                )

        for message in narrative_guard.deterministic_issues(normalized):
            issues.append(
                _issue(
                    "narrative_deterministic_issue",
                    message,
                    source="narrative_guard",
                )
            )

        semantic_report = semantic_consistency_guard.scan_chapter(
            project_dir,
            normalized,
            chapter_number,
        )
        for semantic_issue in semantic_report.get("issues") or []:
            copied = dict(semantic_issue)
            copied["source"] = "semantic_consistency_guard"
            copied.setdefault("blocking", True)
            copied.setdefault("severity", "error")
            copied.setdefault("message", copied.get("code") or "语义一致性检查未通过")
            copied.setdefault("evidence", [])
            issues.append(copied)
        if not semantic_report.get("passed"):
            # A malformed report must not accidentally become non-blocking just
            # because its issues list is empty.
            if not semantic_report.get("issues"):
                issues.append(
                    _issue(
                        "semantic_guard_not_passed",
                        f"语义守卫状态为 {semantic_report.get('status') or 'unknown'}",
                        severity="critical",
                        source="semantic_consistency_guard",
                    )
                )

        issues.extend(_pair_issues(normalized))
        if any(normalized.count(token) for token in _MOJIBAKE_TOKENS):
            issues.append(_issue("mojibake_detected", "正文疑似存在乱码或替换字符"))
        issues.extend(_duplicate_issues(normalized))

    semantic_digest_after = _semantic_file_digest(project_dir)
    if semantic_digest_before != semantic_digest_after:
        issues.append(
            _issue(
                "validation_config_changed",
                "验收期间语义配置发生变化，已阻断",
                severity="critical",
            )
        )
    issues = _deduplicate_issues(issues)
    blocking = [item for item in issues if item.get("blocking", True)]
    passed = not blocking
    invalid_config_codes = {
        "char_limits_invalid",
        "semantic_config_missing",
        "semantic_config_invalid",
        "semantic_guard_not_passed",
        "validation_config_changed",
        "chapter_number_invalid",
    }
    status = "pass" if passed else (
        "blocked" if any(item.get("code") in invalid_config_codes for item in blocking) else "fail"
    )
    chinese_chars = len(_CHINESE_CHAR_RE.findall(normalized))
    config_digest = _validation_config_digest(
        project_dir,
        limits,
        bool(expected_title_required),
        semantic_digest_after,
    )
    return {
        "passed": passed,
        "ok": passed,
        "status": status,
        "chapter": chapter_number,
        "chinese_chars": chinese_chars,
        "char_limits": limits,
        "expected_title_required": bool(expected_title_required),
        "issues": issues,
        "summary": {
            "total": len(issues),
            "blocking": len(blocking),
        },
        "chapter_sha256": chapter_digest,
        "semantic_config_sha256": semantic_digest_after,
        "config_digest": config_digest,
        "validation_version": VALIDATION_VERSION,
    }


def build_validation_receipt(report: dict[str, Any]) -> dict[str, Any]:
    """Build a portable receipt from a successful validation report."""
    if not isinstance(report, dict) or not report.get("passed"):
        raise ValueError("只能为已通过的候选正文生成验收收据")
    required = ("chapter", "chapter_sha256", "config_digest", "validation_version")
    if any(report.get(key) in (None, "") for key in required):
        raise ValueError("验收报告缺少生成收据所需字段")
    material = {
        "receipt_type": RECEIPT_TYPE,
        "validation_version": int(report["validation_version"]),
        "chapter": int(report["chapter"]),
        "chapter_sha256": str(report["chapter_sha256"]),
        "config_digest": str(report["config_digest"]),
        "semantic_config_sha256": str(report.get("semantic_config_sha256") or ""),
        "chinese_chars": int(report.get("chinese_chars") or 0),
        "passed": True,
        "status": "pass",
    }
    material["receipt_sha256"] = _canonical_digest(material)
    return material


def verify_validation_receipt(
    receipt: dict[str, Any],
    project_dir: str | Path,
    chapter_number: int,
    text: str,
    char_limits: dict[str, Any],
    expected_title_required: bool = True,
) -> dict[str, Any]:
    """Revalidate text and fail closed on any stale or forged receipt field."""
    report = validate_candidate(
        project_dir,
        chapter_number,
        text,
        char_limits,
        expected_title_required=expected_title_required,
    )
    receipt_issues: list[dict[str, Any]] = []
    required = {
        "receipt_type",
        "validation_version",
        "chapter",
        "chapter_sha256",
        "config_digest",
        "semantic_config_sha256",
        "chinese_chars",
        "passed",
        "status",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict):
        receipt = {}
        receipt_issues.append(_issue("validation_receipt_invalid", "验收收据必须是对象", severity="critical"))
    missing = sorted(required - set(receipt))
    if missing:
        receipt_issues.append(
            _issue(
                "validation_receipt_missing_fields",
                "验收收据缺少字段：" + "、".join(missing),
                severity="critical",
            )
        )
    if receipt.get("validation_version") != VALIDATION_VERSION:
        receipt_issues.append(_issue("validation_receipt_old_version", "验收收据版本过期或无效", severity="critical"))
    if receipt.get("receipt_type") != RECEIPT_TYPE:
        receipt_issues.append(_issue("validation_receipt_type_mismatch", "验收收据类型无效", severity="critical"))
    if receipt.get("chapter") != report["chapter"]:
        receipt_issues.append(_issue("validation_receipt_chapter_mismatch", "验收收据章号不匹配", severity="critical"))
    if receipt.get("chapter_sha256") != report["chapter_sha256"]:
        receipt_issues.append(_issue("validation_receipt_text_mismatch", "验收收据与当前正文哈希不匹配", severity="critical"))
    if receipt.get("config_digest") != report["config_digest"]:
        receipt_issues.append(_issue("validation_receipt_config_mismatch", "验收收据与当前配置不匹配", severity="critical"))
    if receipt.get("semantic_config_sha256") != report["semantic_config_sha256"]:
        receipt_issues.append(_issue("validation_receipt_semantic_config_mismatch", "语义配置哈希不匹配", severity="critical"))
    if receipt.get("chinese_chars") != report["chinese_chars"]:
        receipt_issues.append(_issue("validation_receipt_count_mismatch", "验收收据字符数不匹配", severity="critical"))
    if receipt.get("passed") is not True or receipt.get("status") != "pass":
        receipt_issues.append(_issue("validation_receipt_not_passed", "验收收据未记录通过状态", severity="critical"))
    strict_types_valid = (
        type(receipt.get("validation_version")) is int
        and type(receipt.get("chapter")) is int
        and type(receipt.get("chinese_chars")) is int
        and type(receipt.get("passed")) is bool
        and isinstance(receipt.get("chapter_sha256"), str)
        and isinstance(receipt.get("config_digest"), str)
        and isinstance(receipt.get("semantic_config_sha256"), str)
        and isinstance(receipt.get("receipt_sha256"), str)
    )
    hashes_valid = all(
        re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(key) or ""))
        for key in ("chapter_sha256", "config_digest", "semantic_config_sha256", "receipt_sha256")
    )
    if not strict_types_valid or not hashes_valid:
        receipt_issues.append(_issue("validation_receipt_format_invalid", "验收收据字段类型或哈希格式无效", severity="critical"))
    if not missing:
        receipt_material = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        if receipt.get("receipt_sha256") != _canonical_digest(receipt_material):
            receipt_issues.append(_issue("validation_receipt_digest_mismatch", "验收收据内容被篡改", severity="critical"))

    if receipt_issues:
        report["issues"] = _deduplicate_issues(report["issues"] + receipt_issues)
        report["summary"] = {
            "total": len(report["issues"]),
            "blocking": sum(1 for item in report["issues"] if item.get("blocking", True)),
        }
        report["passed"] = False
        report["ok"] = False
        report["status"] = "blocked"
    report["receipt_valid"] = bool(report["passed"] and not receipt_issues)
    return report


__all__ = [
    "VALIDATION_VERSION",
    "validate_candidate",
    "build_validation_receipt",
    "verify_validation_receipt",
]
