# -*- coding: utf-8 -*-
"""
跨章一致性扫描器 (Cross-Chapter Consistency Scanner)
======================================================
用途：扫描指定章节范围内的所有章节，检测：
  1. 事件重复 — 同一事件在多章中出现
  2. 时间线回档 — 后面章节的事件时间早于前面章节
  3. 禁词出现 — 在不应出现的章节范围出现禁词
  4. 角色名旧称 — 已被改名的旧角色名残留
  5. 叙述模板复用 — 同一解释/免责声明句式跨章过密

提供 scan_events / scan_forbidden / scan_old_names / scan_timeline_jumps /
scan_rhetorical_templates 五个核心函数，由 GUI 批量托管流程直接调用。
"""

import os
import re
import glob
import json
import argparse

# ============================================================
# 配置
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 事件模式库：正则匹配文本 + 事件标签 + 描述。
# 通用底座默认为空，由 plot/tool_rules.json 的 event_patterns 提供项目规则。
EVENT_PATTERNS = []

# 特殊事件完成章节：该事件在指定章节完成后，后续正常引用不算重复
# {event_label: completion_chapter}
EVENT_COMPLETION_CHAPTERS = {}

# 禁词规则：(关键词, 禁止原因, 允许出现的最小章节号, 0=全禁)
FORBIDDEN_TERMS = []

# 章节门控禁词：(模式, 原因, 禁写范围：在该章节之前禁止)
CHAPTER_GATED_TERMS = []

# 旧名前缀（用于精确匹配，避免误杀）
OLD_NAME_PATTERNS = {}
ALLOWED_CONTEXTS = {}
RHETORICAL_TEMPLATE_RULES = []
TOOL_RULE_ERRORS = []
OFFICIAL_CHAPTER_RE = re.compile(r"^第(\d{1,6})章\.txt$")


