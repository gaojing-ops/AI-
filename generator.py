# -*- coding: utf-8 -*-
"""
通用长篇小说生成器 - 模块化上下文长篇小说系统
=================================================
设计目标：解决长篇小说的设定记忆、上下文控制和断点续写问题。
使用方法：
    1. 在 config.json 中配置 API Key
    2. 运行脚本：python generator.py
"""

import os
import sys

# Load the bundled runtime dependencies before importing OpenAI.  This keeps
# direct module use and the double-click GUI launcher self-contained.
if not getattr(sys, "frozen", False):
    _LOCAL_PACKAGES = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".tools",
        "python-packages",
    )
    if os.path.isdir(_LOCAL_PACKAGES) and _LOCAL_PACKAGES not in sys.path:
        sys.path.insert(0, _LOCAL_PACKAGES)

import json
import re
from datetime import datetime
from openai import OpenAI
import glob

# ============================================================
# 系统基础配置
# ============================================================
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
DEFAULT_PROJECT_DIR = os.path.join(SCRIPT_DIR, "projects", "未命名项目")

# 目录结构映射
DIRS = {
    "world": os.path.join(DEFAULT_PROJECT_DIR, "world_building"),
    "chars": os.path.join(DEFAULT_PROJECT_DIR, "characters"),
    "plot":  os.path.join(DEFAULT_PROJECT_DIR, "plot"),
    "out":   os.path.join(DEFAULT_PROJECT_DIR, "output"),
    "hist":  os.path.join(DEFAULT_PROJECT_DIR, "history"),
    "logs":  os.path.join(DEFAULT_PROJECT_DIR, "logs"),
    "publish": os.path.join(DEFAULT_PROJECT_DIR, "publish"),
}

# 确保目录和配置文件存在
for d in DIRS.values():
    os.makedirs(d, exist_ok=True)

def default_config():
    """返回一份不会落盘的本机默认配置。"""
    return {
        "model_provider": "deepseek",
        "codex_executable": "",
        "codex_timeout_seconds": 900,
        "api_key": "YOUR_DEEPSEEK_API_KEY_HERE",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "review_model": "deepseek-v4-pro",
        "temperature": 0.8,
        "max_tokens": 8192,
        "current_volume": 1,
        "author_name": "匿名作者"
    }


def load_config():
    """加载本机配置；缺失或字段不全时使用内存默认值补齐。"""
    defaults = default_config()
    if not os.path.exists(CONFIG_FILE):
        return defaults
        
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config.json 顶层必须是对象")
            defaults.update(loaded)
            return defaults
    except Exception as e:
        print(f"[错误] 无法加载配置 config.json: {e}")
        return defaults

config = load_config()


def get_runtime_api_key(config_data=None):
    """环境变量优先，但绝不把环境变量密钥写回持久化配置。"""
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    saved_key = str((config_data or config).get("api_key") or "").strip()
    if saved_key == "YOUR_DEEPSEEK_API_KEY_HERE":
        return ""
    return saved_key

# ============================================================
# 核心功能模块
# ============================================================

def read_text_safe(filepath: str) -> str:
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def read_text_exact(filepath: str) -> str:
    """Read persisted text without trimming evidence-bearing whitespace."""
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8-sig", newline=None) as f:
            return f.read()
    except Exception:
        return ""

# ============================================================
# 一致性自动检查模块
# ============================================================

# 通用底座默认不携带任何旧书专属规则。
# 项目级规则从 plot/tool_rules.json 读取。
FORBIDDEN_KEYWORDS = {}
EARLY_FORBIDDEN_RULES = []
WATCH_KEYWORDS = []

def _normalize_rule_list(items):
    result = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, str):
            result[item] = "项目禁词"
        elif isinstance(item, list) and len(item) >= 2:
            result[str(item[0])] = str(item[1])
        elif isinstance(item, dict) and item.get("keyword"):
            result[str(item["keyword"])] = str(item.get("reason", "项目禁词"))
    return result

