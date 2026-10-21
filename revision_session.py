"""Persistent, normal-mode revision checkpoints; never approve or rewrite prose.

Already committed chapters are verified from receipts, not a progress counter.
Model failure retains the isolated prefix. Original adoption remains one normal
rollback-capable suffix transaction, followed by a separate next-chapter commit.
"""
from __future__ import annotations

import hashlib
import copy
import difflib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

import chapter_commit
import chapter_budget
import formal_suffix_rewrite
import narrative_guard
import project_job_lock
import state_ledger
import state_repair
import story_architect

CHAPTER = re.compile(r"^第(\d{1,6})章\.txt$")
STEPS = ("chapter_saved", "state_superseded", "canon_restored",
         "narrative_audit_superseded", "canon_updated", "state_committed",
         "derived_memory_rebuilt", "facts_updated", "status_recorded",
         "narrative_audit_persisted", "commercial_review_persisted", "publish_resolved")
PROGRESS_ONLY = {"plot/写作进度.md"}
OUTLINE_HEADING = re.compile(r"(?m)^第\s*\d+\s*章(?:\s+[^\n]*)?\s*$")


def _check_bundle_replacement(before, after, tip, changed_chapters):
    """Conservative future-only edits: no global/passed contract changes."""
    if set(before) != set(after):
        raise RuntimeError("替换配套必须保留原完整文件集合")
    dates = set()
    protected_dates = set()
    for name, text in before.items():
        if name.endswith("逐章细纲.txt"):
            for chapter, section in story_architect.split_outline_sections(text).items():
                fields = story_architect.parse_outline_fields(section)
                date = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", fields.get("状态落点", ""))
                if chapter <= tip:
                    protected_dates.update(date)
                elif chapter in changed_chapters:
                    dates.update(date)
    for name, old in before.items():
        new = after[name]
        if old == new:
            continue
        if name.endswith("逐章细纲.txt"):
            old_sections = story_architect.split_outline_sections(old)
            new_sections = story_architect.split_outline_sections(new)
            old_numbers = [int(re.match(r"第\s*(\d+)", m.group())[1]) for m in OUTLINE_HEADING.finditer(old)]
            new_numbers = [int(re.match(r"第\s*(\d+)", m.group())[1]) for m in OUTLINE_HEADING.finditer(new)]
            old_head = OUTLINE_HEADING.search(old)
            new_head = OUTLINE_HEADING.search(new)
            if (not old_head or not new_head or set(old_sections) != set(new_sections)
                    or len(old_numbers) != len(set(old_numbers)) or len(new_numbers) != len(set(new_numbers))
                    or old_numbers != new_numbers
                    or old[:old_head.start()] != new[:new_head.start()]
                    or any(old_sections[n] != new_sections[n] for n in old_sections
                           if n <= tip or n not in changed_chapters)):
                raise RuntimeError(f"配套不能改变全局或已验章节合同：{name}")
        elif name == "比赛与成长台账.md":
            left, right = old.splitlines(), new.splitlines()
            for tag, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
                if tag == "equal":
                    continue
                if tag != "replace" or b - a != d - c:
                    raise RuntimeError("台账替换仅允许未验日期的同表行定点修改")
                for old_line, new_line in zip(left[a:b], right[c:d]):
                    match = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|", old_line)
                    target = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|", new_line)
                    if (not match or not target or match[1] != target[1]
                            or match[1] not in dates or match[1] in protected_dates):
                        raise RuntimeError("台账修改日期不属于本次未验章节合同")
        else:
            raise RuntimeError(f"此全局配套不能在已验断点上替换：{name}")


def _resource_order_patch(rejected, patch, text):
    """Explicit local diagnosis, not a forged model verdict or extra approval.

    First enforce the ORIGINAL model permissions for every content edit. Only
    then reorder those unchanged resource rows by their continuous prose quotes.
    No new fact or balance can be supplied by the ordering operation.
    """
    patch = copy.deepcopy(patch)
    requested = patch.pop("resource_order_by_evidence", False)
    if requested is not True:
        if requested is not False:
            raise ValueError("resource_order_by_evidence必须是布尔值")
        return rejected, patch, []
    if not isinstance(rejected, dict) or rejected.get("status") != "REJECTED":
        raise ValueError("资源顺序修复必须绑定真实拒绝输入")
    previous = json.loads(rejected["previous_raw"])
    permissions = state_repair.plan(previous, rejected["issues"], expected_chapter=previous["chapter"])
    if permissions is None:
        raise ValueError("缺少原拒绝修改权限")
    proposed = state_repair.apply(previous, patch, permissions)
    prose = state_ledger._normalized_evidence(text)
    positions = []
    for row in proposed.get("resources", []):
        quote = state_ledger._normalized_evidence(row.get("evidence_quote", ""))
        offset = prose.find(quote) if quote else -1
        if offset < 0 or len(quote) < 4 or prose.count(quote) != 1:
            raise ValueError("资源顺序诊断须每行都有正文中唯一连续有效引文")
        positions.append(offset)
    order = sorted(range(len(positions)), key=lambda i: positions[i])
    if order == list(range(len(order))):
        return rejected, patch, []
    diagnostics = [f"resources[{i}] 本地引文顺序诊断：原资源数组未按取得、使用、失效的正文顺序；"
                   "只授权完整原行重排，不改变事实内容，不冒充模型意见。"
                   for i in range(len(previous.get("resources", [])))]
    context = copy.deepcopy(rejected)
    context["model_rejection_sha256"] = state_repair.digest(rejected)
    context["local_resource_order_diagnostics"] = {"issues": diagnostics, "source_quote_offsets": positions,
                                                "stable_order": order}
    context["issues"] = list(rejected["issues"]) + diagnostics
    patch["remove"] = list(patch.get("remove", [])) + [f"resources[{i}]" for i in range(len(previous.get("resources", [])))
                                                        if f"resources[{i}]" not in patch.get("remove", [])]
    patch["updates"] = {k: v for k, v in patch.get("updates", {}).items() if not k.startswith("resources[")}
    patch.setdefault("append", {})["resources"] = [proposed["resources"][i] for i in order]
    if "resources" in patch.get("prepend", {}):
        del patch["prepend"]["resources"]
    return context, patch, diagnostics


