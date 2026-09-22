"""Rebuild resource projections from the immutable chain; dry-run by default."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import novel_cli
import state_ledger


def protected_hashes(project: Path) -> dict[str, str]:
    files = []
    for relative in ("output", "publish", "plot/state_deltas", "plot/runtime/commit_receipts"):
        files.extend(path for path in (project / relative).rglob("*") if path.is_file())
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files}


def rebuild(project: Path, expected_latest: int, execute: bool = False) -> dict:
    project = project.resolve(strict=True)
    plot = project / "plot"
    formal, duplicates = novel_cli._scan_chapters(project / "output")
    if duplicates or sorted(formal) != list(range(1, expected_latest + 1)):
        raise ValueError("正式章节不连续或尖端与 expected-latest 不符")
    old = json.loads((plot / "story_state.json").read_text(encoding="utf-8"))
    protected = protected_hashes(project)
    rebuilt = state_ledger.rebuild_state_from_deltas(plot)
    if rebuilt["current_chapter"] != expected_latest:
        raise ValueError("重放尖端与正式尖端不符")
    for number, path in formal.items():
        if rebuilt["applied_chapters"].get(str(number)) != novel_cli._chapter_text_sha256(path):
            raise ValueError(f"第{number}章原始增量与正式正文不符")
    # Refuse to hide a wider migration under a resource-balance repair.
    if set(old.get("resources", {})) != set(rebuilt["resources"]):
        raise ValueError("资源键集合不同，拒绝局部重建")
    differences = []
    for key, current in old["resources"].items():
        target = rebuilt["resources"][key]
        fields = sorted(field for field in set(current) | set(target)
                        if current.get(field) != target.get(field))
        if set(fields) - {"quantity", "status"}:
            raise ValueError(f"资源{key}存在非余额/状态差异：{fields}")
        if fields:
            differences.append({"key": key, "item": target["item"],
                                "fields": {field: [current.get(field), target.get(field)]
                                           for field in fields}})
    comparison = copy.deepcopy(old)
    comparison["resources"] = rebuilt["resources"]
    comparison["updated_at"] = rebuilt["updated_at"]
    if comparison != rebuilt:
        raise ValueError("存在非资源语义差异，拒绝局部重建")
    report = {"read_only": not execute, "chapter": expected_latest, "changes": differences,
              "protected_files": len(protected), "executed": False}
    if not execute or not differences:
        return report
    if protected != protected_hashes(project):
        raise ValueError("原始证据在预检期间发生变化")
    backup = project / ".runtime" / "resource_balance_rebuild" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup.mkdir(parents=True)
    saved = {}
    for name in ("story_state.json", "story_state.sha256"):
        source = plot / name
        if source.exists():
            saved[name] = source.read_bytes()
            shutil.copy2(source, backup / name)
    state_ledger._atomic_write_json(backup / "manifest.json", {**report, "protected": protected})
    try:
        state_ledger._write_materialized_state(plot, rebuilt)
        state_ledger.verify_materialized_state(plot)
        if protected != protected_hashes(project):
            raise ValueError("重建期间原始证据发生变化")
    except Exception:
        for name, data in saved.items():
            state_ledger._atomic_write_text(plot / name, data.decode("utf-8"))
        if "story_state.sha256" not in saved:
            (plot / "story_state.sha256").unlink(missing_ok=True)
        raise
    report.update(executed=True, backup=str(backup), original_evidence_unchanged=True)
    state_ledger._atomic_write_json(backup / "result.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--expected-latest", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(rebuild(args.project, args.expected_latest, args.execute), ensure_ascii=False, indent=2))
