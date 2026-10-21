"""Whole-block prompt budgets. Required evidence is never silently truncated."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    name: str
    text: str
    role: str = "user"
    priority: int = 0
    required: bool = True


class PromptBudgetError(RuntimeError):
    def __init__(self, report):
        self.report = report
        limits = ", ".join(report["exceeded"])
        super().__init__(
            "PROMPT_BUDGET_EXCEEDED：核心上下文超限（" + limits +
            "）；已在模型调用前停止，未截断正文或证据。请检查上下文来源和预算，勿原样重试。"
        )


def assemble(blocks, *, estimate_tokens, system_cap, user_cap, token_budget):
    """Keep input ordering and complete blocks; only optional blocks may drop.

    Token values are estimates for the caller's tokenizer, not provider usage.
    Reports intentionally contain no prompt text, excerpts or credentials.
    """
    blocks = tuple(blocks)
    if any(type(value) is not int or value < 1 for value in (system_cap, user_cap, token_budget)):
        raise ValueError("上下文预算必须是正整数")
    if len({b.name for b in blocks}) != len(blocks):
        raise ValueError("上下文块名称必须唯一")
    if any(b.role not in ("system", "user") or b.priority not in (0, 1, 2)
           or not isinstance(b.text, str) for b in blocks):
        raise ValueError("上下文块角色、优先级或文本无效")

    def measure(selected):
        system = "".join(b.text for i, b in enumerate(blocks) if i in selected and b.role == "system")
        user = "".join(b.text for i, b in enumerate(blocks) if i in selected and b.role == "user")
        stats = {"system_chars": len(system), "user_chars": len(user),
                 "input_tokens": estimate_tokens(system) + estimate_tokens(user) + 64}
        exceeded = [key for key, actual, cap in (
            ("system_chars", len(system), system_cap), ("user_chars", len(user), user_cap),
            ("estimated_input_tokens", stats["input_tokens"], token_budget)) if actual > cap]
        return system, user, stats, exceeded

    all_indexes = set(range(len(blocks)))
    selected = {i for i, b in enumerate(blocks) if b.required}
    original = measure(all_indexes)[2]

    def report(status, final, exceeded):
        return {"status": status, "trimmed": selected != all_indexes,
                "original": original, "final": final, "exceeded": exceeded,
                "token_count_kind": "estimate_not_provider_usage",
                "limits": {"system_chars": system_cap, "user_chars": user_cap,
                           "input_tokens": token_budget},
                "blocks": [{"name": b.name, "role": b.role, "priority": b.priority,
                            "required": b.required, "original_chars": len(b.text),
                            "kept_chars": len(b.text) if i in selected else 0,
                            "estimated_tokens": estimate_tokens(b.text),
                            "reason": "required" if b.required else
                                      ("fits" if i in selected else "optional_budget")}
                           for i, b in enumerate(blocks)]}

    system, user, stats, exceeded = measure(selected)
    if exceeded:
        raise PromptBudgetError(report("BLOCKED", stats, exceeded))
    for i in sorted(all_indexes - selected, key=lambda i: (blocks[i].priority, i)):
        trial = selected | {i}
        if not measure(trial)[3]:
            selected = trial
    system, user, stats, exceeded = measure(selected)
    return system, user, report("READY", stats, exceeded)