def _manual_audit_window_seconds(project, chapter):
    """Read-only admission check: do not launch an audit with a few seconds left."""
    budget = chapter_budget.ChapterBudget(project, chapter)
    row = budget._read()  # Atomic budget file; project locks already held.
    current = time.time()
    if current < row["last_seen"] or row["stop_reason"] or row["calls"] >= row["call_limit"]:
        raise RuntimeError("CHAPTER_BUDGET_STOPPED：人工审计预算已停、耗尽或时钟回退")
    started = row["started_at"]
    return row["seconds_limit"] if started is None else max(0, row["seconds_limit"] - (current - started))


def _unavailable_admission(row, entry):
    """Legacy migration uses terminal events, never reclassifies semantic FAIL."""
    outcome = entry.get("outcome")
    if outcome is not None:
        return outcome == "UNAVAILABLE"
    events = row.get("events", [])
    start = next((i for i in range(len(events) - 1, -1, -1)
                  if events[i].get("event") == "manual_state_patch_audit_started"
                  and events[i].get("identity") == entry["identity"]), None)
    if start is None:
        return False
    for event in events[start + 1:]:
        if event.get("event") in {"manual_state_patch_audit_started", "manual_state_patch_audit_passed",
                                  "state_model_call_completed"}:
            return False
        if event.get("event") == "manual_state_patch_stopped":
            return (event.get("chapter") == entry.get("manual_context", {}).get("chapter")
                    and str(event.get("error", "")).startswith("独立证据审计调用未完成："))
    return False


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON必须是对象：{Path(path).name}")
    return value


def official_paths(project):
    result = {}
    for path in (Path(project) / "output").rglob("*.txt"):
        match = CHAPTER.fullmatch(path.name)
        if match:
            number = int(match[1])
            if number in result:
                raise RuntimeError(f"第{number}章有重复正式路径")
            result[number] = path
    if sorted(result) != list(range(1, max(result, default=0) + 1)):
        raise RuntimeError("正式章节不连续")
    return result


def source_manifest(project):
    root = Path(project)
    result = {}
    for relative in formal_suffix_rewrite.DEFAULT_TRACKED_PATHS:
        for path in (root / relative).rglob("*"):
            if path.is_file():
                key = path.relative_to(root).as_posix()
                if key not in PROGRESS_ONLY:
                    result[key] = sha256(path)
    # Model/genre/gate configuration must not drift during isolation.
    for name in ("project_config.json",):
        if (root / name).is_file():
            result[name] = sha256(root / name)
    return result


