# -*- coding: utf-8 -*-
"""Low-frequency commercial review for unattended long-form generation."""

import json
import os
import re
import hashlib
from datetime import datetime
from difflib import SequenceMatcher


OFFICIAL_CHAPTER_RE = re.compile(r"^第(\d{1,6})章\.txt$")
DEFAULT_MILESTONES = (3, 10, 30)
DEFAULT_HARD_GATES = (3, 30)
REVISION_DEBT_SCHEMA_VERSION = 1
REVIEW_SEVERITIES = {"BLOCKER", "REVISION", "ADVISORY"}
REVIEW_RECEIPT_VERSION = 1
MULTI_REVIEW_RECEIPT_VERSION = 2


def _int_list(values, fallback=DEFAULT_MILESTONES):
    result = []
    for value in values or fallback:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return sorted(result)


def volume_end_chapters(volume_ranges):
    ends = []
    for item in volume_ranges or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            end = int(item[1])
        except (TypeError, ValueError):
            continue
        if end > 0:
            ends.append(end)
    return sorted(set(ends))


def hard_gate_chapters(config, volume_ranges=None):
    """Return checkpoints that require an explicit PASS/CONTINUE decision."""
    config = config or {}
    gates = set(_int_list(config.get("commercial_hard_gate_chapters"), DEFAULT_HARD_GATES))
    if config.get("golden_three_require_explicit_continue", True):
        gates.add(max(1, int(config.get("golden_three_gate_chapter", 3) or 3)))
    if config.get("commercial_review_volume_end", True):
        gates.update(volume_end_chapters(volume_ranges))
    return sorted(gates)


def is_hard_gate(chapter_num, config, volume_ranges=None):
    return int(chapter_num or 0) in hard_gate_chapters(config, volume_ranges)


def hard_gate_debt_blocked(chapter_num, result, debt, config, volume_ranges=None):
    """A hard gate cannot pass while earlier commercial actions remain unverified."""
    chapter_num = int(chapter_num or 0)
    if not is_hard_gate(chapter_num, config, volume_ranges):
        return False
    open_items = [
        item for item in (debt or {}).get("items", [])
        if item.get("status") == "OPEN"
        and int(item.get("opened_chapter") or 0) < chapter_num
        and (
            not int(item.get("due_by") or 0)
            or int(item.get("due_by") or 0) <= chapter_num
        )
    ]
    if not open_items:
        return False
    return str((result or {}).get("debt_status") or "PARTIAL").upper() != "RESOLVED"


def should_run(chapter_num, config, volume_ranges=None):
    if not (config or {}).get("commercial_review_enabled", True):
        return False
    chapter_num = int(chapter_num or 0)
    if is_hard_gate(chapter_num, config, volume_ranges):
        return True
    milestones = _int_list((config or {}).get("commercial_review_milestones"))
    if chapter_num in milestones:
        return True
    try:
        interval = int((config or {}).get("commercial_review_interval", 0) or 0)
    except (TypeError, ValueError):
        interval = 0
    if interval > 0 and chapter_num % interval == 0:
        return True
    return bool(
        (config or {}).get("commercial_review_volume_end", True)
        and chapter_num in volume_end_chapters(volume_ranges)
    )


def should_pause_after_review(
    chapter_num,
    status,
    action,
    config,
    *,
    review_unavailable=False,
    volume_ranges=None,
):
    """Fail closed at configured hard gates and on unavailable stage review."""
    config = config or {}
    if review_unavailable:
        return bool(config.get("commercial_review_pause_on_unavailable", True))
    normalized_status = str(status or "").upper()
    normalized_action = str(action or "").upper()
    if (
        config.get("commercial_hard_gate_require_explicit_continue", True)
        and is_hard_gate(chapter_num, config, volume_ranges)
    ):
        return not (
            normalized_status == "PASS" and normalized_action == "CONTINUE"
        )
    return bool(
        config.get("commercial_review_pause_on_fail", True)
        and (
            normalized_status == "FAIL"
            or normalized_action == "PAUSE"
        )
    )


