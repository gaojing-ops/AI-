"""Keep selected source documents whole; emit metadata, never hidden excerpts."""
import hashlib
from prompt_budget import PromptBudgetError


class SourceBudgetError(PromptBudgetError):
    """A context cannot contain its required whole sources and budget notice."""

    def __init__(self, message, metadata=None):
        self.metadata = metadata or {"status": "BLOCKED"}
        self.report = self.metadata
        RuntimeError.__init__(self, message)


def whole_source(label, text, category):
    text = str(text or "")
    return (f"【{label}】\n{text}", {
        "category": category, "source_label": str(label), "source_chars": len(text),
        "selected_chars": len(text), "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "selection": "whole_filtered_source", "truncated": False,
    })


def require_complete_source(text, max_chars, label):
    """Return a whole rendered source or a terminal budget error, never a slice."""
    if not isinstance(text, str) or not isinstance(label, str) or not label or len(label) > 256:
        raise SourceBudgetError("PROMPT_BUDGET_EXCEEDED：完整来源文本或标签无效")
    if max_chars is None:
        return text
    metadata = {
        "source_label": label, "source_chars": len(text),
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "budget_chars": max_chars, "selected_chars": 0, "status": "BLOCKED",
    }
    if type(max_chars) is not int or not 1 <= max_chars <= 128000:
        raise SourceBudgetError("PROMPT_BUDGET_EXCEEDED：完整来源预算必须为1—128000整数或None", metadata)
    if len(text) > max_chars:
        raise SourceBudgetError(
            f"PROMPT_BUDGET_EXCEEDED：{label}完整来源{len(text)}字符超过{max_chars}；"
            "模型调用前停止，未截断来源，勿原样重试。", metadata,
        )
    return text


def select_whole_sources(sources, max_chars):
    """Budget complete source blocks in input priority order, with no retries.

    Required sources must all fit. Optional omissions are named in the prompt;
    ineligible future sources appear only in metadata, never in prompt text.
    Metadata records hashes and counts, not source prose. No files are written.
    """
    if type(max_chars) is not int or not 1 <= max_chars <= 128000:
        raise SourceBudgetError("完整来源预算必须为1—128000的整数字符数")
    if not isinstance(sources, (list, tuple)) or len(sources) > 64:
        raise SourceBudgetError("一次完整来源选择最多64个来源")
    records, blocks, eligible, required = [], [], [], []
    labels = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SourceBudgetError("来源必须是对象")
        label = source.get("label")
        text = source.get("text")
        category = source.get("category", "context")
        is_required = source.get("required", False)
        is_eligible = source.get("eligible", True)
        if (not isinstance(label, str) or not label or len(label) > 256
                or label in labels or not isinstance(text, str)
                or len(text) > 128000 or not isinstance(category, str)
                or type(is_required) is not bool or type(is_eligible) is not bool):
            raise SourceBudgetError("来源标签、正文长度、类型或重复来源无效")
        labels.add(label)
        block, record = whole_source(label, text, category)
        record.update(required=is_required, eligible=is_eligible,
                      selected_chars=0, selection="not_selected")
        records.append(record)
        blocks.append(block)
        if not is_eligible:
            record["selection"] = str(source.get("reason") or "ineligible")
        elif not text:
            record["selection"] = "empty_source"
            if is_required:
                raise SourceBudgetError(f"必需来源为空：{label}")
        else:
            eligible.append(index)
            if is_required:
                required.append(index)

    def render(indices, omitted):
        parts = [blocks[index] for index in eligible if index in indices]
        if omitted:
            names = "、".join(records[index]["source_label"] for index in omitted)
            parts.append(f"[预算未纳入完整来源：{names}；这些来源没有提供，不能据此声称已审。]")
        return "\n\n".join(parts)

    required_chars = len(render(set(required), []))
    metadata = {"max_chars": max_chars, "required_chars": required_chars,
                "eligible_count": len(eligible), "sources": records}
    if required_chars > max_chars:
        metadata.update(status="BLOCKED", selected_count=0, used_chars=0,
                        full_coverage=False)
        for index in eligible:
            records[index]["selection"] = "not_sent_budget_blocked"
        raise SourceBudgetError(
            f"完整商业来源需{required_chars}字符，预算{max_chars}；未截断，未自动扩预算", metadata)

    selected = set(eligible)
    text = render(selected, [])
    if len(text) > max_chars:
        selected = set(required)
        omitted = [index for index in eligible if index not in selected]
        text = render(selected, omitted)
        if len(text) > max_chars:
            metadata.update(status="BLOCKED", selected_count=0, used_chars=0,
                            full_coverage=False)
            for index in eligible:
                records[index]["selection"] = "not_sent_budget_blocked"
            raise SourceBudgetError("预算不足以保留必需来源与完整省略说明；未截断", metadata)
        for index in eligible:
            if index in required:
                continue
            candidate = selected | {index}
            rest = [item for item in eligible if item not in candidate]
            proposed = render(candidate, rest)
            if len(proposed) <= max_chars:
                selected, text = candidate, proposed

    omitted = [index for index in eligible if index not in selected]
    for index in eligible:
        records[index]["selection"] = "whole_filtered_source" if index in selected else "omitted_budget"
        if index in selected:
            records[index]["selected_chars"] = records[index]["source_chars"]
    metadata.update(status="OK", selected_count=len(selected), used_chars=len(text),
                    full_coverage=not omitted,
                    omitted_labels=[records[index]["source_label"] for index in omitted])
    return {"text": text, "metadata": metadata}
