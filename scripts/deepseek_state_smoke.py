"""Opt-in, isolated DeepSeek extraction/audit smoke test; never commits a book.

Default is dry-run. Credentials come from a non-echoing terminal or explicit
private-vault opt-in, never from argv or diagnostic output.
"""
import argparse
import getpass
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui_app  # Bootstraps the distribution's local dependencies.
import deepseek_runtime
import generator


TEST_TEXT = "林舟走进空教室。林舟把窗户关上。林舟坐在第一排的椅子上。"


def run_smoke(client, model, *, scenario="success", audit_model=None):
    if model not in deepseek_runtime.SUPPORTED_MODELS:
        raise ValueError("unsupported model")
    audit_model = audit_model or model
    if audit_model not in deepseek_runtime.SUPPORTED_MODELS or scenario not in {"success", "repair"}:
        raise ValueError("unsupported scenario or audit model")
    call_limit = 3 if scenario == "repair" else 2
    app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
    app.config = {"structured_state_enabled": True, "temporal_memory_enabled": False,
                  "state_delta_independent_audit": True, "pre_save_continuity_audit_enabled": True,
                  "state_delta_max_attempts": 1, "state_repair_max_rejections": 2 if scenario == "repair" else 1}
    audits, calls, rejections = [], [], []
    app._append_batch_audit = audits.append
    phase = {"name": ""}
    def progress(event, **kwargs):
        if event == "state_model_call_started":
            phase["name"] = kwargs.get("phase", "")
    app._formal_suffix_emit_progress = progress
    app._official_chapter_paths_by_number = lambda: {}

    def review(system, user, **kwargs):
        if len(calls) >= call_limit:
            raise RuntimeError("SMOKE_CALL_LIMIT")
        selected_model = audit_model if phase["name"] == "独立证据审计" else model
        row = {"number": len(calls) + 1, "status": "STARTED", "model": selected_model,
               "phase": phase["name"],
               "request_sha256": hashlib.sha256((system + "\0" + user).encode()).hexdigest()}
        calls.append(row)  # Count attempted calls, including errors.
        started = time.monotonic()
        try:
            response = client.chat.completions.create(**deepseek_runtime.build_chat_kwargs(
                model=selected_model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": user}],
                max_tokens=min(1200, kwargs.get("max_tokens", 1200)), thinking=False,
                temperature=kwargs.get("temp", 0),
            ))
            choice = response.choices[0]
            row["finish_reason"] = choice.finish_reason
            row["usage"] = deepseek_runtime.normalize_usage(response.usage)
            row["estimated_cost_cny_peak"] = deepseek_runtime.estimate_cost_cny(selected_model, row["usage"])
            if choice.finish_reason != "stop":
                raise RuntimeError("SMOKE_INCOMPLETE_RESPONSE")
            text = choice.message.content or ""
            row["response_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            row["status"] = "RETURNED_NOT_YET_VALIDATED"
            return text
        except Exception as exc:
            row["status"] = "ERROR"
            row["error_type"] = type(exc).__name__
            row["http_status"] = getattr(exc, "status_code", None)
            raise
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 2)

    app.call_llm_review = review
    original_dirs = dict(generator.DIRS)
    result = {"model": model, "audit_model": audit_model, "scenario": scenario,
              "call_limit": call_limit, "output_limit_per_call": 1200,
              "sample": "synthetic_classroom", "formal_commit": False, "calls": calls}
    try:
        with tempfile.TemporaryDirectory(prefix="novel-state-smoke-") as directory:
            generator.DIRS.update({"plot": directory, "logs": directory, "out": directory})
            try:
                context = None
                if scenario == "repair":
                    # Deliberately faulty candidate in the ISOLATED copy. Only
                    # a REAL audit rejection may authorize the repair below.
                    seeded = {"chapter": 1, "events": [{"event_type": "world",
                        "summary": "林舟把窗户打开", "evidence_quote": "林舟把窗户关上。"}]}
                    system, prompt = gui_app.state_ledger.build_delta_audit_prompts(seeded, chapter_text=TEST_TEXT)
                    raw = app._call_state_review(system, prompt, chapter=1, attempt=1,
                                                 phase="独立证据审计", temp=0)
                    payload = gui_app.state_ledger.parse_json_object(raw)
                    issues = gui_app.state_ledger.validate_delta_audit(payload, seeded, chapter_text=TEST_TEXT)
                    # No synthetic rejection and no re-prompt to force a verdict.
                    if not issues or not payload.get("reject"):
                        raise RuntimeError("SMOKE_REAL_REJECTION_NOT_OBSERVED")
                    result["seed_rejected_by_real_audit"] = True
                    history = gui_app.state_repair.record([], seeded, issues)
                    context = {"schema_version": 1, "status": "REJECTED", "chapter": 1,
                        "chapter_sha256": gui_app.state_ledger.chapter_sha256(TEST_TEXT),
                        "previous_raw": json.dumps(seeded, ensure_ascii=False), "issues": issues,
                        "repair_history": history}
                    # Persist and read the context in the isolated directory,
                    # exercising the same data shape as a resumed repair.
                    saved = Path(directory) / "repair_context.json"
                    saved.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
                    context = json.loads(saved.read_text(encoding="utf-8"))
                prepared = app._prepare_structured_state_delta(
                    1, TEST_TEXT, repair_context=context, rejection_callback=rejections.append,
                )
                result["status"] = "PASS"
                result["delta_counts"] = {key: len(prepared["delta"].get(key, []))
                                          for key in gui_app.state_ledger.MAX_ITEMS}
                if scenario == "repair":
                    final_text = json.dumps(prepared["delta"], ensure_ascii=False)
                    result["incorrect_open_claim_removed"] = "打开" not in final_text
                    if not result["incorrect_open_claim_removed"]:
                        result["status"] = "FAIL"
            except Exception as exc:
                result["status"] = "FAIL"
                result["error_type"] = type(exc).__name__
                # Do not emit exception messages: SDK errors can include server
                # response bodies, request details, or sensitive data.
            result["formal_state_written"] = (Path(directory) / "story_state.json").exists()
    finally:
        generator.DIRS.clear()
        generator.DIRS.update(original_dirs)
    result["rejection_rounds"] = (len(rejections[-1]["repair_history"]) if rejections
                                  else int(result.get("seed_rejected_by_real_audit", False)))
    result["budgets"] = [row for row in audits if row.get("event") == "prompt_budget"]
    result["estimated_cost_cny_peak"] = sum(row.get("estimated_cost_cny_peak", 0) for row in calls)
    if result["formal_state_written"]:
        result["status"] = "FAIL"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--model", choices=sorted(deepseek_runtime.SUPPORTED_MODELS), default="deepseek-flash")
    parser.add_argument("--audit-model", choices=sorted(deepseek_runtime.SUPPORTED_MODELS))
    parser.add_argument("--scenario", choices=["success", "repair"], default="success")
    parser.add_argument("--use-saved-key", action="store_true", help="Read the configured private credential, never print it")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"status": "DRY_RUN", "model": args.model, "call_limit": 3 if args.scenario == "repair" else 2,
                          "network_calls": 0, "formal_commit": False}))
        return 0
    if not args.use_saved_key and not sys.stdin.isatty():
        print(json.dumps({"status": "BLOCKED", "reason": "non_echoing_terminal_required"}))
        return 2
    try:
        if args.use_saved_key:
            key = generator.get_runtime_api_key()
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("DeepSeek API key (hidden, memory only): ").strip()
        if not key:
            raise ValueError("empty key")
        from openai import OpenAI
        import httpx
        # Official host only. Disable SDK retries, proxy inheritance and redirects.
        with OpenAI(api_key=key, base_url="https://api.deepseek.com", max_retries=0,
                    http_client=httpx.Client(trust_env=False, follow_redirects=False,
                                             timeout=httpx.Timeout(45, connect=10))) as client:
            key = ""
            available = {row.id for row in client.models.list().data}
            wanted = {args.model, args.audit_model or args.model}
            if any(deepseek_runtime.canonical_model(m) not in
                   {deepseek_runtime.canonical_model(a) for a in available} for m in wanted):
                print(json.dumps({"status": "BLOCKED", "reason": "configured_model_unavailable",
                                  "model": args.model, "model_calls": 0,
                                  "available_models": sorted(available)[:20]}))
                return 2
            print("MODEL_PREFLIGHT_OK; bounded isolated scenario", flush=True)
            result = run_smoke(client, args.model, scenario=args.scenario, audit_model=args.audit_model)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result["status"] == "PASS" else 2
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "status_code", None)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