def _load_project_tool_rules():
    global FORBIDDEN_KEYWORDS, EARLY_FORBIDDEN_RULES, WATCH_KEYWORDS
    # Rules are project-scoped. Clear the previous project's values before
    # attempting to load the new file, including the missing/invalid-file path.
    FORBIDDEN_KEYWORDS = {}
    EARLY_FORBIDDEN_RULES = []
    WATCH_KEYWORDS = []

    rules_path = os.path.join(DIRS["plot"], "tool_rules.json")
    if not os.path.exists(rules_path):
        return
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
    except Exception:
        return

    FORBIDDEN_KEYWORDS.update(_normalize_rule_list(rules.get("banned_keywords")))
    scanner_forbidden = rules.get("scanner_forbidden_terms")
    if isinstance(scanner_forbidden, list) and scanner_forbidden:
        merged = {}
        for item in scanner_forbidden:
            if isinstance(item, list) and len(item) >= 2:
                pattern = str(item[0])
                # generator 的快检是字面量包含；正则规则保留给 cross_chapter_scanner。
                if any(ch in pattern for ch in ".*?[]()|+"):
                    continue
                merged[pattern] = str(item[1])
        if merged:
            FORBIDDEN_KEYWORDS.update(merged)

    gated = rules.get("chapter_gated_terms")
    if isinstance(gated, list):
        for item in gated:
            if isinstance(item, list) and len(item) >= 3:
                try:
                    EARLY_FORBIDDEN_RULES.append((int(item[2] or 0), {str(item[0]): str(item[1])}))
                except Exception:
                    continue

    for key in ("watch_keywords", "high_risk_watch_keywords", "style_risk_keywords"):
        values = rules.get(key)
        if isinstance(values, list):
            WATCH_KEYWORDS.extend(str(v) for v in values if str(v).strip())

def reload_project_tool_rules():
    _load_project_tool_rules()

_load_project_tool_rules()

