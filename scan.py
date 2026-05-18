# -*- coding: utf-8 -*-
"""
AI味扫描器 — 统计章节中的AI模板化关键词残留
================================================
用法：
  python scan.py              # 扫描全部章节
  python scan.py 151 200      # 扫描指定范围
"""

import os
import sys
import re
import glob

sys.stdout.reconfigure(encoding='utf-8')

# 统一关键词集（与 de_ai_flavor.py 保持一致）
AI_FLAVOR_KEYWORDS = {
    '不可置信地': 0,
    '她倒要看看': 0,
    '深吸了一口气': 0,
    '嘴角微勾': 0,
    '空气仿佛凝固': 0,
    '眼神复杂': 0,
    '仿佛被什么击中': 0,
    '不由得': 0,
    '倒吸一口凉气': 0,
    '深邃的眼': 0,
    '陷入了死寂': 0,
    '而她，还一无所知': 0,
    '一切，都才刚刚开始': 0,
    '命运的齿轮': 0,
    '某种说不清': 0,
    '时间，正在悄悄流逝': 0,
}


def scan_chapters(target_dir, start_chap=None, end_chap=None):
    files = sorted(glob.glob(os.path.join(target_dir, "第*.txt")))
    keywords = dict(AI_FLAVOR_KEYWORDS)
    total_chars = 0
    chap_count = 0

    for f in files:
        basename = os.path.basename(f)
        match = re.search(r'第(\d+)章', basename)
        if not match:
            continue

        chap_num = int(match.group(1))
        if start_chap is not None and chap_num < start_chap:
            continue
        if end_chap is not None and chap_num > end_chap:
            continue

        with open(f, 'r', encoding='utf-8') as fh:
            text = fh.read()

        total_chars += len(re.sub(r'\s+', '', text))
        chap_count += 1
        for kw in keywords:
            keywords[kw] += text.count(kw)

    if chap_count == 0:
        print("[错误] 未找到匹配的章节文件")
        return

    if start_chap and end_chap:
        print(f"扫描章节: {start_chap}-{end_chap} ({chap_count}章)")
    else:
        print(f"扫描章节: 全部 ({chap_count}章)")

    print(f"总字数: {total_chars:,}")
    print(f"平均每章: {total_chars // chap_count:,} 字")
    print()
    print("AI味关键词残留:")
    for kw, cnt in sorted(keywords.items(), key=lambda x: -x[1]):
        if cnt <= 2:
            status = '✅'
        elif cnt <= 10:
            status = '⚠️'
        else:
            status = '❌'
        print(f"  {status} \"{kw}\": {cnt} 处")


if __name__ == "__main__":
    # 默认目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_dir = os.path.join(script_dir, "output", "第01卷")

    target_dir = default_dir
    start_chap = None
    end_chap = None

    # 解析参数
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dir_arg = None
    for a in sys.argv[1:]:
        if a.startswith('--dir='):
            dir_arg = a.split('=', 1)[1]
        elif a == '--dir' and sys.argv.index(a) + 1 < len(sys.argv):
            dir_arg = sys.argv[sys.argv.index(a) + 1]

    if dir_arg:
        target_dir = dir_arg

    if len(args) >= 2:
        try:
            start_chap = int(args[0])
            end_chap = int(args[1])
        except ValueError:
            pass
    elif len(args) == 1:
        try:
            # 单个数字：扫描该数字作为区间
            # 如 scan.py 200 扫描150-200
            pass
        except ValueError:
            pass

    scan_chapters(target_dir, start_chap, end_chap)