def _clip_keep_ends(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = max(300, limit * 2 // 5)
    tail = max(300, limit - head)
    return text[:head].rstrip() + "\n[中段已截断]\n" + text[-tail:].lstrip()


def collect_recent_chapters(output_dir, chapter_num, current_content="", lookback=30, max_chars=120000):
    rows = {}
    if os.path.isdir(output_dir):
        for root_dir, dirs, files in os.walk(output_dir):
            dirs[:] = [name for name in dirs if name != ".backup"]
            for filename in files:
                match = OFFICIAL_CHAPTER_RE.fullmatch(filename)
                if not match:
                    continue
                number = int(match.group(1))
                if not max(1, chapter_num - lookback + 1) <= number <= chapter_num:
                    continue
                try:
                    with open(os.path.join(root_dir, filename), "r", encoding="utf-8-sig") as f:
                        rows[number] = f.read().strip()
                except Exception:
                    continue
    if current_content:
        rows[int(chapter_num)] = current_content.strip()

    selected = []
    used = 0
    for number in sorted(rows, reverse=True):
        text = rows[number]
        block = f"===== 第{number}章全文 =====\n{text}"
        if selected and used + len(block) > max_chars:
            break
        if not selected and len(block) > max_chars:
            block = f"===== 第{number}章全文 =====\n{_clip_keep_ends(text, max_chars - 40)}"
        selected.append((number, block))
        used += len(block)
    selected.reverse()
    return selected


def collect_expected_chapters(
    output_dir,
    expected_chapters,
    *,
    current_chapter=0,
    current_content="",
):
    """Load an exact hard-gate range without silently clipping old chapters."""
    expected = [int(item) for item in expected_chapters or []]
    wanted = set(expected)
    rows = {}
    if os.path.isdir(output_dir):
        for root_dir, dirs, files in os.walk(output_dir):
            dirs[:] = [name for name in dirs if name != ".backup"]
            for filename in files:
                match = OFFICIAL_CHAPTER_RE.fullmatch(filename)
                if not match:
                    continue
                number = int(match.group(1))
                if number not in wanted:
                    continue
                try:
                    with open(
                        os.path.join(root_dir, filename),
                        "r",
                        encoding="utf-8-sig",
                    ) as handle:
                        rows[number] = handle.read().strip()
                except Exception:
                    continue
    if current_content and int(current_chapter or 0) in wanted:
        rows[int(current_chapter)] = str(current_content).strip()
    missing = [number for number in expected if not rows.get(number)]
    if missing:
        raise RuntimeError(
            "硬闸门商业审稿缺少第"
            + "、".join(str(item) for item in missing[:20])
            + "章"
        )
    return [
        (number, f"===== 第{number}章全文 =====\n{rows[number]}")
        for number in expected
    ]


def partition_review_blocks(blocks, max_chars):
    """Split a complete range into lossless, chapter-aligned review chunks."""
    budget = max(24000, int(max_chars or 0))
    chunks = []
    current = []
    used = 0
    for number, block in blocks or []:
        size = len(block)
        if size > budget:
            raise RuntimeError(f"第{number}章单章超过商业审稿正文预算，拒绝截断")
        if current and used + size > budget:
            chunks.append(current)
            current = []
            used = 0
        current.append((int(number), block))
        used += size
    if current:
        chunks.append(current)
    return chunks


def build_prompts(
    chapter_num,
    chapter_blocks,
    story_context="",
    canon_context="",
    commercial_context="",
    hard_gate=False,
    map_scope=False,
):
    system_prompt = (
        "你是长篇商业网文的阶段总编，不负责润色句子，只判断作品是否值得继续扩大生产。"
        "请同时采用四个视角：平台编辑、普通追读读者、长篇结构编辑、AI模板痕迹审查。"
        "必须区分可从正文验证的事实和对市场表现的推测，不得虚构阅读数据。"
        "硬伤包括：核心卖点迟迟不兑现、连续重复同类冲突、主角目标模糊、人物声音同质化、"
        "黄金三章缺钩子、十章仍未建立稳定追读问题、三十章仍没有差异化高潮。"
    )
    coverage = [int(number) for number, _block in (chapter_blocks or [])]
    if hard_gate and map_scope:
        coverage_label = "、".join(str(number) for number in coverage)
        gate_rule = (
            "\n- 当前是硬闸门的证据分块，不是最终全区间裁决。"
            f"本次只覆盖第{coverage_label}章；只判断这些章节自身是否存在商业硬伤。"
            "不得因为没有提供其他分块章节、当前硬闸门终章或跨分块债务证据而降低"
            "OVERALL、CURRENT、ACTION。需要其他分块才能核验的债务，在DEBT_EVIDENCE中"
            "明确写交由最终汇总核验。分块自身若存在身份、时间线、拼接、重复或其他结构硬伤，"
            "仍必须FAIL/PAUSE，不能放行。"
        )
        review_request = (
            f"请审查硬闸门证据分块（第{coverage_label}章）的商业可读性与局部硬伤。"
            f"全区间截至第{chapter_num}章的最终裁决由后续汇总完成。"
        )
    else:
        review_request = f"请审查截至第{chapter_num}章的商业可读性与继续生产价值。"
        gate_rule = (
        "\n- 当前是硬闸门：只有截至本章已经到期的修订债务已有正文证据、且可以无条件继续时，"
        "才能输出 PASS/CONTINUE；否则必须输出 FAIL/PAUSE。"
        "尚未到 opened_chapter 或 due_by 的未来债务不阻断本次 PASS/CONTINUE；"
        "此时 DEBT_STATUS 仍写 RESOLVED，表示当前闸门没有到期欠账，并在 DEBT_EVIDENCE 中以 OPEN 标明未到执行区间。"
        "硬闸门审查的是整个覆盖区间：任何历史章节仍有身份、死因、时间线、拼接、元话语或债务硬伤，"
        "CURRENT 也必须输出 FAIL，不能以问题不是最新章新增为由放行。"
        if hard_gate else ""
        )
    user_prompt = f"""{review_request}

【故事契约】
{story_context or '无'}

【商业立项与上轮行动】
{commercial_context or '无'}

【当前正史】
{canon_context or '无'}

【阶段正文】
{chr(10).join(block for _, block in chapter_blocks)}

严格按以下格式输出：
OVERALL: PASS/WARN/FAIL
CURRENT: PASS/FAIL
ACTION: CONTINUE/ADJUST/PAUSE
SUMMARY: 一句话结论
STRENGTHS:
- 最多3条，必须引用正文中可验证的表现
RISKS:
- 最多5条，说明具体章节或反复出现的模式
NEXT_10_CHAPTERS:
- 给出3条可执行调整，不得推翻正史和既定真相节奏
DEBT_STATUS: RESOLVED
DEBT_EVIDENCE:
- 对上轮行动单逐条说明正文证据，格式必须是“debt_id|VERIFIED|第N章具体证据”或“debt_id|OPEN|未兑现/部分兑现/未到执行区间的原因”；只有 VERIFIED 会核销债务；首次审稿写“首次建立”

判定规则：
- PASS：可以按现方向继续扩大生产。
- WARN：可以继续，但下一批必须调整，当前章不必整章重写。
- FAIL：继续批量会扩大结构性问题，候选章只能保存为待审草稿，不能写入正式目录。
- 非硬闸门时，CURRENT只判断第{chapter_num}章是否新增致命问题；最终硬闸门按整个覆盖区间裁决；证据分块只裁决本分块。
{gate_rule}
"""
    return system_prompt, user_prompt


def build_reduce_prompts(
    chapter_num,
    chunk_results,
    *,
    story_context="",
    canon_context="",
    commercial_context="",
):
    """Build the final decision from lossless chapter-level map reviews."""
    system_prompt = (
        "你是长篇商业网文硬闸门的最终总编。下方每个分块都由自动审稿模型基于完整正文审查，"
        "你必须综合所有分块，不得把某一块的硬伤用另一块的优点抵消。"
        "只要任一分块是WARN、FAIL、ADJUST、PAUSE、结构不完整，最终必须FAIL/PAUSE；"
        "只有所有分块均明确PASS/CURRENT PASS/CONTINUE，且截至本章已经到期的修订债务逐项有正文证据，"
        "最终才可PASS/CONTINUE。不得虚构未出现在分块报告中的阅读数据或正文证据。"
        "尚未到 opened_chapter 或 due_by 的未来债务不阻断本次PASS/CONTINUE；"
        "DEBT_STATUS仍写RESOLVED表示当前闸门没有到期欠账，未来项在DEBT_EVIDENCE中标为OPEN。"
    )
    reports = []
    for index, item in enumerate(chunk_results or [], 1):
        coverage = "、".join(str(number) for number in item.get("reviewed_chapters", []))
        reports.append(
            f"===== 分块{index}（第{coverage}章）=====\n{item.get('raw') or ''}"
        )
    user_prompt = f"""请对截至第{int(chapter_num)}章作最终硬闸门裁决。

【故事契约】
{story_context or '无'}

【商业立项与上轮行动】
{commercial_context or '无'}

【当前正史】
{canon_context or '无'}

【完整分块审查】
{chr(10).join(reports)}

严格按以下格式输出：
OVERALL: PASS/WARN/FAIL
CURRENT: PASS/FAIL
ACTION: CONTINUE/ADJUST/PAUSE
SUMMARY: 一句话结论
STRENGTHS:
- 最多3条
RISKS:
- 最多5条，标明章节
NEXT_10_CHAPTERS:
- 3条可执行调整
DEBT_STATUS: RESOLVED
DEBT_EVIDENCE:
- 每条格式必须是“debt_id|VERIFIED|第N章具体证据”或“debt_id|OPEN|未兑现/部分兑现/未到执行区间的原因”；只有 VERIFIED 会核销债务；首次审稿写“首次建立”
"""
    return system_prompt, user_prompt


def parse_review(raw):
    text = (raw or "").strip()
    if text.startswith("{"):
        duplicate_keys = []

        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    duplicate_keys.append(str(key))
                result[key] = value
            return result

        try:
            payload = json.loads(text, object_pairs_hook=unique_pairs)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and not duplicate_keys:
            overall = str(payload.get("OVERALL") or "").strip().upper()
            current = str(payload.get("CURRENT") or "").strip().upper()
            action = str(payload.get("ACTION") or "").strip().upper()
            debt_status = str(payload.get("DEBT_STATUS") or "").strip().upper()
            summary = str(payload.get("SUMMARY") or "").strip()
            actions_in = payload.get("NEXT_10_CHAPTERS")
            evidence_in = payload.get("DEBT_EVIDENCE")
            next_actions = [
                str(item).strip()
                for item in (actions_in if isinstance(actions_in, list) else [])
                if str(item).strip()
            ]
            debt_evidence = [
                str(item).strip()
                for item in (evidence_in if isinstance(evidence_in, list) else [])
                if str(item).strip()
            ]
            malformed = not (
                overall in {"PASS", "WARN", "FAIL"}
                and current in {"PASS", "FAIL"}
                and action in {"CONTINUE", "ADJUST", "PAUSE"}
                and debt_status == "RESOLVED"
                and bool(summary)
                and bool(debt_evidence)
            )
            return {
                "status": overall if not malformed else "FAIL",
                "current": current if not malformed else "FAIL",
                "action": action if not malformed else "PAUSE",
                "summary": summary or text[:300],
                "next_actions": next_actions[:5],
                "debt_status": debt_status or "PARTIAL",
                "debt_evidence": debt_evidence[:8],
                "raw": text,
                "malformed": malformed,
            }
    terminal_labels = {
        name: re.findall(rf"(?mi)^\s*{name}\s*:", text)
        for name in ("OVERALL", "CURRENT", "ACTION", "DEBT_STATUS")
    }
    overall_matches = re.findall(r"(?mi)^\s*OVERALL:\s*(PASS|WARN|FAIL)\s*$", text)
    current_matches = re.findall(r"(?mi)^\s*CURRENT:\s*(PASS|FAIL)\s*$", text)
    action_matches = re.findall(r"(?mi)^\s*ACTION:\s*(CONTINUE|ADJUST|PAUSE)\s*$", text)
    debt_matches = re.findall(
        r"(?mi)^\s*DEBT_STATUS:\s*(RESOLVED|PARTIAL|FAILED)\s*$", text
    )
    summary = re.search(r"(?mi)^\s*SUMMARY:\s*(.+)$", text)
    next_actions = []
    action_section = re.search(
        r"(?mis)^\s*NEXT_10_CHAPTERS:\s*(.*?)(?=^\s*[A-Z_]+:\s*|\Z)",
        text,
    )
    if action_section:
        for line in action_section.group(1).splitlines():
            item = re.sub(r"^\s*[-*\d.、)）]+\s*", "", line).strip()
            if item and item not in next_actions:
                next_actions.append(item)
    debt_evidence = []
    debt_section = re.search(
        r"(?mis)^\s*DEBT_EVIDENCE:\s*(.*?)(?=^\s*[A-Z_]+:\s*|\Z)",
        text,
    )
    if debt_section:
        for line in debt_section.group(1).splitlines():
            item = re.sub(r"^\s*[-*\d.、)）]+\s*", "", line).strip()
            if item and item not in debt_evidence:
                debt_evidence.append(item)
    terminal_unique = all(
        len(matches) == 1
        for matches in (overall_matches, current_matches, action_matches, debt_matches)
    ) and all(len(matches) == 1 for matches in terminal_labels.values())
    debt_resolved = len(debt_matches) == 1 and debt_matches[0].upper() == "RESOLVED"
    malformed = not (
        terminal_unique and summary and debt_resolved and bool(debt_evidence)
    )
    return {
        "status": overall_matches[0].upper() if len(overall_matches) == 1 and not malformed else "FAIL",
        "current": current_matches[0].upper() if len(current_matches) == 1 and not malformed else "FAIL",
        "action": action_matches[0].upper() if len(action_matches) == 1 and not malformed else "PAUSE",
        "summary": summary.group(1).strip() if summary else text[:300],
        "next_actions": next_actions[:5],
        "debt_status": debt_matches[0].upper() if len(debt_matches) == 1 else "PARTIAL",
        "debt_evidence": debt_evidence[:8],
        "raw": text,
        "malformed": malformed,
    }


def _receipt_digest(receipt):
    material = {k: v for k, v in dict(receipt or {}).items() if k != "receipt_sha256"}
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def chapter_hashes_for_blocks(blocks):
    hashes = {}
    for number, block in blocks or []:
        text = str(block or "").split("\n", 1)[-1]
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        hashes[str(int(number))] = hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
    return hashes


def build_review_receipt(
    system_prompt,
    user_prompt,
    raw,
    model_name,
    reviewed_chapters,
    chapter_hashes=None,
):
    model = str(model_name or "").strip()
    if not model or "manual" in model.lower():
        raise RuntimeError("商业审稿必须来自已配置的自动审查模型")
    receipt = {
        "version": REVIEW_RECEIPT_VERSION,
        "origin": "automatic_model_review",
        "model": model,
        "reviewed_chapters": [int(item) for item in reviewed_chapters],
        "chapter_hashes": dict(chapter_hashes or {}),
        "system_prompt_sha256": hashlib.sha256(str(system_prompt or "").encode("utf-8")).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(str(user_prompt or "").encode("utf-8")).hexdigest(),
        "response_sha256": hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest(),
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return receipt


def _review_stage_receipt(kind, system_prompt, user_prompt, raw, model_name, chapters):
    return {
        "kind": str(kind),
        "model": str(model_name or "").strip(),
        "reviewed_chapters": [int(item) for item in chapters or []],
        "system_prompt_sha256": hashlib.sha256(
            str(system_prompt or "").encode("utf-8")
        ).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(
            str(user_prompt or "").encode("utf-8")
        ).hexdigest(),
        "response_sha256": hashlib.sha256(
            str(raw or "").encode("utf-8")
        ).hexdigest(),
    }


def build_multi_review_receipt(
    model_name,
    reviewed_chapters,
    stages,
    chapter_hashes=None,
):
    model = str(model_name or "").strip()
    if not model or "manual" in model.lower():
        raise RuntimeError("商业审稿必须来自已配置的自动审查模型")
    receipt = {
        "version": MULTI_REVIEW_RECEIPT_VERSION,
        "origin": "automatic_model_review_map_reduce",
        "model": model,
        "reviewed_chapters": [int(item) for item in reviewed_chapters or []],
        "chapter_hashes": dict(chapter_hashes or {}),
        "stages": list(stages or []),
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return receipt


def verify_review_receipt(result, model_name=""):
    if (
        bool((result or {}).get("malformed"))
        or str((result or {}).get("debt_status") or "").upper() != "RESOLVED"
        or not [str(item).strip() for item in ((result or {}).get("debt_evidence") or []) if str(item).strip()]
    ):
        raise RuntimeError("商业审稿债务终态必须唯一、为 RESOLVED 且包含证据")
    receipt = (result or {}).get("review_receipt")
    if not isinstance(receipt, dict):
        raise RuntimeError("商业审稿缺少自动审查调用收据")
    version = int(receipt.get("version") or 0)
    if version not in {REVIEW_RECEIPT_VERSION, MULTI_REVIEW_RECEIPT_VERSION}:
        raise RuntimeError("商业审稿调用收据版本无效")
    valid_origins = {
        REVIEW_RECEIPT_VERSION: "automatic_model_review",
        MULTI_REVIEW_RECEIPT_VERSION: "automatic_model_review_map_reduce",
    }
    if receipt.get("origin") != valid_origins[version]:
        raise RuntimeError("人工回填不能形成商业审稿 PASS")
    persisted_model = str(model_name or "").strip()
    receipt_model = str(receipt.get("model") or "").strip()
    if (
        not persisted_model
        or "manual" in persisted_model.lower()
        or receipt_model != persisted_model
    ):
        raise RuntimeError("商业审稿模型收据不一致")
    reviewed = [int(item) for item in ((result or {}).get("reviewed_chapters") or [])]
    if receipt.get("reviewed_chapters") != reviewed:
        raise RuntimeError("商业审稿覆盖章节与调用收据不一致")
    result_hashes = {
        str(key): str(value)
        for key, value in dict((result or {}).get("chapter_hashes") or {}).items()
    }
    receipt_hashes = {
        str(key): str(value)
        for key, value in dict(receipt.get("chapter_hashes") or {}).items()
    }
    if set(result_hashes) != {str(item) for item in reviewed}:
        raise RuntimeError("商业审稿缺少逐章正文哈希")
    if receipt_hashes != result_hashes:
        raise RuntimeError("商业审稿正文哈希与调用收据不一致")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in result_hashes.values()
    ):
        raise RuntimeError("商业审稿逐章正文哈希无效")
    if version == REVIEW_RECEIPT_VERSION:
        for field in (
            "system_prompt_sha256",
            "user_prompt_sha256",
            "response_sha256",
            "receipt_sha256",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field) or "")):
                raise RuntimeError(f"商业审稿调用收据字段无效：{field}")
        if receipt.get("response_sha256") != hashlib.sha256(
            str((result or {}).get("raw") or "").encode("utf-8")
        ).hexdigest():
            raise RuntimeError("商业审稿正文与调用收据不一致")
    else:
        stages = receipt.get("stages")
        if not isinstance(stages, list) or len(stages) < 2:
            raise RuntimeError("分块商业审稿收据缺少map/reduce阶段")
        map_coverage = []
        map_stages = []
        reduce_count = 0
        for stage in stages:
            if not isinstance(stage, dict):
                raise RuntimeError("分块商业审稿阶段收据无效")
            if str(stage.get("model") or "") != receipt_model:
                raise RuntimeError("分块商业审稿阶段模型不一致")
            kind = str(stage.get("kind") or "")
            if kind == "map":
                map_stages.append(stage)
                map_coverage.extend(
                    int(item) for item in (stage.get("reviewed_chapters") or [])
                )
            elif kind == "reduce":
                reduce_count += 1
                if [
                    int(item) for item in (stage.get("reviewed_chapters") or [])
                ] != reviewed:
                    raise RuntimeError(
                        "商业审稿 reduce 阶段没有完整覆盖审核章节"
                    )
            else:
                raise RuntimeError("分块商业审稿阶段类型无效")
            for field in (
                "system_prompt_sha256",
                "user_prompt_sha256",
                "response_sha256",
            ):
                if not re.fullmatch(r"[0-9a-f]{64}", str(stage.get(field) or "")):
                    raise RuntimeError(f"分块商业审稿收据字段无效：{field}")
        if map_coverage != reviewed or reduce_count != 1 or stages[-1].get("kind") != "reduce":
            raise RuntimeError("分块商业审稿没有完整、顺序一致地覆盖硬闸门")
        if stages[-1].get("response_sha256") != hashlib.sha256(
            str((result or {}).get("raw") or "").encode("utf-8")
        ).hexdigest():
            raise RuntimeError("商业审稿汇总结论与调用收据不一致")
        map_reviews = (result or {}).get("map_reviews")
        if not isinstance(map_reviews, list) or len(map_reviews) != len(map_stages):
            raise RuntimeError("分块商业审稿缺少可独立复核的原始结论")
        any_map_blocked = False
        for stage, map_review in zip(map_stages, map_reviews):
            if not isinstance(map_review, dict):
                raise RuntimeError("分块商业审稿结论结构无效")
            raw = str(map_review.get("raw") or "")
            if stage.get("response_sha256") != hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest():
                raise RuntimeError("分块商业审稿原始响应与调用收据不一致")
            coverage = [
                int(item) for item in (map_review.get("reviewed_chapters") or [])
            ]
            if coverage != [
                int(item) for item in (stage.get("reviewed_chapters") or [])
            ]:
                raise RuntimeError("分块商业审稿覆盖区间被修改")
            parsed_map = parse_review(raw)
            for field in ("status", "current", "action"):
                if str(map_review.get(field) or "").upper() != str(
                    parsed_map.get(field) or ""
                ).upper():
                    raise RuntimeError("分块商业审稿结构字段与原始结论不一致")
            if parsed_map.get("malformed"):
                raise RuntimeError("分块商业审稿原始结论结构无效")
            any_map_blocked = any_map_blocked or (
                parsed_map.get("status") != "PASS"
                or parsed_map.get("current") != "PASS"
                or parsed_map.get("action") != "CONTINUE"
            )
        if any_map_blocked and (
            str((result or {}).get("status") or "").upper() == "PASS"
            and str((result or {}).get("current") or "").upper() == "PASS"
            and str((result or {}).get("action") or "").upper() == "CONTINUE"
        ):
            raise RuntimeError("硬闸门汇总结论覆盖了分块失败")
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("receipt_sha256") or "")):
            raise RuntimeError("商业审稿调用收据字段无效：receipt_sha256")
    if receipt.get("receipt_sha256") != _receipt_digest(receipt):
        raise RuntimeError("商业审稿调用收据已被修改")

    parsed = parse_review((result or {}).get("raw") or "")
    for field in ("status", "current", "action"):
        if str((result or {}).get(field) or "").upper() != str(parsed.get(field) or "").upper():
            raise RuntimeError("商业审稿结构字段与模型原始结论不一致")
    if (
        str((result or {}).get("debt_status") or "").upper()
        != str(parsed.get("debt_status") or "").upper()
        or list((result or {}).get("debt_evidence") or [])
        != list(parsed.get("debt_evidence") or [])
    ):
        raise RuntimeError("商业审稿债务字段与模型原始结论不一致")
    if bool((result or {}).get("malformed")) != bool(parsed.get("malformed")):
        raise RuntimeError("商业审稿结构状态与模型原始结论不一致")