def _split_nonempty_paragraphs(text: str) -> list:
    return [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

def run_consistency_check(chapter_text: str, chapter_num: int) -> list:
    """对新生成的章节做关键词一致性扫描，返回问题列表"""
    issues = []
    lines = chapter_text.split('\n')

    # 1. 检查禁止关键词
    for kw, reason in FORBIDDEN_KEYWORDS.items():
        for i, line in enumerate(lines, 1):
            if kw in line:
                issues.append({
                    "level": "[BAN] 禁止",
                    "keyword": kw,
                    "line": i,
                    "context": line.strip()[:60],
                    "reason": reason,
                })

    # 检查早期禁止关键词：这些词汇在指定章节前严格禁止出现，
    # 在指定章节之后同样需要检查（防止后续章节与已揭示的真相矛盾）。
    # 如果某条规则在当前章节已超出限制（超出+10章缓冲期），
    # 则不再标记为[BAN]禁止，改为[WARN]注意（因为可能是回忆/补充说明）。
    for chapter_limit, keyword_map in EARLY_FORBIDDEN_RULES:
        for kw, reason in keyword_map.items():
            for i, line in enumerate(lines, 1):
                if kw in line:
                    # 计算是否已过"安全期"：超过限制章节+10章后，改为[WARN]注意而非[BAN]禁止
                    is_violation = chapter_num < (chapter_limit + 10)
                    issue_level = "[BAN] 禁止" if is_violation else "[WARN] 注意"
                    # 在[BAN]禁止时标注具体限制理由，在[WARN]注意时标注已过限制期
                    detail = reason if is_violation else f"{reason}（注：已超出第{chapter_limit}章限制期，"
                    detail += "若为本章新揭示真相则正常，否则请确认上下文）" if not is_violation else ""
                    issues.append({
                        "level": issue_level,
                        "keyword": kw,
                        "line": i,
                        "context": line.strip()[:60],
                        "reason": detail,
                    })

    # 1.5 检查 Markdown 残留
    markdown_patterns = [
        (r'\*\*.+?\*\*', "检测到 **加粗** Markdown 残留"),
        (r'__.+?__', "检测到 __加粗__ Markdown 残留"),
        (r'^\s*#{1,6}\s+', "检测到 # 标题 Markdown 残留"),
        (r'^\s*[-*]\s+', "检测到列表 Markdown 残留"),
        (r'`[^`]+`', "检测到反引号 Markdown 残留"),
    ]
    for i, line in enumerate(lines, 1):
        for pattern, reason in markdown_patterns:
            if re.search(pattern, line):
                issues.append({
                    "level": "[BAN] 禁止",
                    "keyword": "Markdown残留",
                    "line": i,
                    "context": line.strip()[:60],
                    "reason": reason,
                })
                break

    # 2. 检查监控关键词（提醒人工确认）
    for kw in WATCH_KEYWORDS:
        hits = [(i, line.strip()[:60]) for i, line in enumerate(lines, 1) if kw in line]
        if hits:
            issues.append({
                "level": "[WARN] 注意",
                "keyword": kw,
                "line": hits[0][0],
                "context": hits[0][1],
                "reason": f"关键词'{kw}'出现{len(hits)}次，请人工确认是否与设定表一致",
            })

    # 3. 检查章节长度异常
    char_count = len(chapter_text)
    if char_count < 1500:
        issues.append({"level": "[WARN] 注意", "keyword": "字数过少", "line": 0,
                       "context": f"本章仅{char_count}字", "reason": "低于1500字下限"})
    elif char_count > 5000:
        issues.append({"level": "[WARN] 注意", "keyword": "字数过多", "line": 0,
                       "context": f"本章{char_count}字", "reason": "超过5000字上限"})

    # 4. 检查章尾是否出现“锤句堆叠”的重度 AI 腔
    tail_text = chapter_text[-1400:] if len(chapter_text) > 1400 else chapter_text
    tail_paras = _split_nonempty_paragraphs(tail_text)
    short_run = 0
    max_short_run = 0
    best_run = []
    current_run = []
    for para in tail_paras:
        compact = para.replace("\n", "").strip()
        if compact and len(compact) <= 18:
            short_run += 1
            current_run.append(compact[:18])
            if short_run > max_short_run:
                max_short_run = short_run
                best_run = list(current_run)
        else:
            short_run = 0
            current_run = []
    if max_short_run >= 6:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "章尾短句堆叠",
            "line": 0,
            "context": " / ".join(best_run[:4]),
            "reason": f"章尾连续短段达到{max_short_run}段，容易形成明显AI切句感",
        })

    # 5. 检查章尾破折号是否被拿来代替节奏与悬念
    tail_dash_count = tail_text.count("——")
    if tail_dash_count >= 6:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "章尾破折号过密",
            "line": 0,
            "context": f"章尾检测到{tail_dash_count}个破折号",
            "reason": "章尾破折号过多，容易出现刻意停顿和摆姿势感",
        })

    # 6. 检查全章模糊意象词是否过密
    vague_count = chapter_text.count("某种") + chapter_text.count("像是")
    if vague_count >= 18:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "模糊词过密",
            "line": 0,
            "context": f"'某种/像是'累计{vague_count}次",
            "reason": "模糊意象词过密，容易让行文显得虚和AI味偏重",
        })

    # 7. 检查高频停顿模板。只标风格风险，不触发整章重写。
    stall_phrases = ("没有回答", "没有说话", "沉默了几秒")
    stall_count = sum(chapter_text.count(phrase) for phrase in stall_phrases)
    if stall_count >= 4:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "对白停顿模板过密",
            "line": 0,
            "context": f"没有回答/没有说话/沉默了几秒累计{stall_count}次",
            "reason": "同类对白停顿反复出现，应用具体动作、回避对象或现场声音替换",
        })

    breath_count = chapter_text.count("深吸一口气")
    if breath_count >= 2:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "深呼吸模板过密",
            "line": 0,
            "context": f"深吸一口气出现{breath_count}次",
            "reason": "同一章重复用深呼吸切换情绪，容易形成模板感",
        })

    sudden_count = chapter_text.count("猛地") + chapter_text.count("忽然")
    if sudden_count > 4:
        issues.append({
            "level": "[WARN] 风格",
            "keyword": "突发副词过密",
            "line": 0,
            "context": f"猛地/忽然累计{sudden_count}次",
            "reason": "突发副词过密会削弱真正转折的力度",
        })

    return issues

def list_files_in_dir(directory: str) -> dict:
    """返回目录下所有 txt 文件的 {文件名(不含扩展名): 完整路径} 映射"""
    files_map = {}
    pattern = os.path.join(directory, "*.txt")
    for file in glob.glob(pattern):
        name = os.path.splitext(os.path.basename(file))[0]
        files_map[name] = file
    return files_map

_OFFICIAL_CHAPTER_RE = re.compile(r"^第(\d{1,6})章\.txt$")
_VOLUME_DIR_RE = re.compile(r"^第(\d+)卷$")


def _load_project_volume_ranges():
    """Load validated volume ranges from the active project's config."""
    project_root = os.path.dirname(DIRS["out"])
    config_path = os.path.join(project_root, "project_config.json")
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception:
        return []

    normalized = []
    for item in payload.get("volume_ranges") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            start = int(item[0])
            end = int(item[1])
        except (TypeError, ValueError):
            continue
        if start <= 0 or end < start:
            continue
        normalized.append((start, end))
    normalized.sort(key=lambda item: item[0])
    return normalized


