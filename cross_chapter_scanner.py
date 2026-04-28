# -*- coding: utf-8 -*-
"""
跨章一致性扫描器 (Cross-Chapter Consistency Scanner)
======================================================
用途：扫描指定章节范围内的所有章节，检测：
  1. 事件重复 — 同一事件在多章中出现
  2. 时间线回档 — 后面章节的事件时间早于前面章节
  3. 禁词出现 — 在不应出现的章节范围出现禁词
  4. 角色名旧称 — 已被改名的旧角色名残留

使用方式：
  python cross_chapter_scanner.py                       # 扫描第01卷全部
  python cross_chapter_scanner.py 151 200               # 扫描指定范围
  python cross_chapter_scanner.py 185 191                # 扫描接口区间
  python cross_chapter_scanner.py --check-events         # 只做事件去重检查
  python cross_chapter_scanner.py --check-forbidden      # 只做禁词检查
"""

import os
import sys
import re
import glob

# ============================================================
# 配置
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# ★ 项目专属配置区 ★
# 以下所有列表需要根据你的小说项目进行定制。
# 将示例条目替换为你自己的事件、禁词和角色名即可。
# ============================================================

# 事件模式库：正则匹配文本 + 事件标签 + 描述
# 格式: (正则, 标签, 描述)
# 注意：正则尽量精准匹配"事件发生时刻"，避免匹配到后续回忆/评论性的提及
EVENT_PATTERNS = [
    # 示例：（请替换为你的项目事件）
    # (r'第.*?次.*?月考.*?前.*?五十', '第一次月考/前五十', '第一次月考排名事件'),
]

# 特殊事件完成章节：该事件在指定章节完成后，后续正常引用不算重复
# {event_label: completion_chapter}
EVENT_COMPLETION_CHAPTERS = {
    # 示例：
    # '槐树命名': 180,
}

# 禁词规则：(关键词, 禁止原因, 允许出现的最小章节号, 0=全禁)
FORBIDDEN_TERMS = [
    # 示例：（请替换为你的项目禁词）
    # ("旧角色名", "已改名为新角色名", 0),
    # ("血腥描写关键词", "违反基调规则", 0),
]

# 章节门控禁词：(模式, 原因, 禁写范围：在该章节之前禁止)
CHAPTER_GATED_TERMS = [
    # 示例：
    # (r'终极真相', "终极真相揭示在第300章", 300),
]

# 旧名前缀（用于精确匹配，避免误杀）
OLD_NAME_PATTERNS = {
    # 示例：
    # "旧名": "新名",
}


# ============================================================
# 核心函数
# ============================================================

def read_chapter(filepath):
    """读取章节文件，返回(章号, 标题, 全文)"""
    if not os.path.exists(filepath):
        return None, None, None

    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    # 提取章号
    basename = os.path.basename(filepath)
    match = re.search(r'(\d{4})', basename)
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
    files = sorted(glob.glob(pattern))

    if start is not None and end is not None:
        filtered = []
        for f in files:
            basename = os.path.basename(f)
            match = re.search(r'(\d{4})', basename)
            if match:
                num = int(match.group(1))
                if start <= num <= end:
                    filtered.append(f)
        return sorted(filtered)

    return files


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
        # 已完成事件的后续正常引用不算重复
        completion_chap = EVENT_COMPLETION_CHAPTERS.get(label, 0)
        first = occurrences[0]
        for i, occ in enumerate(occurrences[1:], 1):
            if occ['chap'] == first['chap']:
                continue
            if completion_chap and first['chap'] >= completion_chap:
                continue  # 事件已在completion_chap章完成，后续出现是正常引用
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
                # 特殊处理"沈家"：如果上下文是"沈瑶家"则跳过
                if keyword == "沈家":
                    ctx_start = max(0, m.start() - 5)
                    ctx_end = min(len(text), m.end() + 5)
                    ctx = text[ctx_start:ctx_end]
                    if "沈瑶家" in ctx or "沈瑶" in ctx:
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