def expected_hard_gate_chapters(
    chapter_num,
    lookback,
    volume_ranges=None,
    target_total_chapters=0,
):
    chapter_num = int(chapter_num)
    target_total = int(target_total_chapters or 0)
    if target_total > 0 and chapter_num == target_total:
        return list(range(1, target_total + 1))
    for item in volume_ranges or []:
        try:
            start, end = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if chapter_num == end:
            return list(range(start, end + 1))
    return list(range(max(1, chapter_num - int(lookback) + 1), chapter_num + 1))


def _atomic_write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def _review_id(chapter_num, result, reviewed_chapters, model_name=""):
    raw = json.dumps(
        {
            "chapter": int(chapter_num),
            "raw": result.get("raw") or "",
            "reviewed_chapters": list(reviewed_chapters or []),
            "model": model_name or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "review_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def save_report(report_dir, chapter_num, result, reviewed_chapters, model_name=""):
    review_id = str(result.get("review_id") or "").strip() or _review_id(
        chapter_num, result, reviewed_chapters, model_name=model_name
    )
    result["review_id"] = review_id
    path = os.path.join(
        report_dir, f"commercial_review_ch{chapter_num:04d}_{review_id}.md"
    )
    header = (
        f"# 第{chapter_num}章阶段商业审稿\n\n"
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"- 模型：{model_name or '未记录'}\n"
        f"- 覆盖章节：{', '.join(str(item) for item in reviewed_chapters)}\n"
        f"- 结论：{result.get('status')} / {result.get('action')}\n\n"
    )
    if result.get("gate_blocked"):
        header += (
            f"- 系统硬闸门：BLOCKED\n"
            f"- 阻止原因：{result.get('gate_block_reason') or '修订债务未核销'}\n\n"
        )
    _atomic_write(path, header + (result.get("raw") or ""))
    latest = {
        "chapter": chapter_num,
        "review_id": review_id,
        "status": result.get("status"),
        "current": result.get("current"),
        "action": result.get("action"),
        "summary": result.get("summary"),
        "raw": result.get("raw") or "",
        "next_actions": result.get("next_actions") or [],
        "debt_status": result.get("debt_status", "PARTIAL"),
        "debt_evidence": result.get("debt_evidence") or [],
        "gate_blocked": bool(result.get("gate_blocked")),
        "gate_block_reason": str(result.get("gate_block_reason") or ""),
        "model": model_name,
        "reviewed_chapters": reviewed_chapters,
        "expected_chapters": result.get("expected_chapters") or reviewed_chapters,
        "chapter_hashes": result.get("chapter_hashes") or {},
        "map_reviews": result.get("map_reviews") or [],
        "review_receipt": result.get("review_receipt") or {},
        "malformed": bool(result.get("malformed")),
        "report_path": path,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write(
        os.path.join(report_dir, "latest_commercial_review.json"),
        json.dumps(latest, ensure_ascii=False, indent=2),
    )
    return path


def persist_review(report_dir, chapter_num, result, model_name=""):
    """Persist an already-computed review after its chapter is official.

    The deterministic review id makes crash recovery idempotent: replaying the
    same reviewed draft overwrites the same report instead of fabricating a
    second milestone review.
    """
    reviewed = list(result.get("reviewed_chapters") or [])
    if not reviewed:
        raise RuntimeError("商业审稿缺少覆盖章节，拒绝落盘")
    if result.get("malformed"):
        raise RuntimeError("商业审稿结构无效，拒绝落盘")
    expected = [int(item) for item in (result.get("expected_chapters") or reviewed)]
    if [int(item) for item in reviewed] != expected:
        raise RuntimeError("商业审稿未完整覆盖硬闸门章节，拒绝落盘")
    verify_review_receipt(result, model_name=model_name)
    result["report_path"] = save_report(
        report_dir,
        int(chapter_num),
        result,
        reviewed,
        model_name=model_name,
    )
    return result


def save_action_plan(path, chapter_num, result):
    """Write the latest review directions into the generation context."""
    actions = result.get("next_actions") or []
    if not actions and result.get("summary"):
        actions = [result.get("summary")]
    lines = [
        f"# 第{int(chapter_num)}章后商业行动单",
        "",
        f"结论：{result.get('status', 'WARN')} / {result.get('action', 'ADJUST')}",
        f"级别：{classify_review_severity(result, hard_gate=bool(result.get('hard_gate')))}",
        f"摘要：{result.get('summary', '')}",
        "",
        "下一批必须执行：",
    ]
    lines.extend(f"- {item}" for item in actions[:5])
    lines.extend([
        "",
        "边界：不得推翻正史、唯一真相和已经落地的不可逆事件。",
        "通过下一次阶段商业审稿后，本文件会自动替换。",
        "",
    ])
    _atomic_write(path, "\n".join(lines))
    return path


def _normalize_action(text):
    return re.sub(r"[\s\W_]+", "", str(text or "").lower(), flags=re.UNICODE)


_ACTION_WINDOW_RE = re.compile(r"第\s*(\d+)\s*(?:[—–－\-至到]\s*(\d+)\s*)?章")


def _action_window(text):
    match = _ACTION_WINDOW_RE.search(str(text or ""))
    if not match:
        return None
    start = int(match.group(1))
    return start, int(match.group(2) or start)


def _actions_semantically_duplicate(left, right):
    """Conservatively merge paraphrases aimed at the same explicit chapter window."""
    left_window = _action_window(left)
    right_window = _action_window(right)
    if left_window is None or left_window != right_window:
        return False
    left_text = _normalize_action(_ACTION_WINDOW_RE.sub("", str(left or ""), count=1))
    right_text = _normalize_action(_ACTION_WINDOW_RE.sub("", str(right or ""), count=1))
    if not left_text or not right_text:
        return False
    ratio = SequenceMatcher(None, left_text, right_text).ratio()

    def bigrams(value):
        return {value[index:index + 2] for index in range(max(0, len(value) - 1))}

    left_pairs = bigrams(left_text)
    right_pairs = bigrams(right_text)
    common_pairs = len(left_pairs & right_pairs)
    overlap = common_pairs / max(1, len(left_pairs | right_pairs))
    return (
        ratio >= 0.55
        or overlap >= 0.36
        or (common_pairs >= 6 and overlap >= 0.15)
    )


def classify_review_severity(result, hard_gate=False):
    """Map legacy model verdicts to stable local workflow severity."""
    result = result if isinstance(result, dict) else {}
    explicit = str(result.get("severity") or "").strip().upper()
    if explicit in REVIEW_SEVERITIES:
        return explicit
    status = str(result.get("status") or "WARN").strip().upper()
    action = str(result.get("action") or "ADJUST").strip().upper()
    if result.get("gate_blocked"):
        return "BLOCKER"
    if hard_gate and (status == "FAIL" or action == "PAUSE"):
        return "BLOCKER"
    if status in {"WARN", "FAIL", "UNAVAILABLE"} or action in {
        "ADJUST", "PAUSE", "UNAVAILABLE",
    }:
        return "REVISION"
    return "ADVISORY"


def _debt_id(chapter_num, action):
    raw = f"{int(chapter_num)}|{_normalize_action(action)}"
    return "debt_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _debt_evidence_status(row, debt_id):
    """Return an explicit per-debt status from one evidence row.

    Evidence is intentionally fail-closed.  Mentioning a debt id inside a
    sentence such as "未到核销区间" must never verify it. An explicit
    status without any evidence text must not verify it either.
    """
    parts = [part.strip() for part in str(row or "").split("|", 2)]
    if len(parts) != 3 or parts[0] != str(debt_id or "") or not parts[2]:
        return ""
    status = parts[1].upper()
    return status if status in {"OPEN", "VERIFIED"} else ""


class RevisionDebtError(RuntimeError):
    """An existing debt ledger is unreadable or unsafe to use."""


def _validate_revision_debt(data):
    if not isinstance(data, dict):
        raise ValueError("顶层必须是对象")
    if (type(data.get("schema_version")) is not int
            or data["schema_version"] != REVISION_DEBT_SCHEMA_VERSION):
        raise ValueError("不支持的schema_version")
    for field in ("items", "review_history"):
        if not isinstance(data.get(field), list):
            raise ValueError(f"{field}必须是数组")
    seen = set()
    for index, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}]必须是对象")
        for field in ("id", "action"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"items[{index}].{field}必须是非空文本")
        if item["id"] in seen:
            raise ValueError(f"items[{index}]债务ID重复")
        seen.add(item["id"])
        if item.get("status") not in {"OPEN", "VERIFIED", "SUPERSEDED"}:
            raise ValueError(f"items[{index}].status无效")
        for field in ("opened_chapter", "due_by"):
            if type(item.get(field)) is not int or item[field] < 0:
                raise ValueError(f"items[{index}].{field}必须是非负整数")
    if any(not isinstance(row, dict) for row in data["review_history"]):
        raise ValueError("review_history条目必须是对象")


def load_revision_debt(path):
    """Only a missing file is initial state; corruption must never erase debt."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {
            "schema_version": REVISION_DEBT_SCHEMA_VERSION,
            "updated_at": "",
            "last_review_chapter": 0,
            "items": [],
            "review_history": [],
        }
    except (OSError, UnicodeError, ValueError) as exc:
        raise RevisionDebtError(f"修订债务账本无法读取：{path}（{exc}）") from exc
    try:
        _validate_revision_debt(data)
    except (ValueError, TypeError) as exc:
        raise RevisionDebtError(f"修订债务账本结构无效：{path}（{exc}）") from exc
    return data


def update_revision_debt(path, chapter_num, result, horizon=10):
    """Track whether milestone review actions were actually paid down later."""
    chapter_num = int(chapter_num)
    data = load_revision_debt(path)
    review_id = str(result.get("review_id") or "").strip()
    if review_id and any(
        str(row.get("review_id") or "") == review_id
        for row in data.get("review_history", [])
    ):
        return data
    items = data.setdefault("items", [])
    debt_status = str(result.get("debt_status") or "PARTIAL").upper()
    evidence = result.get("debt_evidence") or []

    for item in items:
        if item.get("status") != "OPEN":
            continue
        item["last_reviewed_chapter"] = chapter_num
        item["last_review_evidence"] = evidence[:8]
        item_id = str(item.get("id") or "")
        item_evidence = [
            str(row)
            for row in evidence
            if _debt_evidence_status(row, item_id)
        ]
        verified_evidence = [
            row
            for row in item_evidence
            if _debt_evidence_status(row, item_id) == "VERIFIED"
        ]
        opened_chapter = int(item.get("opened_chapter") or 0)
        if (
            debt_status == "RESOLVED"
            and chapter_num >= opened_chapter
            and verified_evidence
        ):
            item["status"] = "VERIFIED"
            item["closed_chapter"] = chapter_num
            item["last_review_evidence"] = verified_evidence[:8]

    open_by_action = {
        _normalize_action(item.get("action")): item
        for item in items
        if item.get("status") == "OPEN"
    }
    for action in result.get("next_actions") or []:
        action = str(action or "").strip()
        normalized = _normalize_action(action)
        if not normalized:
            continue
        if normalized in open_by_action:
            existing = open_by_action[normalized]
            # Repeating the same action is evidence that the debt remains
            # unresolved, not a reason to move its deadline forever.
            existing["repeat_count"] = int(existing.get("repeat_count") or 1) + 1
            existing["last_seen_chapter"] = chapter_num
            continue
        semantic_match = next(
            (
                item
                for item in items
                if item.get("status") == "OPEN"
                and _actions_semantically_duplicate(item.get("action"), action)
            ),
            None,
        )
        if semantic_match is not None:
            semantic_match["repeat_count"] = int(
                semantic_match.get("repeat_count") or 1
            ) + 1
            semantic_match["last_seen_chapter"] = chapter_num
            alternates = semantic_match.setdefault("alternate_actions", [])
            if action != semantic_match.get("action") and action not in alternates:
                alternates.append(action)
                del alternates[5:]
            continue
        item = {
            "id": _debt_id(chapter_num, action),
            "action": action,
            "status": "OPEN",
            "opened_chapter": chapter_num,
            "due_by": chapter_num + max(3, int(horizon)),
            "last_reviewed_chapter": chapter_num,
            "last_review_evidence": [],
            "repeat_count": 1,
            "last_seen_chapter": chapter_num,
        }
        items.append(item)
        open_by_action[normalized] = item

    history = data.setdefault("review_history", [])
    history_row = {
        "chapter": chapter_num,
        "review_id": review_id,
        "status": result.get("status"),
        "action": result.get("action"),
        "severity": classify_review_severity(
            result, hard_gate=bool(result.get("hard_gate"))
        ),
        "debt_status": debt_status,
        "evidence": evidence[:8],
        "reviewed_chapters": result.get("reviewed_chapters") or [],
        "report_path": result.get("report_path", ""),
    }
    history = [
        row for row in history
        if not review_id or str(row.get("review_id") or "") != review_id
    ]
    history.append(history_row)
    data["review_history"] = history[-100:]
    # The history cap must not silently retire unfinished work. Retain all
    # open debts plus the most recent closed/superseded records, in order.
    recent_closed_ids = {
        item["id"] for item in [row for row in items if row.get("status") != "OPEN"][-200:]
    }
    data["items"] = [
        item for item in items
        if item.get("status") == "OPEN" or item["id"] in recent_closed_ids
    ]
    data["last_review_chapter"] = chapter_num
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def render_revision_debt(data, current_chapter=0, max_chars=2600):
    open_items = [item for item in (data or {}).get("items", []) if item.get("status") == "OPEN"]
    if not open_items:
        return ""
    open_items.sort(key=lambda item: (int(item.get("due_by") or 10**9), int(item.get("opened_chapter") or 0)))
    lines = [
        "# 未结阶段修订债务",
        "这些不是建议，而是下一次阶段商业审稿必须核销的执行项。不得靠一句说明假装完成。",
    ]
    for item in open_items[:12]:
        due_by = int(item.get("due_by") or 0)
        overdue = "｜已逾期" if current_chapter and due_by and current_chapter > due_by else ""
        not_due = "｜未到期" if current_chapter and due_by and current_chapter < due_by else ""
        lines.append(
            f"- {item.get('id')}｜第{item.get('opened_chapter')}章提出｜最迟第{item.get('due_by')}章："
            f"{item.get('action')}{overdue}{not_due}"
        )
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 16].rstrip() + "\n[修订债务已截断]"


def run_review(
    output_dir,
    report_dir,
    chapter_num,
    current_content,
    config,
    volume_ranges,
    story_context,
    canon_context,
    llm_call,
    model_name="",
    commercial_context="",
    persist=True,
    llm_retry=None,
):
    lookback = max(3, int((config or {}).get("commercial_review_lookback", 30) or 30))
    max_chars = max(24000, int((config or {}).get("commercial_review_fulltext_chars", 120000) or 120000))
    hard_gate = is_hard_gate(chapter_num, config, volume_ranges)
    if hard_gate:
        expected = expected_hard_gate_chapters(
            chapter_num,
            lookback,
            volume_ranges,
            target_total_chapters=int(
                (config or {}).get("target_total_chapters", 0) or 0
            ),
        )
        blocks = collect_expected_chapters(
            output_dir,
            expected,
            current_chapter=int(chapter_num),
            current_content=current_content,
        )
        chunks = partition_review_blocks(blocks, max_chars)
    else:
        blocks = collect_recent_chapters(
            output_dir,
            int(chapter_num),
            current_content=current_content,
            lookback=lookback,
            max_chars=max_chars,
        )
        if not blocks:
            raise RuntimeError("没有可供商业审稿的正式章节")
        expected = [number for number, _ in blocks]
        chunks = [blocks]

    reviewed = [number for number, _ in blocks]
    if reviewed != expected:
        raise RuntimeError("商业审稿章节覆盖与期望区间不一致")
    chapter_hashes = chapter_hashes_for_blocks(blocks)

    def call_and_parse(system_prompt, user_prompt):
        raw_response = llm_call(system_prompt, user_prompt)
        parsed_response = parse_review(raw_response)
        used_system = system_prompt
        used_user = user_prompt
        if parsed_response.get("malformed") and llm_retry:
            used_system = (
                system_prompt
                + "\n\n上一次输出未满足固定字段格式。只重新输出规定字段，"
                "不得增加前言、代码块或省略终端标签。"
            )
            raw_response = llm_retry(used_system, used_user)
            parsed_response = parse_review(raw_response)
        return used_system, used_user, raw_response, parsed_response

    if hard_gate and len(chunks) > 1:
        chunk_results = []
        stages = []
        for index, chunk in enumerate(chunks, 1):
            coverage = [number for number, _ in chunk]
            system_prompt, user_prompt = build_prompts(
                int(chapter_num),
                chunk,
                story_context=story_context,
                canon_context=canon_context,
                commercial_context=(
                    (commercial_context or "")
                    + f"\n\n当前是硬闸门分块 {index}/{len(chunks)}，"
                    "只审所提供章节；任何硬伤都必须如实判定。"
                ),
                hard_gate=True,
                map_scope=True,
            )
            system_prompt, user_prompt, raw, parsed = call_and_parse(
                system_prompt, user_prompt
            )
            if parsed.get("malformed"):
                raise RuntimeError(f"硬闸门第{index}个分块审稿结构无效")
            parsed["reviewed_chapters"] = coverage
            chunk_results.append(parsed)
            stages.append(
                _review_stage_receipt(
                    "map",
                    system_prompt,
                    user_prompt,
                    raw,
                    model_name,
                    coverage,
                )
            )

        reduce_system, reduce_user = build_reduce_prompts(
            int(chapter_num),
            chunk_results,
            story_context=story_context,
            canon_context=canon_context,
            commercial_context=commercial_context,
        )
        reduce_system, reduce_user, final_raw, result = call_and_parse(
            reduce_system, reduce_user
        )
        if result.get("malformed"):
            raise RuntimeError("硬闸门汇总审稿结构无效")
        any_chunk_blocked = any(
            item.get("status") != "PASS"
            or item.get("current") != "PASS"
            or item.get("action") != "CONTINUE"
            for item in chunk_results
        )
        if any_chunk_blocked and (
            result.get("status") == "PASS"
            and result.get("current") == "PASS"
            and result.get("action") == "CONTINUE"
        ):
            raise RuntimeError("硬闸门汇总试图覆盖分块失败，已拒绝放行")
        stages.append(
            _review_stage_receipt(
                "reduce",
                reduce_system,
                reduce_user,
                final_raw,
                model_name,
                reviewed,
            )
        )
        result["review_receipt"] = build_multi_review_receipt(
            model_name,
            reviewed,
            stages,
            chapter_hashes=chapter_hashes,
        )
        result["map_reviews"] = [
            {
                "reviewed_chapters": item.get("reviewed_chapters") or [],
                "status": item.get("status"),
                "current": item.get("current"),
                "action": item.get("action"),
                "summary": item.get("summary"),
                "raw": item.get("raw") or "",
            }
            for item in chunk_results
        ]
    else:
        system_prompt, user_prompt = build_prompts(
            int(chapter_num),
            blocks,
            story_context=story_context,
            canon_context=canon_context,
            commercial_context=commercial_context,
            hard_gate=hard_gate,
        )
        system_prompt, user_prompt, raw, result = call_and_parse(
            system_prompt, user_prompt
        )
        result["review_receipt"] = build_review_receipt(
            system_prompt,
            user_prompt,
            raw,
            model_name,
            reviewed,
            chapter_hashes=chapter_hashes,
        )

    result["reviewed_chapters"] = reviewed
    result["expected_chapters"] = expected
    result["chapter_hashes"] = chapter_hashes
    result["review_id"] = _review_id(
        int(chapter_num), result, reviewed, model_name=model_name
    )
    result["report_path"] = ""
    if persist:
        persist_review(
            report_dir,
            int(chapter_num),
            result,
            model_name=model_name,
        )
    return result