def verify_prefix(project, inputs):
    """No legacy exemptions: inspect every current chapter's real evidence."""
    root = Path(project)
    paths = official_paths(root)
    # Validation is read-only; load_state may repair/rebuild a damaged ledger.
    state_ledger.verify_materialized_state(root / "plot")
    state = read_json(root / "plot" / "story_state.json")
    tip = max(paths, default=0)
    if int(state.get("current_chapter", 0)) != tip:
        raise RuntimeError("断点正文与状态末章不一致")
    config = read_json(root / "project_config.json")
    review_bindings = config.get("revision_prefix_review_bindings", {})
    if not isinstance(review_bindings, dict):
        raise RuntimeError("历史审核模型绑定结构损坏")
    for key, entry in review_bindings.items():
        if (not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key
                or int(key) not in paths or not isinstance(entry, dict)
                or not isinstance(entry.get("review_model"), str) or not entry["review_model"].strip()
                or any(not re.fullmatch(r"[a-f0-9]{64}", str(entry.get(field, "")))
                       for field in ("chapter_sha256", "receipt_sha256"))):
            raise RuntimeError("历史审核模型绑定无效，不能用未来绑定跳过审核")
    chapters = {}
    for number, path in sorted(paths.items()):
        binding = inputs.get(str(number))
        text = path.read_text(encoding="utf-8-sig")
        digest = state_ledger.chapter_sha256(text)
        review_binding = review_bindings.get(str(number))
        if review_binding and review_binding["chapter_sha256"] != digest:
            raise RuntimeError(f"第{number}章历史审核模型绑定正文失效")
        if not binding or digest != binding["chapter_sha256"]:
            raise RuntimeError(f"第{number}章断点与候选正文不一致")
        if state.get("applied_chapters", {}).get(str(number)) != digest:
            raise RuntimeError(f"第{number}章断点状态正文绑定失效")
        wrapper = read_json(root / "plot" / "state_deltas" / f"chapter_{number:04d}.json")
        if wrapper.get("chapter_sha256") != digest:
            raise RuntimeError(f"第{number}章断点增量正文绑定失效")
        receipts = root / "plot" / "runtime" / "commit_receipts"
        matched = None
        for receipt_path in sorted(receipts.glob(f"chapter_{number:04d}_*.json"), reverse=True):
            if review_binding and sha256(receipt_path) != review_binding["receipt_sha256"]:
                continue
            receipt = read_json(receipt_path)
            if (receipt.get("schema_version") != chapter_commit.SCHEMA_VERSION
                    or receipt.get("chapter_sha256") != digest or not receipt.get("completed_at")
                    or receipt.get("chapter_status") != "正式可用"
                    or receipt.get("narrative_audit_required") is not True
                    or not all(receipt.get("steps", {}).get(step) is True for step in STEPS)):
                continue
            chapter_commit.verify_saved_chapter(receipt, chapter_path_override=str(path),
                                                plot_dir_override=str(root / "plot"))
            narrative_guard.verify_persisted_audit(root / "plot", number, text,
                expected_audit_id=receipt.get("audit_id", ""),
                expected_review_model=review_binding["review_model"] if review_binding else config.get("review_model", ""),
                min_score=int(config.get("narrative_guard_min_score", 78)))
            matched = {"chapter_sha256": digest, "receipt_sha256": sha256(receipt_path)}
            break
        if matched is None:
            raise RuntimeError(f"第{number}章缺完整正常提交收据，不能凭文件存在跳过")
        chapters[str(number)] = matched
    return {"tip": tip, "chapters": chapters}


def _make_app(project, budget_project, progress, lock=None):
    from headless_app import HeadlessNovelGenerator
    app = HeadlessNovelGenerator(project, echo_progress=True)
    app._chapter_budget_project = str(budget_project)
    app._project_job_lock = lock
    app._formal_suffix_rewrite_context = {"cache_enabled": True, "micro_edit": False,
                                          "progress_callback": progress}
    app._restore_project_bindings()
    return app


