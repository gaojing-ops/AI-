# -*- coding: utf-8 -*-
"""Build the human-readable project status from the formal chapter folders.

The markdown status file is informational only.  Formal progress is derived
from output/ and publish/ so an old hand-written status note cannot make a
finished project look resumable.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


_OFFICIAL_CHAPTER_RE = re.compile(r"^第(\d{1,6})章\.txt$")


def _scan_chapters(directory: Path) -> dict[int, list[Path]]:
    rows: dict[int, list[Path]] = {}
    if not directory.is_dir():
        return rows
    for path in directory.rglob("*.txt"):
        match = _OFFICIAL_CHAPTER_RE.fullmatch(path.name)
        if match:
            rows.setdefault(int(match.group(1)), []).append(path)
    return rows


def _numbers_summary(rows: Mapping[int, list[Path]]) -> tuple[list[int], list[int], list[int]]:
    numbers = sorted(int(number) for number in rows)
    duplicates = sorted(number for number, paths in rows.items() if len(paths) > 1)
    missing = [number for number in range(1, max(numbers, default=0) + 1) if number not in rows]
    return numbers, missing, duplicates


def _chapter_summary(label: str, rows: Mapping[int, list[Path]]) -> str:
    numbers, missing, duplicates = _numbers_summary(rows)
    if not numbers:
        return f"{label}：暂无正式章节"
    details = f"{label}：第 1—{numbers[-1]} 章，共 {len(numbers)} 章"
    if numbers[0] != 1:
        details += f"；起始章节为第 {numbers[0]} 章"
    if missing:
        details += "；缺号：" + "、".join(str(number) for number in missing[:20])
    if duplicates:
        details += "；重复文件：" + "、".join(str(number) for number in duplicates[:20])
    return details


def render_status(project_dir: str | Path, config: Mapping[str, Any] | None = None, *, now: str | None = None) -> str:
    """Return an authoritative, deterministic-enough project status document."""

    root = Path(project_dir)
    settings = config or {}
    output_rows = _scan_chapters(root / "output")
    publish_rows = _scan_chapters(root / "publish")
    output_numbers, output_missing, output_duplicates = _numbers_summary(output_rows)
    publish_numbers, publish_missing, publish_duplicates = _numbers_summary(publish_rows)

    try:
        target = int(settings.get("target_total_chapters", 0) or 0)
    except (TypeError, ValueError):
        target = 0

    expected = set(range(1, target + 1)) if target > 0 else set()
    output_set = set(output_numbers)
    publish_set = set(publish_numbers)
    complete = bool(
        target > 0
        and output_set == expected
        and publish_set == expected
        and not output_duplicates
        and not publish_duplicates
    )
    latest = max(output_numbers, default=0)
    next_chapter = latest + 1
    if complete:
        completion = f"已完结（正式正文与发布稿均完整到第 {target} 章；禁止生成第 {target + 1} 章）"
    else:
        completion = f"未完结；下一候选章节为第 {next_chapter} 章"

    if output_set == publish_set and not output_missing and not publish_missing:
        sync = "output/ 与 publish/ 章节范围一致且连续"
    else:
        sync = "output/ 与 publish/ 章节范围存在差异，需先核对"

    staging_rows = _scan_chapters(root / "rewrite_staging")
    staging_numbers, _, _ = _numbers_summary(staging_rows)
    title = str(settings.get("book_title") or root.name)
    model = str(settings.get("model") or "未配置")
    review_model = str(settings.get("review_model") or model)
    timestamp = now or datetime.now().isoformat(timespec="seconds")

    lines = [
        "# 当前状态（工具自动同步）",
        "",
        f"- 状态更新时间：{timestamp}",
        f"- 项目：{title}",
        f"- 目标章节：{target or '未配置'}",
        f"- {_chapter_summary('正式正文 output/', output_rows)}",
        f"- {_chapter_summary('发布正文 publish/', publish_rows)}",
        f"- 完结判定：{completion}",
        f"- 章节同步：{sync}",
        f"- 早期候选稿 rewrite_staging/：{len(staging_numbers)} 章；不作为正式进度",
        f"- 生成模型：{model}",
        f"- 审查模型：{review_model}",
        "- 正式进度以 output/ 与 publish/ 的实际章节文件为准；本文件不是续写授权来源。",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["render_status"]
