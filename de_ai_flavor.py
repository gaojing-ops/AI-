# -*- coding: utf-8 -*-
"""
AI 味清除脚本
================
批量扫描并清理小说章节中的 AI 模板化套话、霸总油腻词汇和翻译腔。
支持自定义目标目录、章节范围。
"""

import os
import sys
import glob
import re

# ---- 规则定义 ----
REPLACEMENTS = [
    # 废话过渡词
    (r'^(然而|但是|不过|其实|毕竟)，', '',),
    (r'不可否认的是，?|毋庸置疑，?|毫无疑问，?', '',),
    # 滥用表情/动作前缀
    (r'不可置信地', '',),
    (r'难以置信地', '',),
    (r'不由得', '',),
    (r'倒吸(了)?一口凉气', '愣住了',),
    (r'深吸(了)?一口气，?', '',),
    (r'倒吸一口冷气', '愣住',),
    # 霸总油腻词
    (r'嘴角(微勾|勾起.*?弧度|勾起.*?笑意)', '笑了笑',),
    (r'深邃的(眼眸|目光|眼神)', '眼睛',),
    (r'漆黑的眸子', '眼睛',),
    (r'狭长的眸子', '眼睛',),
    (r'修长的手指', '手指',),
    # 夸张氛围
    (r'空气仿佛(骤然|瞬间)?(凝固|停滞)了', '四周突然安静下来',),
    (r'陷入了死寂', '安静下来',),
    (r'时间仿佛(骤然|瞬间)?(凝固|静止)了', '所有人停下动作',),
    # 无用模糊词
    (r'某种说不清道不明的', '',),
    (r'带着某种', '带着',),
    (r'像(是)?见(了)?鬼一样', '大惊失色',),
    # 章尾模板套话
    (r'而(她|他)，?还一无所知。', '',),
    (r'命运的齿轮.*?转动', '一切才刚刚开始',),
    (r'一切，?都才刚刚开始。?', '',),
    # 格式清理
    (r' +$', '',),
    (r'\n{3,}', '\n\n',),
    # 额外AI腔（女频校园向）
    (r'不可置信地睁大了眼(睛)?', '睁大了眼睛',),
    (r'不由得愣住(了)?', '愣住',),
    (r'眼神.*?变得.*?深(邃|沉)', '眼神认真起来',),
    (r'故作镇定地', '',),
    (r'声音.*?带着.*?一丝', '声音',),
    (r'不受控制地', '',),
    (r'鬼使神差地', '',),
]

def clean_text(text):
    """对单段文本执行所有替换规则"""
    for pattern, replacement in REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    # 清理行尾空格
    text = re.sub(r' +$', '', text, flags=re.MULTILINE)
    # 压缩三连以上空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def process_chapter(filepath, dry_run=False):
    """处理单个章节文件，返回是否修改"""
    with open(filepath, 'r', encoding='utf-8') as f:
        original = f.read()

    cleaned = clean_text(original)

    if cleaned == original:
        return False

    if not dry_run:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(cleaned)

    return True


def process_all(target_dir=None, start_chap=None, end_chap=None, dry_run=False):
    """
    批量处理章节文件。

    Args:
        target_dir: 章节目录路径，默认为 output/第01卷/
        start_chap: 起始章节号（含），None 表示不限制
        end_chap:   结束章节号（含），None 表示不限制
        dry_run:    True 表示只扫描不修改
    """
    if target_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        target_dir = os.path.join(script_dir, "output", "第01卷")

    if not os.path.isdir(target_dir):
        print(f"[错误] 目录不存在: {target_dir}")
        return 0

    files = sorted(glob.glob(os.path.join(target_dir, "第*.txt")))
    count = 0
    scanned = 0

    for f in files:
        basename = os.path.basename(f)
        match = re.search(r'第(\d+)章', basename)
        if not match:
            continue

        chap_num = int(match.group(1))

        # 章节范围过滤
        if start_chap is not None and chap_num < start_chap:
            continue
        if end_chap is not None and chap_num > end_chap:
            continue

        scanned += 1
        if process_chapter(f, dry_run=dry_run):
            count += 1
            status = " [DRY RUN 将修改]" if dry_run else ""
            print(f"  ✅ 清理: {basename}{status}")
        else:
            print(f"  ⏭ 跳过: {basename} (无需修改)")

    print(f"\n扫描 {scanned} 章，修改 {count} 章")
    if dry_run:
        print("(DRY RUN 模式，未实际写入)")

    return count


def scan_ai_flavor(target_dir=None, start_chap=None, end_chap=None):
    """
    扫描 AI 味关键词并统计分布。
    """
    if target_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        target_dir = os.path.join(script_dir, "output", "第01卷")

    if not os.path.isdir(target_dir):
        print(f"[错误] 目录不存在: {target_dir}")
        return

    files = sorted(glob.glob(os.path.join(target_dir, "第*.txt")))

    keywords = {
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
    }

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
        print("未找到匹配的章节文件。")
        return

    print(f"扫描章节: {chap_count}章")
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
    import argparse

    parser = argparse.ArgumentParser(description="AI味清除与扫描工具")
    parser.add_argument("action", nargs="?", choices=["clean", "scan"], default="scan",
                        help="clean=执行清理, scan=仅扫描统计 (默认: scan)")
    parser.add_argument("--dir", default=None, help="章节目录路径")
    parser.add_argument("--start", type=int, default=None, help="起始章节号")
    parser.add_argument("--end", type=int, default=None, help="结束章节号")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")

    args = parser.parse_args()

    if args.action == "clean":
        process_all(target_dir=args.dir, start_chap=args.start,
                     end_chap=args.end, dry_run=args.dry_run)
    else:
        scan_ai_flavor(target_dir=args.dir, start_chap=args.start,
                       end_chap=args.end)