def _volume_index_for_chapter(chapter_num, volume_ranges):
    for index, (start, end) in enumerate(volume_ranges, start=1):
        if start <= chapter_num <= end:
            return index
    return 0


def _scan_official_chapters(out_dir):
    """Return official chapters only; drafts/check files are never checkpoints."""
    rows = []
    if not os.path.isdir(out_dir):
        return rows
    for root_dir, dirs, files in os.walk(out_dir):
        dirs[:] = [name for name in dirs if name != ".backup"]
        for filename in files:
            match = _OFFICIAL_CHAPTER_RE.fullmatch(filename)
            if match:
                rows.append((int(match.group(1)), os.path.join(root_dir, filename)))
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows


def _is_consistent_flat_layout(rows, out_dir):
    """A legacy/simple project may intentionally keep every chapter in output/."""
    if not rows:
        return False
    root = os.path.normcase(os.path.abspath(out_dir))
    return all(
        os.path.normcase(os.path.abspath(os.path.dirname(path))) == root
        for _, path in rows
    )


def audit_chapter_layout():
    """Report gaps, duplicates, and inconsistent configured-volume placement."""
    rows = _scan_official_chapters(DIRS["out"])
    by_chapter = {}
    for chapter, path in rows:
        by_chapter.setdefault(chapter, []).append(path)
    duplicates = {
        chapter: paths for chapter, paths in by_chapter.items() if len(paths) > 1
    }
    max_chapter = max(by_chapter, default=0)
    missing = [chapter for chapter in range(1, max_chapter + 1) if chapter not in by_chapter]

    ranges = _load_project_volume_ranges()
    misplaced = []
    # A fully flat output directory is a supported layout for simple/legacy
    # projects.  Only enforce volume folders after a project actually uses them;
    # this prevents a harmless import layout from blocking chapter 31.
    if ranges and not _is_consistent_flat_layout(rows, DIRS["out"]):
        for chapter, path in rows:
            expected_index = _volume_index_for_chapter(chapter, ranges)
            if not expected_index:
                misplaced.append((chapter, path, "未配置分卷范围"))
                continue
            expected_dir = f"第{expected_index:02d}卷"
            actual_dir = os.path.basename(os.path.dirname(path))
            if actual_dir != expected_dir:
                misplaced.append((chapter, path, expected_dir))
    return {
        "max_chapter": max_chapter,
        "missing": missing,
        "duplicates": duplicates,
        "misplaced": misplaced,
    }


def get_latest_chapter_info() -> tuple:
    """Return the global checkpoint and the configured output volume for next chapter.

    Chapter numbers are global across the book. Scanning only the highest volume
    directory made chapter 141 continue inside volume 01 and allowed draft files
    to select an empty later volume. Exact official filenames are now scanned
    recursively, then ``volume_ranges`` determines the next chapter's directory.
    """
    out_dir = DIRS["out"]
    chapter_rows = _scan_official_chapters(out_dir)
    latest_chap = chapter_rows[-1][0] if chapter_rows else 0
    latest_filepath = chapter_rows[-1][1] if chapter_rows else None
    next_chap = latest_chap + 1

    volume_ranges = _load_project_volume_ranges()
    target_vol = _volume_index_for_chapter(next_chap, volume_ranges)
    if target_vol == 0 and latest_filepath:
        match = _VOLUME_DIR_RE.fullmatch(os.path.basename(os.path.dirname(latest_filepath)))
        if match:
            target_vol = int(match.group(1))
    if target_vol == 0:
        try:
            target_vol = max(1, int(config.get("current_volume", 1) or 1))
        except (TypeError, ValueError):
            target_vol = 1

    config["current_volume"] = target_vol
    flat_layout = _is_consistent_flat_layout(chapter_rows, out_dir)
    vol_dir = (
        out_dir
        if flat_layout
        else os.path.join(out_dir, f"第{target_vol:02d}卷")
    )
    os.makedirs(vol_dir, exist_ok=True)
    filepath = os.path.join(vol_dir, f"第{next_chap:04d}章.txt")
    return target_vol, next_chap, filepath, latest_chap, latest_filepath

def init_demo_files():
    """确保所有必要的子文件夹存在（不再创建演示文件）"""
    for d in DIRS.values():
        os.makedirs(d, exist_ok=True)
