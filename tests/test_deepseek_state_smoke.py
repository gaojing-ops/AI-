import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import generator


spec = importlib.util.spec_from_file_location("deepseek_smoke", Path(__file__).parents[1] / "scripts" / "deepseek_state_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def response(text, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                            message=SimpleNamespace(content=text))], usage=None)


def test_smoke_two_calls_and_no_formal_write():
    delta = {"chapter": 1, "events": [{"event_type": "world", "summary": "林舟把窗户关上",
                                      "evidence_quote": "林舟把窗户关上。"}]}
    client = Mock()
    client.chat.completions.create.side_effect = [response(json.dumps(delta)),
        response(json.dumps({"pass": True, "reject": [], "missing": []}))]
    before = dict(generator.DIRS)
    report = smoke.run_smoke(client, "deepseek-v4-flash")
    assert report["status"] == "PASS"
    assert len(report["calls"]) == 2
    assert report["formal_state_written"] is False
    assert generator.DIRS == before
    assert all(call.kwargs["max_tokens"] <= 1200 for call in client.chat.completions.create.call_args_list)


def test_smoke_failure_never_retries_or_prints_exception_secret():
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("FAKE_CREDENTIAL_DO_NOT_PRINT")
    report = smoke.run_smoke(client, "deepseek-v4-flash")
    assert report["status"] == "FAIL"
    assert len(report["calls"]) == 1
    assert "FAKE_CREDENTIAL_DO_NOT_PRINT" not in json.dumps(report)


def test_smoke_length_finish_is_not_pass():
    client = Mock()
    client.chat.completions.create.return_value = response('{}', 'length')
    report = smoke.run_smoke(client, "deepseek-v4-flash")
    assert report["status"] == "FAIL"
    assert len(report["calls"]) == 1


def test_bounded_repair_uses_real_audit_feedback_then_repairs_and_audits():
    client = Mock()
    rejection = {"pass": False, "reject": [{"path": "events[0]", "reason": "打开与原文关上相反",
        "draft_quote": "林舟把窗户关上。", "repair_instruction": "改为林舟把窗户关上"}]}
    client.chat.completions.create.side_effect = [response(json.dumps(rejection)),
        response(json.dumps({"updates": {"events[0]": {"summary": "林舟把窗户关上"}}})),
        response(json.dumps({"pass": True, "reject": [], "missing": []}))]
    report = smoke.run_smoke(client, "deepseek-flash", scenario="repair", audit_model="deepseek-v4-pro")
    assert report["status"] == "PASS"
    assert report["seed_rejected_by_real_audit"] is True
    assert report["incorrect_open_claim_removed"] is True
    assert report["rejection_rounds"] == 1
    assert [row["model"] for row in report["calls"]] == ["deepseek-v4-pro", "deepseek-flash", "deepseek-v4-pro"]
    assert len(report["calls"]) == 3
    assert report["formal_state_written"] is False


def test_no_genuine_seed_rejection_cannot_claim_repair_pass():
    client = Mock()
    client.chat.completions.create.return_value = response(json.dumps({"pass": True, "reject": [], "missing": []}))
    report = smoke.run_smoke(client, "deepseek-flash", scenario="repair")
    assert report["status"] == "FAIL"
    assert len(report["calls"]) == 1