def _bounded_rule_int(value, default, minimum, field_name):
    """Parse one numeric tool-rule field without letting bad config crash scans."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        TOOL_RULE_ERRORS.append(f"{field_name} 必须是整数")
        return max(minimum, int(default))
    if parsed < minimum:
        TOOL_RULE_ERRORS.append(f"{field_name} 不能小于 {minimum}")
        return minimum
    return parsed


def _load_project_tool_rules(project_dir=None):
    global EVENT_PATTERNS, EVENT_COMPLETION_CHAPTERS, FORBIDDEN_TERMS, CHAPTER_GATED_TERMS, OLD_NAME_PATTERNS, ALLOWED_CONTEXTS, RHETORICAL_TEMPLATE_RULES, TOOL_RULE_ERRORS
    EVENT_PATTERNS = []
    EVENT_COMPLETION_CHAPTERS = {}
    FORBIDDEN_TERMS = []
    CHAPTER_GATED_TERMS = []
    OLD_NAME_PATTERNS = {}
    ALLOWED_CONTEXTS = {}
    RHETORICAL_TEMPLATE_RULES = []
    TOOL_RULE_ERRORS = []

    base_dir = project_dir or SCRIPT_DIR
    rules_path = os.path.join(base_dir, "plot", "tool_rules.json")
    if not os.path.exists(rules_path):
        return
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
    except Exception:
        return

    events = rules.get("event_patterns")
    if isinstance(events, list):
        normalized_events = []
        for item in events:
            if isinstance(item, list) and len(item) >= 3:
                normalized_events.append((str(item[0]), str(item[1]), str(item[2])))
            elif isinstance(item, dict) and item.get("pattern") and item.get("label"):
                normalized_events.append((str(item["pattern"]), str(item["label"]), str(item.get("description", item["label"]))))
        EVENT_PATTERNS = normalized_events

    completions = rules.get("event_completion_chapters")
    if isinstance(completions, dict):
        EVENT_COMPLETION_CHAPTERS = {str(k): int(v) for k, v in completions.items() if str(v).isdigit()}

    scanner_forbidden = rules.get("scanner_forbidden_terms")
    if isinstance(scanner_forbidden, list) and scanner_forbidden:
        normalized = []
        for item in scanner_forbidden:
            if isinstance(item, list) and len(item) >= 3:
                normalized.append((str(item[0]), str(item[1]), int(item[2] or 0)))
        if normalized:
            FORBIDDEN_TERMS = normalized

    gated = rules.get("chapter_gated_terms")
    if isinstance(gated, list):
        normalized_gated = []
        for item in gated:
            if isinstance(item, list) and len(item) >= 3:
                normalized_gated.append((str(item[0]), str(item[1]), int(item[2] or 0)))
            elif isinstance(item, dict) and item.get("pattern"):
                normalized_gated.append((str(item["pattern"]), str(item.get("reason", "章节门控")), int(item.get("gate_chapter", 0) or 0)))
        CHAPTER_GATED_TERMS = normalized_gated

    old_names = rules.get("old_name_patterns")
    if isinstance(old_names, dict) and old_names:
        OLD_NAME_PATTERNS = {str(k): str(v) for k, v in old_names.items()}

    allowed = rules.get("scanner_allowed_contexts")
    if isinstance(allowed, dict):
        ALLOWED_CONTEXTS = {str(k): [str(v) for v in vals] for k, vals in allowed.items() if isinstance(vals, list)}

    rhetorical = rules.get("rhetorical_template_rules")
    if isinstance(rhetorical, list):
        normalized_rhetorical = []
        seen_ids = set()
        for index, item in enumerate(rhetorical):
            if not isinstance(item, dict):
                TOOL_RULE_ERRORS.append(f"rhetorical_template_rules[{index}] 必须是对象")
                continue
            rule_id = str(item.get("id") or "").strip()
            patterns = item.get("patterns")
            if not rule_id or rule_id in seen_ids:
                TOOL_RULE_ERRORS.append(
                    f"rhetorical_template_rules[{index}].id 为空或重复"
                )
                continue
            if not isinstance(patterns, list) or not any(str(v).strip() for v in patterns):
                TOOL_RULE_ERRORS.append(
                    f"rhetorical_template_rules[{index}].patterns 不能为空"
                )
                continue
            compiled = []
            for pattern_index, pattern in enumerate(patterns):
                try:
                    compiled.append(re.compile(str(pattern), re.IGNORECASE))
                except re.error as exc:
                    TOOL_RULE_ERRORS.append(
                        f"rhetorical_template_rules[{index}].patterns[{pattern_index}] 正则无效：{exc}"
                    )
            if not compiled:
                continue
            seen_ids.add(rule_id)
            normalized_rhetorical.append({
                "id": rule_id,
                "label": str(item.get("label") or rule_id).strip(),
                "compiled": compiled,
                "per_chapter_max": _bounded_rule_int(
                    item.get("per_chapter_max", 3), 3, 0,
                    f"rhetorical_template_rules[{index}].per_chapter_max",
                ),
                "window_chapters": _bounded_rule_int(
                    item.get("window_chapters", 30), 30, 2,
                    f"rhetorical_template_rules[{index}].window_chapters",
                ),
                "window_total_max": _bounded_rule_int(
                    item.get("window_total_max", 18), 18, 0,
                    f"rhetorical_template_rules[{index}].window_total_max",
                ),
                "min_chapters": _bounded_rule_int(
                    item.get("min_chapters", 4), 4, 2,
                    f"rhetorical_template_rules[{index}].min_chapters",
                ),
                "severity": (
                    "HIGH"
                    if str(item.get("severity") or "HIGH").upper() == "HIGH"
                    else "MEDIUM"
                ),
                "message": str(item.get("message") or "").strip(),
            })
        RHETORICAL_TEMPLATE_RULES = normalized_rhetorical


_load_project_tool_rules()


def reload_project_tool_rules(project_dir=None):
    _load_project_tool_rules(project_dir)


# ============================================================
# 核心函数
# ============================================================

def read_chapter(filepath):
    """读取章节文件，返回(章号, 标题, 全文)"""
    if not os.path.exists(filepath):
        return None, None, None

    with open(filepath, "r", encoding="utf-8-sig") as f:
        text = f.read()

    # 提取章号
    basename = os.path.basename(filepath)
    match = OFFICIAL_CHAPTER_RE.fullmatch(basename)
    if match:
        chap_num = int(match.group(1))
    else:
        chap_num = 0

    # 提取标题
    lines = text.strip().split('\n')
    title = lines[0] if lines else ""

    return chap_num, title, text


def get_chapter_files(volume_dir, start=None, end=None):
    """获取指定卷目录下所有章文件，按章号排序"""
    pattern = os.path.join(volume_dir, "第*.txt")
    files = [
        f for f in sorted(glob.glob(pattern))
        if OFFICIAL_CHAPTER_RE.fullmatch(os.path.basename(f))
    ]

    if start is not None and end is not None:
        filtered = []
        for f in files:
            basename = os.path.basename(f)
            match = OFFICIAL_CHAPTER_RE.fullmatch(basename)
            if match:
                num = int(match.group(1))
                if start <= num <= end:
                    filtered.append(f)
        return sorted(filtered)

    return files


def get_chapter_files_recursive(output_dir, start=None, end=None):
    """Return official chapters across volume boundaries, ordered globally."""
    rows = []
    if not os.path.isdir(output_dir):
        return []
    for root_dir, dirs, files in os.walk(output_dir):
        dirs[:] = [name for name in dirs if name != ".backup"]
        for filename in files:
            match = OFFICIAL_CHAPTER_RE.fullmatch(filename)
            if not match:
                continue
            number = int(match.group(1))
            if start is not None and number < start:
                continue
            if end is not None and number > end:
                continue
            rows.append((number, os.path.join(root_dir, filename)))
    rows.sort(key=lambda item: (item[0], item[1]))
    return [path for _, path in rows]


def _is_recap_line(line):
    """判断一行是否为卷末回顾/总结性文字"""
    recap_keywords = [
        '这一卷，', '这一章，', '从黑衣人到', '没有人掉队',
        '也没有人需要被', '这一路',
    ]
    return any(kw in line for kw in recap_keywords)


def scan_events(chapters_data):
    """
    扫描事件重复。
    返回冲突列表：[{chap_a, line_a, event_label, chap_b, line_b, context}, ...]
    """
    # {event_label: [(chap_num, line_num, context), ...]}
    event_registry = {}
    conflicts = []

    for chap_num, title, text in chapters_data:
        # 跳过章标题行和空白行，从正文开始匹配
        lines = text.split('\n')
        title_end = 0
        for i, line in enumerate(lines):
            if line.strip() and not line.startswith('第') and '章' not in line:
                title_end = i
                break
        body_start_pos = sum(len(l) + 1 for l in lines[:title_end]) if title_end > 0 else 0

        for pattern, label, desc in EVENT_PATTERNS:
            matches = list(re.finditer(pattern, text, re.IGNORECASE))
            for m in matches:
                line_num = text[:m.start()].count('\n') + 1

                # 跳过标题行+空行区域和卷末回顾行
                if m.start() < body_start_pos:
                    continue
                if line_num > 0 and _is_recap_line(lines[line_num - 1]):
                    continue

                start = max(0, m.start() - 20)
                end = min(len(text), m.end() + 40)
                context = text[start:end].strip().replace('\n', ' ')

                completion_chap = EVENT_COMPLETION_CHAPTERS.get(label, 0)
                if completion_chap and chap_num > completion_chap:
                    conflicts.append({
                        'type': '已完成事件重演',
                        'severity': 'HIGH',
                        'event_label': label,
                        'event_desc': desc,
                        'first_chap': completion_chap,
                        'first_line': 0,
                        'first_context': f'该事件已在第{completion_chap}章完成',
                        'dup_chap': chap_num,
                        'dup_line': line_num,
                        'dup_context': context[:100],
                        'message': f"事件「{label}」已在第{completion_chap}章完成，第{chap_num}章出现重演动作",
                    })
                    continue

                if label not in event_registry:
                    event_registry[label] = []

                event_registry[label].append({
                    'chap': chap_num,
                    'line': line_num,
                    'context': context[:100],
                    'label': label,
                    'desc': desc,
                })

    # 检查重复
    for label, occurrences in event_registry.items():
        if len(occurrences) == 1:
            continue

        # 排序
        occurrences.sort(key=lambda x: x['chap'])

        # 标记所有跨章重复（同章内多次出现同一事件字样不算）
        first = occurrences[0]
        for i, occ in enumerate(occurrences[1:], 1):
            if occ['chap'] == first['chap']:
                continue
            conflicts.append({
                'type': '事件重复',
                'severity': 'HIGH',
                'event_label': label,
                'event_desc': first['desc'],
                'first_chap': first['chap'],
                'first_line': first['line'],
                'first_context': first['context'],
                'dup_chap': occ['chap'],
                'dup_line': occ['line'],
                'dup_context': occ['context'],
                'message': f"事件「{label}」已在第{first['chap']}章(第{first['line']}行)出现，在第{occ['chap']}章(第{occ['line']}行)重复出现",
            })

    return conflicts


def scan_forbidden(chapters_data):
    """
    扫描禁词。
    """
    conflicts = []

    for chap_num, title, text in chapters_data:
        # 全局禁词
        for keyword, reason, min_chapter in FORBIDDEN_TERMS:
            matches = list(re.finditer(keyword, text, re.IGNORECASE))
            for m in matches:
                ctx_start = max(0, m.start() - 12)
                ctx_end = min(len(text), m.end() + 12)
                ctx = text[ctx_start:ctx_end]
                if any(allowed in ctx for allowed in ALLOWED_CONTEXTS.get(keyword, [])):
                    continue

                line_num = text[:m.start()].count('\n') + 1
                start = max(0, m.start() - 15)
                end = min(len(text), m.end() + 30)
                context = text[start:end].strip().replace('\n', ' ')

                conflicts.append({
                    'type': '禁词',
                    'severity': 'HIGH',
                    'chap': chap_num,
                    'line': line_num,
                    'keyword': keyword,
                    'reason': reason,
                    'context': context[:100],
                    'message': f"第{chap_num}章(第{line_num}行): 禁词「{keyword}」— {reason}",
                })

        # 章节门控禁词
        for pattern, reason, gate_chapter in CHAPTER_GATED_TERMS:
            if chap_num >= gate_chapter:
                continue  # 已过门控章节，允许出现

            matches = list(re.finditer(pattern, text, re.IGNORECASE))
            for m in matches:
                line_num = text[:m.start()].count('\n') + 1
                start = max(0, m.start() - 15)
                end = min(len(text), m.end() + 30)
                context = text[start:end].strip().replace('\n', ' ')

                conflicts.append({
                    'type': '超前揭示',
                    'severity': 'MEDIUM',
                    'chap': chap_num,
                    'line': line_num,
                    'keyword': m.group(),
                    'reason': reason,
                    'gate_chapter': gate_chapter,
                    'context': context[:100],
                    'message': f"第{chap_num}章(第{line_num}行): 超前揭示「{m.group()}」— {reason}(最早应在第{gate_chapter}章)",
                })

    return conflicts


def scan_old_names(chapters_data):
    """
    扫描旧角色名残留。
    """
    conflicts = []

    for chap_num, title, text in chapters_data:
        for old_name, new_name in OLD_NAME_PATTERNS.items():
            if old_name not in text:
                continue

            lines = text.split('\n')
            for i, line in enumerate(lines, 1):
                if old_name in line:
                    conflicts.append({
                        'type': '旧名残留',
                        'severity': 'HIGH',
                        'chap': chap_num,
                        'line': i,
                        'old_name': old_name,
                        'new_name': new_name,
                        'context': line.strip()[:100],
                        'message': f"第{chap_num}章(第{i}行): 旧名「{old_name}」应改为「{new_name}」",
                    })

    return conflicts


def scan_rhetorical_templates(chapters_data):
    """Detect configured explanation/statement templates that recur too often.

    This is intentionally project-configured.  A phrase such as ``不能证明``
    can be legitimate once, especially in procedural fiction; it becomes a
    quality problem only when a rule-defined density is exceeded across a
    rolling chapter window or repeatedly inside one chapter.
    """
    conflicts = []
    if TOOL_RULE_ERRORS:
        conflicts.append({
            "type": "scanner_config_error",
            "severity": "HIGH",
            "message": "叙述模板扫描规则无效：" + "；".join(TOOL_RULE_ERRORS[:4]),
            "errors": list(TOOL_RULE_ERRORS),
        })
    if not RHETORICAL_TEMPLATE_RULES:
        return conflicts

    normalized = sorted(
        (
            (int(chapter), str(title or ""), str(text or ""))
            for chapter, title, text in chapters_data
        ),
        key=lambda row: row[0],
    )
    for rule in RHETORICAL_TEMPLATE_RULES:
        chapter_hits = []
        for chapter, _title, text in normalized:
            hits = []
            for regex in rule["compiled"]:
                for match in regex.finditer(text):
                    line = text[: match.start()].count("\n") + 1
                    start = max(0, match.start() - 36)
                    end = min(len(text), match.end() + 64)
                    hits.append({
                        "chapter": chapter,
                        "line": line,
                        "match": match.group(0)[:80],
                        "context": text[start:end].strip().replace("\n", " ")[:180],
                    })
            hits.sort(key=lambda item: (item["line"], item["match"]))
            if hits:
                chapter_hits.append({"chapter": chapter, "hits": hits})

        if not chapter_hits:
            continue

        per_chapter_breaches = [
            item for item in chapter_hits
            if len(item["hits"]) > rule["per_chapter_max"]
        ]

        # Find the single worst rolling window so a full-book audit does not
        # emit dozens of overlapping copies of the same finding.
        worst = None
        chapters_only = [item["chapter"] for item in chapter_hits]
        for end_index, end_chapter in enumerate(chapters_only):
            start_chapter = end_chapter - rule["window_chapters"] + 1
            window = [
                item for item in chapter_hits[: end_index + 1]
                if item["chapter"] >= start_chapter
            ]
            total = sum(len(item["hits"]) for item in window)
            score = (total, len(window), end_chapter)
            if worst is None or score > worst["score"]:
                worst = {
                    "score": score,
                    "start": window[0]["chapter"] if window else start_chapter,
                    "end": end_chapter,
                    "rows": window,
                    "total": total,
                }

        window_breach = bool(
            worst
            and worst["total"] > rule["window_total_max"]
            and len(worst["rows"]) >= rule["min_chapters"]
        )
        if not per_chapter_breaches and not window_breach:
            continue

        evidence = []
        source_rows = worst["rows"] if window_breach else per_chapter_breaches
        for row in source_rows:
            evidence.extend(row["hits"][:3])
            if len(evidence) >= 18:
                break
        matched_chapters = sorted({row["chapter"] for row in source_rows})
        max_per_chapter = max(len(row["hits"]) for row in chapter_hits)
        total_matches = worst["total"] if worst else sum(
            len(row["hits"]) for row in chapter_hits
        )
        default_message = (
            f"叙述模板「{rule['label']}」在第{worst['start'] if worst else matched_chapters[0]}"
            f"至{worst['end'] if worst else matched_chapters[-1]}章出现{total_matches}次，"
            f"单章最高{max_per_chapter}次；请保留必要事实边界，把同层复述改为行动、"
            "反方质疑或结果差异。"
        )
        conflicts.append({
            "type": "叙述模板重复",
            "severity": rule["severity"],
            "rule_id": rule["id"],
            "template_label": rule["label"],
            "first_chap": worst["start"] if worst else matched_chapters[0],
            "last_chap": worst["end"] if worst else matched_chapters[-1],
            "matched_chapters": matched_chapters,
            "total_matches": total_matches,
            "max_per_chapter": max_per_chapter,
            "per_chapter_limit": rule["per_chapter_max"],
            "window_total_limit": rule["window_total_max"],
            "evidence": evidence,
            "message": rule["message"] or default_message,
        })

    return conflicts


def scan_timeline_jumps(chapters_data):
    """
    检测时间线回档：检查是否有"开学""入冬""春天"等季节词倒序出现。
    """
    # 简单规则：按章节顺序检查时间标记
    time_markers = [
        (r'秋天', 'autumn'),
        (r'入冬|冬天|寒潮|十二.?月|1[12]月', 'winter'),
        (r'入春|开春|春季|三月.*?开学|3月.*?开学|[三四]月.*?份', 'spring'),
        (r'毕业季|毕业典礼|高考.*?结束|六月.*?高考|六月.*?毕业', 'graduation'),
    ]

    timeline = []
    conflicts = []

    def _is_time_metaphor(line):
        metaphor_cues = ("像", "仿佛", "好像", "如同", "似的", "那种")
        return any(cue in line for cue in metaphor_cues)

    for chap_num, title, text in chapters_data:
        for pattern, season in time_markers:
            matched = False
            for line in text.split('\n'):
                if not re.search(pattern, line, re.IGNORECASE):
                    continue
                if season in ("autumn", "winter", "spring") and _is_time_metaphor(line):
                    continue
                matched = True
                break
            if matched:
                timeline.append((chap_num, season, pattern))
                break

    # 检测回档
    season_order = {'autumn': 2, 'winter': 3, 'spring': 4, 'graduation': 5}
    for i in range(1, len(timeline)):
        prev = season_order.get(timeline[i-1][1], 0)
        curr = season_order.get(timeline[i][1], 0)
        if curr < prev:
            conflicts.append({
                'type': '时间线回档',
                'severity': 'HIGH',
                'prev_chap': timeline[i-1][0],
                'prev_season': timeline[i-1][1],
                'curr_chap': timeline[i][0],
                'curr_season': timeline[i][1],
                'message': f"时间线疑似回档: 第{timeline[i-1][0]}章({timeline[i-1][1]}) → 第{timeline[i][0]}章({timeline[i][1]})",
            })

    return conflicts


def load_chapters(output_dir):
    """Load official chapter files as (number, title, text) tuples."""
    chapters = []
    pattern = os.path.join(os.path.abspath(output_dir), "**", "第????章.txt")
    for path in glob.glob(pattern, recursive=True):
        match = re.fullmatch(r"第(\d{4})章\.txt", os.path.basename(path))
        if not match:
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except Exception:
            continue
        first = text.strip().splitlines()[0] if text.strip() else os.path.basename(path)
        chapters.append((int(match.group(1)), first, text))
    chapters.sort(key=lambda item: item[0])
    return chapters


def scan_all(chapters_data):
    """Run every deterministic cross-chapter scanner and return one report."""
    issues = []
    for scanner in (
        scan_events,
        scan_forbidden,
        scan_old_names,
        scan_timeline_jumps,
        scan_rhetorical_templates,
    ):
        issues.extend(scanner(chapters_data))
    return {
        "status": "FAIL" if any(item.get("severity") == "HIGH" for item in issues) else ("WARN" if issues else "PASS"),
        "chapter_count": len(chapters_data),
        "issue_count": len(issues),
        "issues": issues,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="扫描正式章节的跨章一致性")
    parser.add_argument("--output", default=os.path.join(SCRIPT_DIR, "output"), help="章节输出目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)
    reload_project_tool_rules(SCRIPT_DIR)
    report = scan_all(load_chapters(args.output))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"跨章扫描: {report['status']} | "
            f"章节={report['chapter_count']} | 问题={report['issue_count']}"
        )
        for item in report["issues"][:20]:
            print(f"- [{item.get('severity', 'WARN')}] {item.get('message', item.get('type', '未知问题'))}")
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
