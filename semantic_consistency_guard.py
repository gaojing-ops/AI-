# -*- coding: utf-8 -*-
"""Deterministic, configuration-driven semantic consistency checks.

The guard deliberately does not call a model.  Project-specific invariants are
loaded from ``plot/semantic_invariants.json`` and every finding contains stable
machine-readable severity and evidence fields.

Supported configuration shape::

    {
      "identity_bindings": [
        {
          "id": "subject_real_name",
          "entity": "subject",
          "claim_patterns": ["subject.*?(?P<value>A|B)"],
          "allowed_values": ["A"],
          "severity": "critical"
        }
      ],
      "exclusive_fact_groups": [
        {
          "id": "cause_of_death",
          "facts": [
            {"id": "cause_a", "patterns": ["pattern A"]},
            {"id": "cause_b", "patterns": ["pattern B"]}
          ],
          "max_active": 1,
          "severity": "critical"
        }
      ],
      "forbidden_patterns": [
        {"id": "internal_note", "patterns": ["审稿报告"]}
      ],
      "numeric_rules": [
        {
          "id": "ambient_oxygen",
          "patterns": ["氧浓度.*?(?P<value>\\d+(?:\\.\\d+)?)%"],
          "min": 15,
          "max": 25,
          "unit": "%"
        }
      ]
    }

All four top-level collections are required.  Missing or malformed
configuration fails closed so unattended generation cannot silently skip the
semantic gate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
import json
import math
from pathlib import Path
import re
import unicodedata


CONFIG_FILENAME = "semantic_invariants.json"
CONFIG_KEYS = (
    "identity_bindings",
    "exclusive_fact_groups",
    "forbidden_patterns",
    "numeric_rules",
)

SEVERITIES = ("info", "warning", "error", "critical")
BLOCKING_SEVERITIES = frozenset(("error", "critical"))


# These are universal manuscript leaks, not project lore.  Project-specific
# phrases belong in forbidden_patterns instead.
_META_PATTERNS = (
    (
        "transport_truncation_artifact",
        re.compile(
            r"(?:\b(?:output\s+)?truncated\b|\boriginal\s+token\s+count\b|"
            r"\b\d+\s+tokens?\s+truncated\b|内容(?:已)?截断)",
            re.IGNORECASE,
        ),
    ),
    (
        "chapter_reference",
        re.compile(
            r"(?:第[一二三四五六七八九十百千万两0-9]+章|"
            r"上一章|前一章|下一章|本章|这一章|前文|后文|下文|"
            r"全章(?:中|里|内|唯一|仅|只|首次|第一次|的))"
        ),
    ),
    (
        "outline_language",
        re.compile(r"(?:细纲|章纲|章节目标|章末钩子|本卷目标|第一卷要找的|本卷要找的)"),
    ),
    (
        "author_reader_language",
        re.compile(
            r"(?:作者|读者)(?:会|能|可以|应该|需要|将|会在|看到|知道|发现|注意到|理解)"
        ),
    ),
    (
        "craft_commentary",
        re.compile(
            r"(?:这是|此处|这里)?(?:第[一二三四五六七八九十百千万两0-9]+案)?"
            r"(?:第[一二三四五六七八九十百千万两0-9]+次|第一次|首次)?(?:正向)?"
            r"(?:兑现|呼应|对应|推进)(?:题名|书名|主线|人设|爽点|卖点|能力)"
        ),
    ),
    (
        "draft_instruction",
        re.compile(r"(?:此处|这里|本段|本节)(?:需要|应该|应当|可以)(?:补|改|重写|展开|删)"),
    ),
)

_HEADING_RE = re.compile(
    r"^\s*第[零〇一二三四五六七八九十百千万两0-9]+章(?:\s|$|[：:、.-])"
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+(?:[。！？!?]+|…{2,})?")
_VISIBLE_CHAR_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


class _ConfigError(ValueError):
    pass


def _config_path(project_dir: str | Path) -> Path:
    root = Path(project_dir)
    if root.name.lower() == "plot":
        return root / CONFIG_FILENAME
    return root / "plot" / CONFIG_FILENAME


def _safe_severity(value: object, *, default: str = "error") -> str:
    severity = str(value or default).strip().lower()
    if severity not in SEVERITIES:
        raise _ConfigError(
            f"severity 必须是 {', '.join(SEVERITIES)} 之一，实际为 {severity!r}"
        )
    return severity


def _as_rule_list(value: object, key: str) -> list[dict]:
    """Accept either a JSON list or an id-keyed JSON object."""
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            if isinstance(item, str) and key == "forbidden_patterns":
                result.append({"id": f"forbidden_{index + 1}", "patterns": [item]})
            elif isinstance(item, dict):
                result.append(dict(item))
            else:
                raise _ConfigError(f"{key}[{index}] 必须是对象")
        return result
    if isinstance(value, dict):
        result = []
        for rule_id, item in value.items():
            if isinstance(item, str) and key == "forbidden_patterns":
                item = {"patterns": [item]}
            if not isinstance(item, dict):
                raise _ConfigError(f"{key}.{rule_id} 必须是对象")
            rule = dict(item)
            rule.setdefault("id", str(rule_id))
            if key == "identity_bindings":
                rule.setdefault("entity", str(rule_id))
            result.append(rule)
        return result
    raise _ConfigError(f"{key} 必须是数组或对象")


def _string_list(value: object, location: str, *, required: bool = True) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif value is None and not required:
        return []
    else:
        raise _ConfigError(f"{location} 必须是字符串或字符串数组")
    result = []
    for index, item in enumerate(values):
        text = str(item).strip() if isinstance(item, str) else ""
        if not text:
            raise _ConfigError(f"{location}[{index}] 不能为空")
        result.append(text)
    if required and not result:
        raise _ConfigError(f"{location} 不能为空")
    return result


def _compile_patterns(
    value: object,
    location: str,
    *,
    required_group: str | None = None,
) -> tuple[list[str], list[re.Pattern]]:
    patterns = _string_list(value, location)
    compiled = []
    for index, pattern in enumerate(patterns):
        if len(pattern) > 500:
            raise _ConfigError(f"{location}[{index}] 正则过长，最多500字符")
        if re.search(r"\\[1-9]|\(\?P=|\(\?<=[^)]|\(\?<![^)]", pattern):
            raise _ConfigError(f"{location}[{index}] 禁止反向引用或后向断言")
        if re.search(
            r"\((?:\\.|[^()])*?(?:\*|\+|\{\d+(?:,\d*)?\})"
            r"(?:\\.|[^()])*?\)\s*(?:\*|\+|\{\d+(?:,\d*)?\})",
            pattern,
        ):
            raise _ConfigError(f"{location}[{index}] 禁止嵌套重复量词")
        if re.search(
            r"\((?:\\.|[^()])*\|(?:\\.|[^()])*\)"
            r"\s*(?:\*|\+|\{\d+(?:,\d*)?\})",
            pattern,
        ):
            raise _ConfigError(f"{location}[{index}] 禁止对含分支的分组做无界重复")
        for upper in re.findall(r"\{\d+,(\d+)\}", pattern):
            if int(upper) > 2000:
                raise _ConfigError(f"{location}[{index}] 重复上限过大")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise _ConfigError(f"{location}[{index}] 正则无效: {exc}") from exc
        if required_group and required_group not in regex.groupindex:
            raise _ConfigError(
                f"{location}[{index}] 必须包含命名捕获组 (?P<{required_group}>...)"
            )
        compiled.append(regex)
    return patterns, compiled


def _rule_id(rule: dict, location: str) -> str:
    value = str(rule.get("id") or "").strip()
    if not value:
        raise _ConfigError(f"{location}.id 不能为空")
    return value


def _normalize_identity_rules(raw: object) -> list[dict]:
    rules = _as_rule_list(raw, "identity_bindings")
    normalized = []
    for index, rule in enumerate(rules):
        location = f"identity_bindings[{index}]"
        rule_id = _rule_id(rule, location)
        entity = str(rule.get("entity") or "").strip()
        if not entity:
            raise _ConfigError(f"{location}.entity 不能为空")
        value_group = str(rule.get("value_group") or "value").strip()
        if not value_group:
            raise _ConfigError(f"{location}.value_group 不能为空")
        raw_patterns = rule.get("claim_patterns", rule.get("patterns"))
        patterns, compiled = _compile_patterns(
            raw_patterns,
            f"{location}.claim_patterns",
            required_group=value_group,
        )
        allowed_values = _string_list(
            rule.get("allowed_values"),
            f"{location}.allowed_values",
            required=True,
        )
        normalized.append(
            {
                "id": rule_id,
                "entity": entity,
                "value_group": value_group,
                "patterns": patterns,
                "compiled": compiled,
                "allowed_values": allowed_values,
                "severity": _safe_severity(rule.get("severity"), default="critical"),
                "message": str(rule.get("message") or "").strip(),
            }
        )
    return normalized


def _normalize_fact_groups(raw: object) -> list[dict]:
    groups = _as_rule_list(raw, "exclusive_fact_groups")
    normalized = []
    for index, group in enumerate(groups):
        location = f"exclusive_fact_groups[{index}]"
        group_id = _rule_id(group, location)
        raw_facts = group.get("facts")
        if isinstance(raw_facts, dict):
            facts = []
            for fact_id, fact in raw_facts.items():
                if isinstance(fact, (str, list)):
                    fact = {"patterns": fact}
                if not isinstance(fact, dict):
                    raise _ConfigError(f"{location}.facts.{fact_id} 必须是对象")
                item = dict(fact)
                item.setdefault("id", str(fact_id))
                facts.append(item)
        elif isinstance(raw_facts, list):
            facts = raw_facts
        else:
            raise _ConfigError(f"{location}.facts 必须是数组或对象")
        if len(facts) < 2:
            raise _ConfigError(f"{location}.facts 至少需要两个互斥事实")

        normalized_facts = []
        seen_fact_ids = set()
        for fact_index, fact in enumerate(facts):
            fact_location = f"{location}.facts[{fact_index}]"
            if not isinstance(fact, dict):
                raise _ConfigError(f"{fact_location} 必须是对象")
            fact_id = _rule_id(fact, fact_location)
            if fact_id in seen_fact_ids:
                raise _ConfigError(f"{location} 内 fact id 重复: {fact_id}")
            seen_fact_ids.add(fact_id)
            patterns, compiled = _compile_patterns(
                fact.get("patterns", fact.get("pattern")),
                f"{fact_location}.patterns",
            )
            normalized_facts.append(
                {
                    "id": fact_id,
                    "patterns": patterns,
                    "compiled": compiled,
                    "label": str(fact.get("label") or fact_id),
                }
            )

        try:
            max_active = int(group.get("max_active", 1))
        except (TypeError, ValueError) as exc:
            raise _ConfigError(f"{location}.max_active 必须是正整数") from exc
        if max_active < 1:
            raise _ConfigError(f"{location}.max_active 必须是正整数")
        normalized.append(
            {
                "id": group_id,
                "facts": normalized_facts,
                "max_active": max_active,
                "severity": _safe_severity(group.get("severity"), default="critical"),
                "message": str(group.get("message") or "").strip(),
            }
        )
    return normalized


def _normalize_forbidden_rules(raw: object) -> list[dict]:
    rules = _as_rule_list(raw, "forbidden_patterns")
    normalized = []
    for index, rule in enumerate(rules):
        location = f"forbidden_patterns[{index}]"
        rule_id = _rule_id(rule, location)
        patterns, compiled = _compile_patterns(
            rule.get("patterns", rule.get("pattern")),
            f"{location}.patterns",
        )
        normalized.append(
            {
                "id": rule_id,
                "patterns": patterns,
                "compiled": compiled,
                "severity": _safe_severity(rule.get("severity"), default="error"),
                "message": str(rule.get("message") or "").strip(),
            }
        )
    return normalized


def _finite_number(value: object, location: str) -> float:
    if isinstance(value, bool):
        raise _ConfigError(f"{location} 必须是有限数字")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise _ConfigError(f"{location} 必须是有限数字") from exc
    if not math.isfinite(result):
        raise _ConfigError(f"{location} 必须是有限数字")
    return result


def _normalize_numeric_rules(raw: object) -> list[dict]:
    rules = _as_rule_list(raw, "numeric_rules")
    normalized = []
    for index, rule in enumerate(rules):
        location = f"numeric_rules[{index}]"
        rule_id = _rule_id(rule, location)
        value_group = str(rule.get("value_group") or "value").strip()
        if not value_group:
            raise _ConfigError(f"{location}.value_group 不能为空")
        patterns, compiled = _compile_patterns(
            rule.get("patterns", rule.get("pattern")),
            f"{location}.patterns",
            required_group=value_group,
        )
        if "min" not in rule and "max" not in rule:
            raise _ConfigError(f"{location} 至少需要 min 或 max")
        minimum = _finite_number(rule["min"], f"{location}.min") if "min" in rule else None
        maximum = _finite_number(rule["max"], f"{location}.max") if "max" in rule else None
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _ConfigError(f"{location}.min 不能大于 max")
        normalized.append(
            {
                "id": rule_id,
                "value_group": value_group,
                "patterns": patterns,
                "compiled": compiled,
                "min": minimum,
                "max": maximum,
                "unit": str(rule.get("unit") or "").strip(),
                "severity": _safe_severity(rule.get("severity"), default="error"),
                "message": str(rule.get("message") or "").strip(),
            }
        )
    return normalized


def _validate_and_normalize_config(data: object) -> dict:
    if not isinstance(data, dict):
        raise _ConfigError("配置根节点必须是对象")
    missing = [key for key in CONFIG_KEYS if key not in data]
    if missing:
        raise _ConfigError("缺少必填配置项: " + ", ".join(missing))
    normalized = {
        "identity_bindings": _normalize_identity_rules(data["identity_bindings"]),
        "exclusive_fact_groups": _normalize_fact_groups(data["exclusive_fact_groups"]),
        "forbidden_patterns": _normalize_forbidden_rules(data["forbidden_patterns"]),
        "numeric_rules": _normalize_numeric_rules(data["numeric_rules"]),
    }
    for key, rules in normalized.items():
        ids = [rule["id"] for rule in rules]
        duplicated = sorted(rule_id for rule_id, count in Counter(ids).items() if count > 1)
        if duplicated:
            raise _ConfigError(f"{key} 存在重复 id: {', '.join(duplicated)}")
    if sum(len(rules) for rules in normalized.values()) <= 0:
        raise _ConfigError("语义真相合同不能是四类规则全空的空壳")
    if not (
        normalized["identity_bindings"]
        or normalized["exclusive_fact_groups"]
    ):
        raise _ConfigError("语义真相合同至少需要一条身份绑定或互斥事实规则")
    return normalized


def _make_issue(
    code: str,
    severity: str,
    message: str,
    evidence: list[dict] | None = None,
    *,
    rule_id: str = "",
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "blocking": severity in BLOCKING_SEVERITIES,
        "rule_id": rule_id,
        "message": message,
        "evidence": evidence or [],
    }


def _configuration_report(
    *,
    path: Path,
    available: bool,
    valid: bool,
    issue: dict | None,
    config: dict | None = None,
) -> dict:
    return {
        "available": available,
        "valid": valid,
        "path": str(path),
        "config": config,
        "issues": [issue] if issue else [],
    }


def load_semantic_invariants(project_dir: str | Path) -> dict:
    """Load and validate ``plot/semantic_invariants.json``.

    The function never turns a broken file into an empty ruleset.  Missing
    configuration is explicitly ``available=False``; malformed configuration
    is ``available=True, valid=False``.  Both states are blocking.
    """
    path = _config_path(project_dir)
    if not path.is_file():
        issue = _make_issue(
            "semantic_config_missing",
            "critical",
            f"语义守卫不可用：缺少配置文件 {path}",
            [{"path": str(path)}],
            rule_id="semantic_invariants",
        )
        return _configuration_report(
            path=path, available=False, valid=False, issue=issue
        )
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issue = _make_issue(
            "semantic_config_invalid",
            "critical",
            f"语义守卫配置损坏，已阻断：{exc}",
            [{"path": str(path)}],
            rule_id="semantic_invariants",
        )
        return _configuration_report(
            path=path, available=True, valid=False, issue=issue
        )
    try:
        normalized = _validate_and_normalize_config(raw)
    except _ConfigError as exc:
        issue = _make_issue(
            "semantic_config_invalid",
            "critical",
            f"语义守卫配置无效，已阻断：{exc}",
            [{"path": str(path)}],
            rule_id="semantic_invariants",
        )
        return _configuration_report(
            path=path, available=True, valid=False, issue=issue
        )
    return _configuration_report(
        path=path,
        available=True,
        valid=True,
        issue=None,
        config=normalized,
    )


def _line_excerpt(text: str, start: int) -> tuple[int, int, str]:
    line = text.count("\n", 0, start) + 1
    previous_newline = text.rfind("\n", 0, start)
    column = start - previous_newline
    end_of_line = text.find("\n", start)
    if end_of_line < 0:
        end_of_line = len(text)
    excerpt = text[previous_newline + 1 : end_of_line].strip()
    if len(excerpt) > 240:
        excerpt = excerpt[:237] + "..."
    return line, column, excerpt


def _evidence(
    chapter: object,
    text: str,
    start: int,
    end: int,
    *,
    matched: str | None = None,
    **details: object,
) -> dict:
    line, column, excerpt = _line_excerpt(text, start)
    item = {
        "chapter": chapter,
        "line": line,
        "column": column,
        "start": start,
        "end": end,
        "excerpt": excerpt,
        "matched": matched if matched is not None else text[start:end],
    }
    item.update(details)
    return item


def _normalize_exact_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value.strip()))


def _visible_length(value: str) -> int:
    return len(_VISIBLE_CHAR_RE.findall(value))


def _iter_paragraphs(text: str):
    # Paragraphs are separated by one or more blank lines.  The offsets are
    # preserved for actionable evidence.
    cursor = 0
    for separator in re.finditer(r"\n[ \t\u3000]*\n+", text):
        end = separator.start()
        raw = text[cursor:end]
        leading = len(raw) - len(raw.lstrip())
        if raw.strip():
            yield cursor + leading, end, raw.strip()
        cursor = separator.end()
    raw = text[cursor:]
    leading = len(raw) - len(raw.lstrip())
    if raw.strip():
        yield cursor + leading, len(text), raw.strip()


def _iter_sentences(text: str):
    for match in _SENTENCE_RE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        sentence = raw.strip()
        if sentence:
            yield match.start() + leading, match.end(), sentence


def _scan_meta_leaks(chapter: object, text: str) -> list[dict]:
    issues = []
    seen = set()
    first_line_end = text.find("\n")
    if first_line_end < 0:
        first_line_end = len(text)
    heading_end = first_line_end if _HEADING_RE.match(text[:first_line_end]) else 0
    for meta_id, regex in _META_PATTERNS:
        for match in regex.finditer(text):
            if meta_id == "chapter_reference":
                # The first-line chapter title is required manuscript syntax,
                # not an editorial reference inside the story.
                if heading_end and match.start() < heading_end:
                    continue
                # References to a real document section can be valid in
                # legal/procedural scenes. Bare "第X章" references remain
                # blocked because they almost always leak the author layer.
                prefix = text[max(0, match.start() - 8):match.start()]
                if (
                    match.group(0).startswith("第")
                    and re.search(
                        r"(?:合同|协议|章程|条例|法规|手册|报告|卷宗)\s*$",
                        prefix,
                    )
                ):
                    continue
            key = (match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                _make_issue(
                    "meta_narrative_leak",
                    "error",
                    "正文出现章节、创作或审稿层面的元话语",
                    [
                        _evidence(
                            chapter,
                            text,
                            match.start(),
                            match.end(),
                            meta_pattern=meta_id,
                        )
                    ],
                    rule_id=meta_id,
                )
            )
    return issues


def _duplicate_issues_for_units(
    chapter: object,
    text: str,
    units: Iterable[tuple[int, int, str]],
    *,
    code: str,
    label: str,
    min_visible_chars: int,
) -> list[dict]:
    occurrences: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for start, end, raw in units:
        normalized = _normalize_exact_text(raw)
        if _HEADING_RE.match(raw) or _visible_length(normalized) < min_visible_chars:
            continue
        occurrences[normalized].append((start, end, raw))

    issues = []
    for rows in occurrences.values():
        if len(rows) < 2:
            continue
        evidence = [
            _evidence(chapter, text, start, end, duplicate_index=index + 1)
            for index, (start, end, _raw) in enumerate(rows)
        ]
        issues.append(
            _make_issue(
                code,
                "error",
                f"章内出现{len(rows)}次完全重复的{label}",
                evidence,
                rule_id="exact_duplicate",
            )
        )
    return issues


def _scan_exact_duplicates(chapter: object, text: str) -> list[dict]:
    issues = _duplicate_issues_for_units(
        chapter,
        text,
        _iter_paragraphs(text),
        code="duplicate_paragraph",
        label="段落",
        min_visible_chars=16,
    )
    issues.extend(
        _duplicate_issues_for_units(
            chapter,
            text,
            _iter_sentences(text),
            code="duplicate_sentence",
            label="句子",
            min_visible_chars=10,
        )
    )
    return issues


def _scan_forbidden_patterns(
    chapters: list[tuple[object, str]], rules: list[dict]
) -> list[dict]:
    issues = []
    for rule in rules:
        seen = set()
        for chapter, text in chapters:
            for regex in rule["compiled"]:
                for match in regex.finditer(text):
                    key = (chapter, match.start(), match.end())
                    if key in seen:
                        continue
                    seen.add(key)
                    message = rule["message"] or f"正文命中禁用模式 {rule['id']}"
                    issues.append(
                        _make_issue(
                            "forbidden_pattern",
                            rule["severity"],
                            message,
                            [_evidence(chapter, text, match.start(), match.end())],
                            rule_id=rule["id"],
                        )
                    )
    return issues


def _normalize_claim_value(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"\s+", "", value)
    return value.strip("'\"“”‘’《》【】[]()（）,，。；;：:！!？?")


def _scan_identity_bindings(
    chapters: list[tuple[object, str]], rules: list[dict]
) -> list[dict]:
    issues = []
    for rule in rules:
        observed: dict[str, list[dict]] = defaultdict(list)
        for chapter, text in chapters:
            for regex in rule["compiled"]:
                for match in regex.finditer(text):
                    value = _normalize_claim_value(match.group(rule["value_group"]))
                    if not value:
                        continue
                    observed[value].append(
                        _evidence(
                            chapter,
                            text,
                            match.start(),
                            match.end(),
                            claimed_value=value,
                            entity=rule["entity"],
                        )
                    )

        if len(observed) > 1:
            evidence = []
            for value in sorted(observed):
                evidence.extend(observed[value])
            message = rule["message"] or (
                f"{rule['entity']} 出现互相冲突的身份绑定："
                + "、".join(sorted(observed))
            )
            issues.append(
                _make_issue(
                    "identity_binding_conflict",
                    rule["severity"],
                    message,
                    evidence,
                    rule_id=rule["id"],
                )
            )

        allowed = {_normalize_claim_value(value) for value in rule["allowed_values"]}
        if allowed:
            for value, evidence in sorted(observed.items()):
                if value in allowed:
                    continue
                message = (
                    f"{rule['entity']} 的身份值 {value!r} 不在允许集合 "
                    f"{sorted(allowed)!r} 中"
                )
                issues.append(
                    _make_issue(
                        "identity_value_not_allowed",
                        rule["severity"],
                        message,
                        evidence,
                        rule_id=rule["id"],
                    )
                )
    return issues


def _scan_exclusive_facts(
    chapters: list[tuple[object, str]], groups: list[dict]
) -> list[dict]:
    issues = []
    for group in groups:
        active: dict[str, list[dict]] = defaultdict(list)
        labels = {}
        for fact in group["facts"]:
            labels[fact["id"]] = fact["label"]
            seen = set()
            for chapter, text in chapters:
                for regex in fact["compiled"]:
                    for match in regex.finditer(text):
                        key = (chapter, match.start(), match.end())
                        if key in seen:
                            continue
                        seen.add(key)
                        active[fact["id"]].append(
                            _evidence(
                                chapter,
                                text,
                                match.start(),
                                match.end(),
                                fact_id=fact["id"],
                                fact_label=fact["label"],
                            )
                        )
        if len(active) <= group["max_active"]:
            continue
        evidence = []
        for fact_id in sorted(active):
            evidence.extend(active[fact_id])
        active_labels = [labels[fact_id] for fact_id in sorted(active)]
        message = group["message"] or (
            f"互斥事实组 {group['id']} 同时出现 {len(active)} 项："
            + "、".join(active_labels)
        )
        issues.append(
            _make_issue(
                "exclusive_fact_conflict",
                group["severity"],
                message,
                evidence,
                rule_id=group["id"],
            )
        )
    return issues


def _parse_numeric_value(raw: str) -> float | None:
    normalized = unicodedata.normalize("NFKC", str(raw or ""))
    normalized = normalized.replace(",", "").strip()
    try:
        value = float(normalized)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _scan_numeric_rules(
    chapters: list[tuple[object, str]], rules: list[dict]
) -> list[dict]:
    issues = []
    for rule in rules:
        seen = set()
        for chapter, text in chapters:
            for regex in rule["compiled"]:
                for match in regex.finditer(text):
                    key = (chapter, match.start(), match.end())
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_value = match.group(rule["value_group"])
                    value = _parse_numeric_value(raw_value)
                    if value is None:
                        issues.append(
                            _make_issue(
                                "numeric_value_invalid",
                                "critical",
                                f"数值规则 {rule['id']} 捕获到不可解析的数值 {raw_value!r}",
                                [
                                    _evidence(
                                        chapter,
                                        text,
                                        match.start(),
                                        match.end(),
                                        raw_value=raw_value,
                                    )
                                ],
                                rule_id=rule["id"],
                            )
                        )
                        continue
                    below = rule["min"] is not None and value < rule["min"]
                    above = rule["max"] is not None and value > rule["max"]
                    if not below and not above:
                        continue
                    bounds = []
                    if rule["min"] is not None:
                        bounds.append(f">={rule['min']:g}")
                    if rule["max"] is not None:
                        bounds.append(f"<={rule['max']:g}")
                    message = rule["message"] or (
                        f"数值 {value:g}{rule['unit']} 违反规则 {rule['id']} "
                        f"（要求 {' 且 '.join(bounds)}{rule['unit']}）"
                    )
                    issues.append(
                        _make_issue(
                            "numeric_rule_violation",
                            rule["severity"],
                            message,
                            [
                                _evidence(
                                    chapter,
                                    text,
                                    match.start(),
                                    match.end(),
                                    value=value,
                                    unit=rule["unit"],
                                    minimum=rule["min"],
                                    maximum=rule["max"],
                                )
                            ],
                            rule_id=rule["id"],
                        )
                    )
    return issues


def _normalize_chapter_collection(chapters: object) -> list[tuple[object, str]]:
    if isinstance(chapters, Mapping):
        source = list(chapters.items())
    elif isinstance(chapters, Iterable) and not isinstance(chapters, (str, bytes)):
        source = list(chapters)
    else:
        raise TypeError("chapters 必须是 {章节号: 正文} 映射或章节条目数组")

    result = []
    seen_chapters = set()
    for index, item in enumerate(source):
        if isinstance(item, Mapping):
            chapter = item.get("chapter")
            text = item.get("text")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            chapter, text = item
        else:
            raise TypeError(f"chapters[{index}] 必须包含 chapter 和 text")
        if chapter is None or isinstance(chapter, bool):
            raise TypeError(f"chapters[{index}].chapter 不能为空")
        try:
            hash(chapter)
        except TypeError as exc:
            raise TypeError(f"chapters[{index}].chapter 必须可哈希") from exc
        if chapter in seen_chapters:
            raise TypeError(f"章节号重复: {chapter!r}")
        seen_chapters.add(chapter)
        if not isinstance(text, str):
            raise TypeError(f"chapters[{index}].text 必须是字符串")
        result.append((chapter, text))
    if not result:
        raise TypeError("chapters 不能为空")
    return result


def _sort_issues(issues: list[dict]) -> list[dict]:
    severity_rank = {value: index for index, value in enumerate(SEVERITIES)}

    def key(issue):
        evidence = issue.get("evidence") or [{}]
        first = evidence[0]
        chapter = first.get("chapter")
        chapter_key = (0, chapter) if isinstance(chapter, int) else (1, str(chapter or ""))
        return (
            -severity_rank.get(issue.get("severity"), 0),
            chapter_key,
            int(first.get("line") or 0),
            issue.get("code") or "",
            issue.get("rule_id") or "",
        )

    return sorted(issues, key=key)


def _finalize_report(
    *,
    config_result: dict,
    scope: str,
    chapters: list[tuple[object, str]] | None,
    issues: list[dict],
) -> dict:
    issues = _sort_issues(issues)
    counts = Counter(issue["severity"] for issue in issues)
    blocking_count = sum(1 for issue in issues if issue.get("blocking"))
    config_valid = bool(config_result.get("valid"))
    if not config_result.get("available"):
        status = "unavailable"
    elif not config_valid:
        status = "blocked"
    elif blocking_count:
        status = "fail"
    else:
        status = "pass"
    passed = status == "pass"
    return {
        "status": status,
        "passed": passed,
        "ok": passed,
        "available": bool(config_result.get("available")),
        "config_valid": config_valid,
        "config_path": config_result.get("path", ""),
        "scope": scope,
        "chapters_scanned": [chapter for chapter, _text in (chapters or [])],
        "summary": {
            "total": len(issues),
            "blocking": blocking_count,
            **{severity: counts.get(severity, 0) for severity in SEVERITIES},
        },
        "issues": issues,
    }


def scan_chapters(project_dir: str | Path, chapters: object) -> dict:
    """Scan a chapter collection and return a structured fail/pass report.

    ``chapters`` may be ``{chapter_number: text}``, an iterable of
    ``(chapter_number, text)`` pairs, or dictionaries with ``chapter`` and
    ``text`` keys.  Identity and exclusive-fact conflicts are evaluated across
    the complete collection, while duplicate prose remains chapter-local.
    """
    config_result = load_semantic_invariants(project_dir)
    if not config_result["valid"]:
        return _finalize_report(
            config_result=config_result,
            scope="collection",
            chapters=None,
            issues=list(config_result["issues"]),
        )

    try:
        normalized_chapters = _normalize_chapter_collection(chapters)
    except (TypeError, ValueError) as exc:
        issue = _make_issue(
            "semantic_scan_input_invalid",
            "critical",
            f"语义扫描输入无效，已阻断：{exc}",
            [],
            rule_id="scan_input",
        )
        return _finalize_report(
            config_result=config_result,
            scope="collection",
            chapters=None,
            issues=[issue],
        )

    config = config_result["config"]
    issues = []
    for chapter, text in normalized_chapters:
        issues.extend(_scan_meta_leaks(chapter, text))
        issues.extend(_scan_exact_duplicates(chapter, text))
    issues.extend(
        _scan_forbidden_patterns(normalized_chapters, config["forbidden_patterns"])
    )
    issues.extend(
        _scan_identity_bindings(normalized_chapters, config["identity_bindings"])
    )
    issues.extend(
        _scan_exclusive_facts(normalized_chapters, config["exclusive_fact_groups"])
    )
    issues.extend(_scan_numeric_rules(normalized_chapters, config["numeric_rules"]))
    return _finalize_report(
        config_result=config_result,
        scope="collection",
        chapters=normalized_chapters,
        issues=issues,
    )


def scan_chapter(
    project_dir: str | Path,
    text: str,
    chapter: object = 0,
) -> dict:
    """Scan one chapter using the same fail-closed report contract."""
    report = scan_chapters(project_dir, [(chapter, text)])
    report["scope"] = "chapter"
    return report


__all__ = [
    "CONFIG_FILENAME",
    "load_semantic_invariants",
    "scan_chapter",
    "scan_chapters",
]
