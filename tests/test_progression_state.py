"""Evidence-backed progression is additive, audited, and not outline-derived."""
import copy

import pytest

import continuity_guard
import state_ledger


CANON = {
    "current_chapter": 77,
    "protagonist": "周予安",
    "current_realm": "校园U17球员",
    "realm_order": ["校园U17球员", "俱乐部梯队正式注册球员", "一线队轮换"],
}
QUOTE = "工作人员将回执交给周予安：本年度康桥赛事注册已经完成。"
STAGE = "俱乐部梯队正式注册球员"


def proposal(value=STAGE, name="周予安", quote=QUOTE):
    return {"chapter": 78, "characters": [
        {"name": name, "progression": value, "evidence_quote": quote}
    ], "events": [{"event_type": "reveal", "summary": quote, "evidence_quote": quote}]}


def test_optional_field_keeps_legacy_normalized_delta_and_materialization_identical():
    old = proposal()
    del old["characters"][0]["progression"]
    legacy, issues = state_ledger.validate_delta(old, 78, QUOTE)
    assert not issues
    for blank in (None, "", "  "):
        normalized, issues = state_ledger.validate_delta(proposal(blank), 78, QUOTE)
        assert not issues and normalized == legacy
    before = state_ledger._apply_delta_to_state(state_ledger.initial_state(), legacy, "a" * 64)
    assert "progression" not in before["characters"]["周予安"]


def test_natural_stage_survives_normalization_replay_and_context():
    delta, issues = state_ledger.validate_delta(proposal(), 78, QUOTE, progression_context=CANON)
    assert not issues
    assert STAGE not in QUOTE  # Natural wording need not repeat the canonical tag.
    assert state_ledger.validate_delta(delta, 78, QUOTE)[0] == delta
    state = state_ledger._apply_delta_to_state(state_ledger.initial_state(), delta, "a" * 64)
    assert state["characters"]["周予安"]["progression"] == STAGE
    context, _ = state_ledger.render_context(state, 79, "周予安")
    assert "阶段=" + STAGE in context
    assert state_ledger._apply_delta_to_state(state, delta, "a" * 64) == state


@pytest.mark.parametrize("value", [True, [], {}, 3, "长" * 121])
def test_invalid_stage_shape_is_rejected(value):
    _, issues = state_ledger.validate_delta(proposal(value), 78, QUOTE)
    assert any("progression" in issue for issue in issues)


@pytest.mark.parametrize("name,stage", [("周予安", "世界冠军"), ("贺子骁", STAGE)])
def test_wrong_actor_or_unknown_project_stage_is_rejected(name, stage):
    text = QUOTE + "贺子骁站在门边。"
    _, issues = state_ledger.validate_delta(proposal(stage, name, text), 78, text,
                                           progression_context=CANON)
    assert any("项目阶段表" in issue for issue in issues)


def test_fabricated_stage_quote_cannot_enter_delta():
    _, issues = state_ledger.validate_delta(proposal(), 78, "周予安尚未提交申请。")
    assert any("evidence_quote" in issue for issue in issues)


def test_both_prompts_require_current_evidence_not_plan_history_or_another_actor():
    for system, prompt in [
        state_ledger.build_extraction_prompts(78, QUOTE, progression_context=CANON),
        state_ledger.build_delta_audit_prompts(proposal(), chapter_text=QUOTE,
                                              progression_context=CANON),
    ]:
        assert state_ledger.PROGRESSION_EVIDENCE_RULE in system
        for marker in ("候选", "条件", "他人身份", "过去的回忆", "独立证据审计"):
            assert marker in system
        assert STAGE in prompt and "周予安" in prompt
    assert "未提供阶段表" in state_ledger.build_extraction_prompts(78, QUOTE)[1]


@pytest.mark.parametrize("text", [
    "周予安准备明天申请注册。", "周予安如果通过评估就能注册。",
    "周予安想起三年前曾完成注册。", "周予安尚未完成注册。",
])
def test_independent_rejection_of_false_current_stage_remains_failure(text):
    delta, _ = state_ledger.validate_delta(proposal(quote=text), 78, text,
                                          progression_context=CANON)
    review = {"pass": False, "missing": [], "reject": [{
        "path": "characters[0]", "reason": "没有当前已生效的注册事实",
        "draft_quote": text, "repair_instruction": "删除progression；不修改正文",
    }]}
    assert state_ledger.validate_delta_audit(review, delta, chapter_text=text)


def test_canon_uses_audited_natural_stage_and_records_source_chapter(tmp_path):
    continuity_guard.save_canon(tmp_path, copy.deepcopy(CANON))
    delta, issues = state_ledger.validate_delta(proposal(), 78, QUOTE, progression_context=CANON)
    assert not issues
    continuity_guard.update_after_chapter(tmp_path, 78, QUOTE,
        chapter_outline="状态落点：境界=一线队轮换", prepared_state_delta={"delta": delta})
    canon = continuity_guard.load_canon(tmp_path)
    assert canon["current_realm"] == STAGE
    assert canon["current_realm_source_chapter"] == 78


def test_outline_or_stale_delta_cannot_update_stage(tmp_path):
    continuity_guard.save_canon(tmp_path, copy.deepcopy(CANON))
    continuity_guard.update_after_chapter(tmp_path, 79, "周予安回到教室。",
        chapter_outline="状态落点：境界=一线队轮换", prepared_state_delta=proposal())
    assert continuity_guard.load_canon(tmp_path)["current_realm"] == CANON["current_realm"]


def test_unknown_canon_stage_fails_without_saving_new_current_state(tmp_path):
    continuity_guard.save_canon(tmp_path, copy.deepcopy(CANON))
    with pytest.raises(RuntimeError, match="项目阶段表"):
        continuity_guard.update_after_chapter(tmp_path, 78, QUOTE,
            prepared_state_delta=proposal("凭空阶段"))
    assert continuity_guard.load_canon(tmp_path)["current_realm"] == CANON["current_realm"]


def test_current_audited_stage_can_record_real_demotion(tmp_path):
    canon = dict(CANON, current_realm="一线队轮换")
    continuity_guard.save_canon(tmp_path, canon)
    text = "周予安被调回梯队，保留已经完成的梯队注册。"
    delta, issues = state_ledger.validate_delta(proposal(quote=text), 78, text,
                                               progression_context=canon)
    assert not issues
    continuity_guard.update_after_chapter(tmp_path, 78, text, prepared_state_delta=delta)
    assert continuity_guard.load_canon(tmp_path)["current_realm"] == STAGE
