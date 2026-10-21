import json

import pytest

from prompt_budget import Block, PromptBudgetError, assemble


def fit(blocks, **kwargs):
    return assemble(blocks, estimate_tokens=len, system_cap=1000, user_cap=kwargs.get("cap", 1000),
                    token_budget=kwargs.get("tokens", 2064))


def test_required_content_is_byte_exact_and_order_preserved():
    blocks = [Block("rules", "规则\n", "system"), Block("task", "任务\r\n"),
              Block("canon", '事实：{"x":1}', priority=1), Block("draft", "正文尾")]
    system, user, report = fit(blocks)
    assert system == "规则\n"
    assert user == '任务\r\n事实：{"x":1}正文尾'
    assert report["trimmed"] is False


def test_optional_priority_and_smaller_later_block_can_fit():
    blocks = [Block("core", "C" * 10), Block("low", "L" * 20, priority=2, required=False),
              Block("high", "H" * 10, priority=1, required=False),
              Block("small", "S", priority=2, required=False)]
    _, user, report = fit(blocks, cap=21)
    assert user == "C" * 10 + "H" * 10 + "S"
    assert report["blocks"][1]["reason"] == "optional_budget"
    assert report["trimmed"] is True


def test_required_priority_one_cannot_be_dropped():
    with pytest.raises(PromptBudgetError) as caught:
        fit([Block("canon", "x" * 11, priority=1)], cap=10)
    assert caught.value.report["exceeded"] == ["user_chars"]
    assert caught.value.report["blocks"][0]["kept_chars"] == 11


def test_token_limit_is_enforced_even_below_char_limits():
    with pytest.raises(PromptBudgetError) as caught:
        fit([Block("core", "abcdef")], tokens=69)
    assert "estimated_input_tokens" in caught.value.report["exceeded"]
    assert "abcdef" not in str(caught.value)


def test_optional_token_overflow_does_not_cut_a_quote():
    _, user, report = fit([Block("core", "a"), Block("quote", "整段原句", priority=2, required=False)], tokens=66)
    assert user == "a"
    assert report["blocks"][1]["kept_chars"] == 0


def test_report_has_counts_not_prompt_text_or_secret():
    _, _, report = fit([Block("core", "PRIVATE_SECRET_123")])
    assert "PRIVATE_SECRET_123" not in json.dumps(report)
    assert report["token_count_kind"] == "estimate_not_provider_usage"


def test_exact_budget_boundary_includes_message_overhead():
    assert fit([Block("core", "123")], cap=3, tokens=67)[2]["status"] == "READY"


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "100"])
def test_invalid_budgets_fail_closed(budget):
    with pytest.raises(ValueError):
        fit([Block("core", "x")], cap=budget)


def test_duplicate_block_names_rejected():
    with pytest.raises(ValueError):
        fit([Block("same", "x"), Block("same", "y")])