def scan_timeline_jumps(chapters_data):
    """
    检测时间线回档：检查是否有"开学""入冬""春天"等季节词倒序出现。
    """
    # 简单规则：按章节顺序检查时间标记
    time_markers = [
        (r'开学.*?第.*?[一二三1-3]周', 'early_term'),
        (r'秋天|银杏|落叶', 'autumn'),
        (r'入冬|冬天|寒潮|十二.?月|1[12]月', 'winter'),
        (r'入春|春天.*?来了|春天.*?到|三月.*?开学|3月.*?开学|[三四]月.*?份', 'spring'),
        (r'毕业|高考.*?结束|六.?月', 'graduation'),
    ]

    timeline = []
    conflicts = []

    for chap_num, title, text in chapters_data:
        for pattern, season in time_markers:
            if re.search(pattern, text, re.IGNORECASE):
                timeline.append((chap_num, season, pattern))
                break

    # 检测回档
    season_order = {'early_term': 1, 'autumn': 2, 'winter': 3, 'spring': 4, 'graduation': 5}
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


# ============================================================
# 主入口
# ============================================================

def main():
    # 解析参数
    check_events = '--check-events' not in sys.argv
    check_forbidden = '--check-forbidden' not in sys.argv
    all_checks = True

    if '--check-events' in sys.argv or '--check-forbidden' in sys.argv:
        all_checks = False
        check_events = '--check-events' in sys.argv
        check_forbidden = '--check-forbidden' in sys.argv

    # 解析章号范围
    start_chap = None
    end_chap = None
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(args) >= 2:
        try:
            start_chap = int(args[0])
            end_chap = int(args[1])
        except ValueError:
            pass

    # 目录
    volume_dir = os.path.join(SCRIPT_DIR, "output", "第01卷")
    if not os.path.isdir(volume_dir):
        print(f"[错误] 未找到章节目录: {volume_dir}")
        return

    # 读取章节
    files = get_chapter_files(volume_dir, start_chap, end_chap)
    if not files:
        print(f"[错误] 未找到任何章节文件")
        return

    chapters_data = []
    for fp in files:
        chap, title, text = read_chapter(fp)
        if chap is not None:
            chapters_data.append((chap, title, text))

    if not chapters_data:
        print("[错误] 无法读取任何章节")
        return

    chapters_data.sort(key=lambda x: x[0])

    print("=" * 70)
    print(f"  跨章一致性扫描: 第{chapters_data[0][0]}章 → 第{chapters_data[-1][0]}章 ({len(chapters_data)}章)")
    print("=" * 70)

    all_conflicts = []

    # 事件去重扫描
    if check_events or all_checks:
        print("\n[1/4] 事件去重扫描...")
        event_conflicts = scan_events(chapters_data)
        all_conflicts.extend(event_conflicts)
        if event_conflicts:
            for c in event_conflicts:
                print(f"  [FAIL] {c['message']}")
                print(f"      首次: 第{c['first_chap']}章 — {c['first_context']}")
                print(f"      重复: 第{c['dup_chap']}章 — {c['dup_context']}")
        else:
            print("  [OK] 无事件重复")

    # 禁词扫描
    if check_forbidden or all_checks:
        print("\n[2/4] 禁词/超前揭示扫描...")
        forbidden_conflicts = scan_forbidden(chapters_data)
        all_conflicts.extend(forbidden_conflicts)
        if forbidden_conflicts:
            for c in forbidden_conflicts:
                icon = "[HIGH]" if c['severity'] == 'HIGH' else "[MED]"
                print(f"  {icon} {c['message']}")
                print(f"      上下文: ...{c['context']}...")
        else:
            print("  [OK] 无禁词")

    # 旧名扫描
    if check_forbidden or all_checks:
        print("\n[3/4] 旧角色名扫描...")
        name_conflicts = scan_old_names(chapters_data)
        all_conflicts.extend(name_conflicts)
        if name_conflicts:
            for c in name_conflicts:
                print(f"  [FAIL] {c['message']}")
        else:
            print("  [OK] 无旧名残留")

    # 时间线扫描
    if check_events or all_checks:
        print("\n[4/4] 时间线回档扫描...")
        timeline_conflicts = scan_timeline_jumps(chapters_data)
        all_conflicts.extend(timeline_conflicts)
        if timeline_conflicts:
            for c in timeline_conflicts:
                print(f"  [FAIL] {c['message']}")
        else:
            print("  [OK] 时间线无回档")

    # 总结
    print("\n" + "=" * 70)
    high_count = len([c for c in all_conflicts if c.get('severity') == 'HIGH'])
    med_count = len([c for c in all_conflicts if c.get('severity') == 'MEDIUM'])

    if not all_conflicts:
        print("  [OK] 扫描通过！未发现任何冲突。")
    else:
        print(f"  [WARN] 发现 {len(all_conflicts)} 个问题 ({high_count} 个高优先级, {med_count} 个中优先级)")
    print("=" * 70)

    return all_conflicts


if __name__ == "__main__":
    main()