class RevisionSession:
    def __init__(self, project, path, row):
        self.project = Path(project).resolve()
        self.path = Path(path).resolve()
        self.row = row
        if row.get("schema_version") != 1 or Path(row["source_project"]).resolve() != self.project:
            raise RuntimeError("会话项目身份不一致")
        self.checkpoint = Path(row["checkpoint_project"]).resolve()
        if self.checkpoint.is_relative_to(self.project) or self.project.is_relative_to(self.checkpoint):
            raise RuntimeError("隔离断点不能与原项目事实目录重叠")

    @classmethod
    def open(cls, project, session_path=None):
        path = Path(session_path) if session_path else Path(project) / "runtime" / "revision_sessions" / "current.json"
        return cls(project, path, read_json(path))

    @classmethod
    def create(cls, project, candidates_dir, checkpoint=None, session_path=None, bundle_dir=None):
        from novel_cli import build_doctor_report
        root = Path(project).resolve()
        path = Path(session_path) if session_path else root / "runtime" / "revision_sessions" / "current.json"
        if path.exists():
            raise RuntimeError("已有修订会话，请读断点后接续，不重复初始化")
        candidates = Path(candidates_dir).resolve()
        if not candidates.is_dir():
            raise RuntimeError("修订候选目录不存在")
        baseline = build_doctor_report(root)
        if baseline.get("overall_status") != "PASS":
            raise RuntimeError("原项目前置只读体检未通过")
        inputs = official_paths(root)
        end = max(inputs, default=0)
        if end < 1:
            raise RuntimeError("没有可采用的正式尾段")
        for candidate in candidates.glob("第*章.txt"):
            match = CHAPTER.fullmatch(candidate.name)
            if match and 1 <= int(match[1]) <= end + 1:
                inputs[int(match[1])] = candidate
        bindings = {str(n): {"path": str(p.resolve()), "sha256": sha256(p),
                    "chapter_sha256": state_ledger.chapter_sha256(p.read_text(encoding="utf-8-sig").strip())}
                    for n, p in inputs.items()}
        manifest = source_manifest(root)
        bundle = {p.name: {"path": str(p.resolve()), "sha256": sha256(p)}
                  for p in Path(bundle_dir).glob("*") if p.is_file()} if bundle_dir else {}
        if checkpoint:
            isolated = Path(checkpoint).resolve()
            if isolated.is_relative_to(root) or root.is_relative_to(isolated):
                raise RuntimeError("不能导入与原项目重叠的隔离断点")
            verified = verify_prefix(isolated, bindings)
        else:
            isolated = Path(tempfile.mkdtemp(prefix="novel_revision_", dir=root.anchor)) / "project"
            isolated, _ = formal_suffix_rewrite.create_isolated_preflight_project(root, isolated,
                {n: p for n, p in inputs.items() if n <= end})
            app = _make_app(isolated, root, lambda event: None)
            app._prepare_formal_suffix_rewrite(1, end)
            app.refresh_status()
            for name, entry in bundle.items():
                shutil.copy2(entry["path"], isolated / "plot" / name)
            verified = verify_prefix(isolated, bindings)
        for name, entry in bundle.items():
            if sha256(isolated / "plot" / name) != entry["sha256"]:
                raise RuntimeError(f"隔离配套与采用候选不一致：{name}")
        if source_manifest(root) != manifest:
            raise RuntimeError("初始化期间原项目事实资产变化")
        row = {"schema_version": 1, "source_project": str(root), "checkpoint_project": str(isolated),
               "formal_end": end, "inputs": bindings, "source_manifest": manifest, "bundle": bundle,
               "checkpoint_config_sha256": sha256(isolated / "project_config.json"),
               "completed_through": verified["tip"], "completed_chapters": verified["chapters"],
               "status": "READY", "phase": "isolated_preflight", "events": [], "created_at": now(),
               "formal_adoption": "NOT_ATTEMPTED", "next_chapter_commit": "NOT_ATTEMPTED"}
        session = cls(root, path, row)
        session._save()
        return session

    def _save(self):
        self.row["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        chapter_commit._atomic_write_json(self.path, self.row)

    def _event(self, event):
        self.row["events"].append({"observed_at": now(), **event})
        self._save()
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def summary(self):
        tip = int(self.row["completed_through"])
        return {"status": self.row["status"], "verified_through": tip, "next_chapter": tip + 1,
                "formal_end": self.row["formal_end"], "checkpoint_project": str(self.checkpoint),
                "source_project": str(self.project), "phase": self.row["phase"],
                "error": self.row.get("error", ""), "session": str(self.path),
                "formal_adoption": self.row["formal_adoption"],
                "next_chapter_commit": self.row["next_chapter_commit"]}

    def _check_sources(self):
        if source_manifest(self.project) != self.row["source_manifest"]:
            raise RuntimeError("原项目事实资产变化，拒绝继续旧会话")
        for entry in self.row["inputs"].values():
            if sha256(entry["path"]) != entry["sha256"]:
                raise RuntimeError("修订输入变化，须显式重绑定受影响断点，不能沿用旧通过")
        for name, entry in self.row["bundle"].items():
            if sha256(entry["path"]) != entry["sha256"] or sha256(self.checkpoint / "plot" / name) != entry["sha256"]:
                raise RuntimeError(f"修订配套变化：{name}")
        if sha256(self.checkpoint / "project_config.json") != self.row["checkpoint_config_sha256"]:
            raise RuntimeError("隔离模型/门禁配置变化")

    def _locks(self):
        locks = []
        try:
            for project in (self.project, self.checkpoint):
                lock = project_job_lock.ProjectJobLock(project)
                ok, reason = lock.acquire("持久正常修订断点接续")
                if not ok:
                    raise RuntimeError(reason)
                locks.append(lock)
            return locks
        except BaseException:
            for lock in reversed(locks):
                lock.release()
            raise

    def configure_isolated_provider(self, provider, *, reason):
        """Explicit isolated-only transition; pin old evidence, never renew budget."""
        if provider != "deepseek" or not str(reason).strip():
            raise ValueError("隔离模型切换须明确deepseek及人工授权理由")
        locks = self._locks()
        old_row = copy.deepcopy(self.row)
        config_path = self.checkpoint / "project_config.json"
        old_config = read_json(config_path)
        changed = False
        try:
            self._check_sources()
            if self.row["formal_adoption"] != "NOT_ATTEMPTED" or chapter_commit.load(self.checkpoint / "plot"):
                raise RuntimeError("正式采用中或pending提交不能切换隔离模型")
            if old_config.get("model_provider") == provider:
                raise RuntimeError("隔离提供方已相同，不重放切换")
            verified = verify_prefix(self.checkpoint, self.row["inputs"])
            if verified["tip"] != int(self.row["completed_through"]):
                raise RuntimeError("真实前缀与会话进度不同，先恢复证据")
            config = copy.deepcopy(old_config)
            previous = old_config.get("revision_prefix_review_bindings", {})
            config["revision_prefix_review_bindings"] = {
                key: {**entry, "review_model": previous.get(key, {}).get("review_model", old_config.get("review_model", ""))}
                for key, entry in verified["chapters"].items()
            }
            if any(not e["review_model"] for e in config["revision_prefix_review_bindings"].values()):
                raise RuntimeError("旧前缀缺明确审核模型，不能猜测迁移")
            config.update(model_provider="deepseek", model="deepseek-flash", review_model="deepseek-v4-pro")
            archive = self.checkpoint / "runtime" / "isolated_provider_changes" / uuid.uuid4().hex
            archive.mkdir(parents=True)
            shutil.copy2(config_path, archive / "project_config.before.json")
            chapter_commit._atomic_write_json(archive / "session.before.json", old_row)
            chapter_commit._atomic_write_json(config_path, config)
            changed = True
            self.row["checkpoint_config_sha256"] = sha256(config_path)
            # No model approvals, status resets or budget renewals in this operation.
            self._check_sources()
            if verify_prefix(self.checkpoint, self.row["inputs"]) != verified:
                raise RuntimeError("切换后历史收据证明改变")
            self._event({"event": "isolated_provider_changed", "reason": str(reason).strip(),
                         "old_provider": old_config.get("model_provider", ""), "provider": provider,
                         "old_config_sha256": old_row["checkpoint_config_sha256"],
                         "new_config_sha256": self.row["checkpoint_config_sha256"],
                         "verified_through": verified["tip"], "archive": str(archive),
                         "model_calls": 0, "budget_renewals": 0})
            return {**self.summary(), "operation_status": "ISOLATED_PROVIDER_CONFIGURED", "model_calls": 0}
        except BaseException:
            if changed:
                shutil.copy2(archive / "project_config.before.json", config_path)
                self.row = old_row
                chapter_commit._atomic_write_json(self.path, old_row)
            raise
        finally:
            for lock in reversed(locks):
                lock.release()

    def rebind(self, replacements, *, reason, bundle_dir=None):
        """Explicit substantive future repair; never renew budgets or approvals."""
        if not str(reason).strip() or not replacements:
            raise ValueError("显式重绑定必须提供章节与具体修复理由")
        locks = self._locks()
        old_row = copy.deepcopy(self.row)
        backups = {}
        try:
            self._check_sources()
            if self.row["formal_adoption"] != "NOT_ATTEMPTED":
                raise RuntimeError("已进入正式采用的会话不能替换隔离输入")
            if chapter_commit.load(self.checkpoint / "plot"):
                raise RuntimeError("存在待恢复提交，先恢复原绑定，不能替换")
            verified = verify_prefix(self.checkpoint, self.row["inputs"])
            if verified["tip"] < int(self.row["completed_through"]):
                raise RuntimeError("断点计数超过真实收据，拒绝重绑定")
            changes = {}
            for number, target in replacements.items():
                number = int(number)
                previous = self.row["inputs"].get(str(number))
                target = Path(target).resolve()
                if not previous or number <= verified["tip"]:
                    raise RuntimeError("只能替换已有未验输入，不能触碰已验章节")
                if not target.is_relative_to(self.project / "rewrite_staging") or not target.is_file():
                    raise RuntimeError("替换正文须为本项目rewrite_staging候选")
                match = CHAPTER.fullmatch(target.name)
                text = target.read_text(encoding="utf-8-sig").strip()
                heading = re.match(r"^第\s*(\d+)\s*章(?:\s|$)", text)
                if not match or int(match[1]) != number or not heading or int(heading[1]) != number:
                    raise RuntimeError("替换正文文件名及标题必须与章节一致")
                entry = {"path": str(target), "sha256": sha256(target),
                         "chapter_sha256": state_ledger.chapter_sha256(text)}
                if entry["chapter_sha256"] == previous["chapter_sha256"]:
                    raise RuntimeError("正文未实质修改，不能原样重绑定重复审查")
                changes[str(number)] = {"old": previous, "new": entry}
            replacement_bundle = self.row["bundle"]
            if bundle_dir is not None:
                folder = Path(bundle_dir).resolve()
                if not folder.is_relative_to(self.project / "rewrite_staging") or not folder.is_dir():
                    raise RuntimeError("替换配套须为本项目rewrite_staging版本目录")
                replacement_bundle = {p.name: {"path": str(p.resolve()), "sha256": sha256(p)}
                                      for p in folder.iterdir() if p.is_file()}
                before = {name: Path(entry["path"]).read_text(encoding="utf-8-sig")
                          for name, entry in self.row["bundle"].items()}
                after = {name: Path(entry["path"]).read_text(encoding="utf-8-sig")
                         for name, entry in replacement_bundle.items()}
                _check_bundle_replacement(before, after, verified["tip"], {int(n) for n in changes})
                backup = self.checkpoint / "runtime" / "revision_rebinds" / uuid.uuid4().hex
                for name, entry in replacement_bundle.items():
                    if entry["sha256"] != self.row["bundle"][name]["sha256"]:
                        backup.mkdir(parents=True, exist_ok=True)
                        saved = backup / name
                        shutil.copy2(self.checkpoint / "plot" / name, saved)
                        backups[name] = str(saved)
                        shutil.copy2(entry["path"], self.checkpoint / "plot" / name)
            for number, change in changes.items():
                self.row["inputs"][number] = change["new"]
            self.row["bundle"] = replacement_bundle
            current = int(old_row.get("current_chapter") or verified["tip"] + 1)
            prior_error = str(old_row.get("error") or "")
            budget_stopped = any(marker in prior_error for marker in (
                "CHAPTER_BUDGET_STOPPED", "MANUAL_STATE_AUDIT_STOPPED",
                "MANUAL_STATE_AUDIT_WINDOW_INSUFFICIENT",
            ))
            # A later repair does not fix the currently blocked chapter. Even
            # repairing that chapter cannot renew an exhausted budget epoch.
            retain_stop = old_row.get("status") == "STOPPED" and (
                str(current) not in changes or budget_stopped
            )
            self.row.update(completed_through=verified["tip"], completed_chapters=verified["chapters"],
                            status="STOPPED" if retain_stop else "READY",
                            error=prior_error if retain_stop else "")
            self._event({"event": "future_inputs_rebound", "reason": str(reason).strip(),
                         "changes": changes, "verified_through": verified["tip"],
                         "prefix_proof": verified["chapters"],
                         "bundle_before": old_row["bundle"], "bundle_after": replacement_bundle,
                         "stop_retained": retain_stop, "prior_status": old_row.get("status"),
                         "prior_error": prior_error,
                         "checkpoint_bundle_backups": backups})
            return self.summary()
        except BaseException:
            for name, saved in backups.items():
                shutil.copy2(saved, self.checkpoint / "plot" / name)
            self.row = old_row
            # _event saves before printing; BrokenPipe/observer exceptions may
            # occur after the atomic metadata write. Roll back both sides.
            if read_json(self.path) != old_row:
                chapter_commit._atomic_write_json(self.path, old_row)
            raise
        finally:
            for lock in reversed(locks):
                lock.release()

    def _recover_pending(self, app):
        pending = chapter_commit.load(self.checkpoint / "plot")
        if not pending:
            return
        number = int(pending.get("chapter", 0))
        entry = self.row["inputs"].get(str(number))
        target = Path(pending.get("chapter_path", ""))
        target = target.resolve() if target.is_absolute() else (self.checkpoint / target).resolve()
        output = (self.checkpoint / "output").resolve()
        plot = Path(pending.get("plot_dir", "plot"))
        plot = plot.resolve() if plot.is_absolute() else (self.checkpoint / plot).resolve()
        if (not entry or pending.get("chapter_sha256") != entry["chapter_sha256"]
                or plot != (self.checkpoint / "plot").resolve()
                or not target.is_relative_to(output) or not CHAPTER.fullmatch(target.name)
                or int(CHAPTER.fullmatch(target.name)[1]) != number):
            raise RuntimeError("待恢复提交的正文或目标路径不属于本会话")
        app._recover_pending_chapter_commit()
        self._event({"event": "pending_commit_recovered", "chapter": number})

    def advance(self, through):
        through = int(through)
        if not 1 <= through <= int(self.row["formal_end"]):
            raise ValueError("隔离复验终点超出已授权正式采用范围")
        locks = self._locks()
        try:
            self._check_sources()
            app = _make_app(self.checkpoint, self.project, self._event, locks[1])
            self._recover_pending(app)
            verified = verify_prefix(self.checkpoint, self.row["inputs"])
            if verified["tip"] < int(self.row["completed_through"]):
                raise RuntimeError("断点计数超过真实完整收据，不能跳过章节")
            self.row.update(completed_through=verified["tip"], completed_chapters=verified["chapters"],
                            status="RUNNING", phase="isolated_preflight", error="")
            self._event({"event": "checkpoint_verified", "chapter": verified["tip"]})
            for number in range(verified["tip"] + 1, through + 1):
                self.row["current_chapter"] = number
                self._save()
                text = Path(self.row["inputs"][str(number)]["path"]).read_text(encoding="utf-8-sig")
                volume = next((i for i, item in enumerate(app._get_volume_ranges(), 1)
                               if int(item[0]) <= number <= int(item[1])), 1)
                path = self.checkpoint / "output" / f"第{volume:02d}卷" / f"第{number:04d}章.txt"
                app._restore_project_bindings()
                app._commit_manual_chapter(number, str(path), text, "已授权正常修订采用的持久隔离断点")
                verified = verify_prefix(self.checkpoint, self.row["inputs"])
                if verified["tip"] != number:
                    raise RuntimeError("正常提交未留下完整可接续证据")
                self.row.update(completed_through=number, completed_chapters=verified["chapters"])
                self._event({"event": "checkpoint_committed", "chapter": number})
            self.row["status"] = "PREFLIGHT_COMPLETED" if through == self.row["formal_end"] else "READY"
            self._check_sources()
            self._save()
        except Exception as exc:
            self.row.update(status="STOPPED", error=re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(exc)))
            self._event({"event": "checkpoint_stopped", "chapter": self.row.get("current_chapter"),
                         "error": self.row["error"]})
        finally:
            try:
                formal_suffix_rewrite.promote_isolated_preflight_cache(self.checkpoint, self.project)
            finally:
                for lock in reversed(locks):
                    lock.release()
        return self.summary()

    def repair_state(self, patch_file, *, resume_unavailable=False, reason=""):
        """Audit ONE substantive, explicitly supplied state-only patch.

        It never extracts state again, rewrites prose, renews a budget or commits.
        Successful prepared state is reused only by the normal advance path.
        """
        locks = self._locks()
        number = None
        admission = None
        budget_path = None
        try:
            self._check_sources()
            if self.row["formal_adoption"] != "NOT_ATTEMPTED" or chapter_commit.load(self.checkpoint / "plot"):
                raise RuntimeError("正式采用中或pending提交不能人工修隔离状态")
            verified = verify_prefix(self.checkpoint, self.row["inputs"])
            if verified["tip"] < int(self.row["completed_through"]):
                raise RuntimeError("断点计数超过真实收据")
            number = verified["tip"] + 1
            if number > int(self.row["formal_end"]):
                raise RuntimeError("隔离尾段已验完，无未验状态可修")
            patch_file = Path(patch_file).resolve()
            if not patch_file.is_relative_to(self.project / "rewrite_staging") or patch_file.suffix != ".json":
                raise RuntimeError("人工状态补丁须为本项目rewrite_staging JSON")
            patch = read_json(patch_file)
            app = _make_app(self.checkpoint, self.project, self._event, locks[1])
            text = Path(self.row["inputs"][str(number)]["path"]).read_text(encoding="utf-8-sig").strip()
            outline = app._extract_chapter_outline(number)
            state = read_json(self.checkpoint / "plot" / "story_state.json")
            fingerprint = app._formal_suffix_preflight_fingerprint(number, text, outline)
            if app._load_formal_suffix_preflight_stage(number, fingerprint, "structured_state") is not None:
                raise RuntimeError("当前精确上下文已有已审状态，直接正常接续，禁止重复人工审计")
            rejected = app._load_formal_suffix_preflight_stage(number, fingerprint, "structured_state_rejected")
            original_rejected = rejected
            rejected, patch, local_diagnostics = _resource_order_patch(rejected, patch, text)
            manual = {"schema_version": 1, "chapter": number,
                      "chapter_sha256": state_ledger.chapter_sha256(text),
                      "state_sha256": state_repair.digest(state),
                      "rejection_sha256": state_repair.digest(rejected), "patch": patch}
            # Validate binding/permissions/substance before any audit admission.
            state_repair.manual_candidate(manual, rejected, chapter=number,
                                         chapter_sha256=manual["chapter_sha256"], state=state)
            identity = state_repair.digest({"context": manual, "fingerprint": fingerprint})
            admitted = self.row.setdefault("manual_state_patches", [])
            previous_admission = next((item for item in admitted if item["identity"] == identity), None)
            if previous_admission is not None and not resume_unavailable:
                raise RuntimeError("本人工补丁已审计或已入场，禁止原样重复调用")
            budget_path = self.project / "runtime" / "chapter_budgets" / f"chapter_{number:04d}.json"
            budget_epoch = read_json(budget_path).get("epoch", "unstarted") if budget_path.exists() else "unstarted"
            resumed_from = None
            if resume_unavailable:
                if (previous_admission is None or not str(reason).strip()
                        or not _unavailable_admission(self.row, previous_admission)):
                    raise RuntimeError("显式恢复须为同一绑定的未完成服务审计并提供具体理由；语义拒绝不能恢复")
                if previous_admission.get("budget_epoch", budget_epoch) == budget_epoch:
                    raise RuntimeError("未完成服务恢复须先人工处理过期预算，不能自动续期或同epoch重试")
                if any(item.get("resumed_from") == identity for item in admitted):
                    raise RuntimeError("此未完成输入已显式恢复一次，禁止重复恢复或循环续期")
                resumed_from = identity
                identity = state_repair.digest({"resume_of": resumed_from, "resume_number": 1})
            prior_admissions = [item for item in admitted
                                if item.get("manual_context", {}).get("chapter") == number
                                and item.get("budget_epoch", budget_epoch) == budget_epoch]
            if len(prior_admissions) >= 2:
                raise RuntimeError("MANUAL_STATE_AUDIT_STOPPED：同章同预算阶段已入场两份人工补丁；保留问题，不自动第三轮或续期")
            if _manual_audit_window_seconds(self.project, number) < 120:
                raise RuntimeError("MANUAL_STATE_AUDIT_WINDOW_INSUFFICIENT：剩余不足120秒，未发起模型；保存补丁，不自动续期")
            repair_args = app._formal_suffix_state_repair_arguments(number, fingerprint)
            repair_args["repair_context"] = rejected
            app._begin_chapter_model_budget(number)
            # Some adapters create the durable record on begin; others create
            # it at their first actual call. Bind the real epoch, never let an
            # initial 'unstarted' entry evade the two-audit phase cap.
            budget_epoch = read_json(budget_path).get("epoch", "unstarted") if budget_path.exists() else budget_epoch
            self.row.update(status="RUNNING", phase="manual_state_audit", current_chapter=number, error="")
            admission = {"identity": identity, "patch_path": str(patch_file), "patch_sha256": sha256(patch_file),
                             "manual_context": manual, "fingerprint": fingerprint, "admitted_at": now(),
                             "original_rejection": original_rejected, "local_diagnostics": local_diagnostics,
                             "budget_epoch": budget_epoch}
            if resumed_from:
                admission.update(resumed_from=resumed_from, resume_reason=str(reason).strip())
            admitted.append(admission)
            self._event({"event": "manual_state_patch_audit_started", "chapter": number, "identity": identity,
                         "resumed_from": resumed_from})
            prepared = app._prepare_structured_state_delta(number, text, outline, state_override=state,
                                                          manual_patch_context=manual, **repair_args)
            app._validate_cached_state_preflight(prepared, number, text, state)
            app._store_formal_suffix_preflight_stage(number, fingerprint, "structured_state", prepared)
            admission["outcome"] = "PASSED"
            self.row.update(status="STATE_PREPARED", phase="isolated_preflight")
            self._check_sources()
            self._event({"event": "manual_state_patch_audit_passed", "chapter": number, "identity": identity})
        except Exception as exc:
            self.row.update(status="STOPPED", error=re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(exc)))
            if admission is not None and admission.get("outcome") != "PASSED":
                admission.update(outcome="UNAVAILABLE" if self.row["error"].startswith("独立证据审计调用未完成：") else "REJECTED",
                                 outcome_error=self.row["error"])
            self._event({"event": "manual_state_patch_stopped", "chapter": number, "error": self.row["error"]})
        finally:
            try:
                if admission is not None and budget_path is not None and budget_path.exists():
                    epoch = read_json(budget_path).get("epoch", "unstarted")
                    if admission["budget_epoch"] != epoch:
                        admission["budget_epoch"] = epoch
                        self._save()
            finally:
                try:
                    formal_suffix_rewrite.promote_isolated_preflight_cache(self.checkpoint, self.project)
                finally:
                    for lock in reversed(locks):
                        lock.release()
        return self.summary()

    def apply(self):
        """Only a complete isolated prefix may enter normal live adoption."""
        from gui_app import NovelGeneratorGUI
        from novel_cli import build_doctor_report
        locks = self._locks()
        try:
            self._check_sources()
            verified = verify_prefix(self.checkpoint, self.row["inputs"])
            end = int(self.row["formal_end"])
            if verified["tip"] != end:
                raise RuntimeError("隔离完整尾段未通过，不能正式采用")
            formal_suffix_rewrite.promote_isolated_preflight_cache(self.checkpoint, self.project)
            app = _make_app(self.project, self.project, self._event, locks[0])
            prepare = app._prepare_formal_suffix_rewrite

            def prepare_with_bundle(first, last):
                prepare(first, last)
                for name, entry in self.row["bundle"].items():
                    shutil.copy2(entry["path"], self.project / "plot" / name)
                app.refresh_status()  # Reflect rewound truth, not stale GUI tip.

            app._prepare_formal_suffix_rewrite = prepare_with_bundle
            self.row.update(status="RUNNING", phase="formal_adoption", formal_adoption="RUNNING")
            self._save()
            result = NovelGeneratorGUI.rewrite_formal_suffix_from_drafts(app, 1, end,
                draft_paths={int(n): entry["path"] for n, entry in self.row["inputs"].items() if int(n) <= end},
                progress_callback=self._event, micro_edit=False)
            if result.get("status") != "COMPLETED":
                raise RuntimeError("正常正式采用未完成")
            self.row["formal_adoption"] = "COMPLETED"
            # The next candidate is independently reviewed against adopted canon.
            entry = self.row["inputs"].get(str(end + 1))
            if entry:
                self.row.update(phase="next_chapter", next_chapter_commit="RUNNING")
                self._save()
                app._restore_project_bindings()
                volume = next((i for i, item in enumerate(app._get_volume_ranges(), 1)
                               if int(item[0]) <= end + 1 <= int(item[1])), 1)
                app._commit_manual_chapter(end + 1,
                    str(self.project / "output" / f"第{volume:02d}卷" / f"第{end+1:04d}章.txt"),
                    Path(entry["path"]).read_text(encoding="utf-8-sig"), "已授权采用后下一章单独审提")
                self.row["next_chapter_commit"] = "COMPLETED"
            post = build_doctor_report(self.project)
            self.row["post_doctor"] = post
            if post.get("overall_status") != "PASS":
                raise RuntimeError("采用后工程验收未通过")
            self.row.update(status="COMPLETED", phase="completed")
            self._save()
        except Exception as exc:
            self.row.update(status="STOPPED", error=re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(exc)))
            self._event({"event": "adoption_stopped", "error": self.row["error"]})
        finally:
            for lock in reversed(locks):
                lock.release()
        return self.summary()
