# -*- coding: utf-8 -*-
"""Transactional orchestration for rewriting a contiguous formal suffix.

The normal chapter commit journal protects one chapter.  Rewriting chapters
31-35 is a wider operation: every chapter after the first replacement depends
on the state produced by the previous one.  This module wraps those existing
per-chapter commits in a project-level snapshot and rolls the whole mutable
surface back if any chapter or final verification fails.

It deliberately does not manufacture review receipts, state deltas, or canon
snapshots.  Callers must use the existing reviewed chapter commit path in the
``commit_chapter`` callback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from copy import deepcopy
from difflib import SequenceMatcher
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import project_job_lock


CHAPTER_RE = re.compile(r"^第(\d+)章\.txt$")
MICRO_EDIT_SENSITIVE_TOKEN_RE = re.compile(
    r"(?:\d{1,4}|[零〇一二三四五六七八九十百千万两]{1,6})"
    r"(?:年|月|日|号|点|分|秒|分钟|小时|岁|名|次|章|比)"
)
MARKER_NAME = "pending_formal_suffix_rewrite.json"
ARCHIVE_DIRNAME = "formal_suffix_rewrite_archives"
PREFLIGHT_CACHE_DIRNAME = "formal_suffix_preflight_cache"
REWRITE_LOCK_NAME = "formal_suffix_rewrite.lock"
PREFLIGHT_CACHE_SCHEMA_VERSION = 1
PREFLIGHT_CACHE_MAX_PER_CHAPTER = 6
DEFAULT_TRACKED_PATHS = (
    "output",
    "publish",
    "plot",
    "history",
    "logs",
    "characters",
    "world_building",
)


class FormalSuffixRewriteError(RuntimeError):
    """Raised when a formal suffix cannot be committed or safely restored."""


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _acquire_rewrite_lock(
    project: Path,
    *,
    task: str,
    allow_pending: bool = False,
) -> tuple[Path, str]:
    """Atomically exclude overlapping suffix transactions and recovery."""

    runtime_dir = project / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / REWRITE_LOCK_NAME
    marker = runtime_dir / MARKER_NAME
    for _attempt in range(2):
        token = uuid.uuid4().hex
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "task": str(task or "正式尾段事务"),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "token": token,
        }
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            owner = _read_json_object(lock_path)
            owner_pid = int(owner.get("pid") or 0)
            if owner_pid and project_job_lock._pid_is_alive(owner_pid):
                raise FormalSuffixRewriteError(
                    "已有正式尾段任务正在运行："
                    f"{owner.get('task') or '未知任务'}（PID {owner_pid}）"
                )
            if marker.exists() and not allow_pending:
                raise FormalSuffixRewriteError(
                    "检测到中断的正式尾段事务；必须先恢复事务快照，禁止直接重启"
                )
            try:
                lock_path.unlink()
            except OSError as exc:
                raise FormalSuffixRewriteError(
                    "正式尾段任务锁已失效但无法清理"
                ) from exc
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            lock_path.unlink(missing_ok=True)
            raise
        return lock_path, token
    raise FormalSuffixRewriteError("无法取得正式尾段任务锁")


def _release_rewrite_lock(lock_path: Path, token: str) -> None:
    owner = _read_json_object(lock_path)
    if str(owner.get("token") or "") == str(token or ""):
        lock_path.unlink(missing_ok=True)


def _exclusive_rewrite(func):
    @wraps(func)
    def wrapped(project_dir, *args, **kwargs):
        project = Path(project_dir).resolve()
        lock_path, token = _acquire_rewrite_lock(
            project,
            task="正式尾段重写",
        )
        try:
            return func(project_dir, *args, **kwargs)
        finally:
            _release_rewrite_lock(lock_path, token)

    return wrapped


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _stable_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_preflight_fingerprint(payload: object) -> str:
    """Return the deterministic identity used by reusable model preflights."""

    return _stable_json_sha256(payload)


def _preflight_cache_path(project: Path, chapter: int, fingerprint: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint or "")):
        raise FormalSuffixRewriteError("正式尾段预检缓存指纹无效")
    cache_dir = project / ".runtime" / PREFLIGHT_CACHE_DIRNAME
    return cache_dir / f"chapter_{int(chapter):04d}_{fingerprint[:24]}.json"


def _preflight_record_digest(record: Mapping[str, Any]) -> str:
    material = {
        key: value
        for key, value in dict(record or {}).items()
        if key != "record_sha256"
    }
    return _stable_json_sha256(material)


def _load_preflight_record(
    project: Path,
    chapter: int,
    fingerprint: str,
    *, strict: bool = False,
) -> tuple[Path, dict[str, Any] | None]:
    path = _preflight_cache_path(project, chapter, fingerprint)
    if not path.is_file():
        return path, None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            record = json.load(handle)
    except Exception as exc:
        if strict:
            raise FormalSuffixRewriteError("状态修复记录无法读取") from exc
        return path, None
    if not isinstance(record, dict):
        if strict:
            raise FormalSuffixRewriteError("状态修复记录结构无效")
        return path, None
    if (
        int(record.get("schema_version") or 0) != PREFLIGHT_CACHE_SCHEMA_VERSION
        or int(record.get("chapter") or 0) != int(chapter)
        or str(record.get("context_fingerprint") or "") != fingerprint
        or str(record.get("record_sha256") or "")
        != _preflight_record_digest(record)
    ):
        if strict:
            raise FormalSuffixRewriteError("状态修复记录绑定或摘要无效")
        return path, None
    return path, record


def load_preflight_stage(
    project_dir: str | os.PathLike[str],
    *,
    chapter: int,
    context_fingerprint: str,
    stage: str,
    strict: bool = False,
) -> object | None:
    """Load one intact cached preflight stage for the exact predecessor context."""

    project = Path(project_dir).resolve()
    if not project.is_dir():
        return None
    _path, record = _load_preflight_record(
        project, int(chapter), str(context_fingerprint or ""), strict=strict
    )
    if not record:
        return None
    stage_row = dict(record.get("stages") or {}).get(str(stage or ""))
    if not isinstance(stage_row, dict) or "payload" not in stage_row:
        if strict and stage_row is not None:
            raise FormalSuffixRewriteError("状态修复阶段结构无效")
        return None
    payload = stage_row.get("payload")
    if str(stage_row.get("payload_sha256") or "") != _stable_json_sha256(payload):
        if strict:
            raise FormalSuffixRewriteError("状态修复阶段摘要无效")
        return None
    return deepcopy(payload)


def _preflight_cache_has_recovery_data(path: Path) -> bool:
    """Recovery/failed-attempt evidence is not a disposable approval cache."""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            record = json.load(handle)
        if not isinstance(record, dict) or record.get("record_sha256") != _preflight_record_digest(record):
            return True  # Preserve damaged evidence for diagnosis, never erase it.
        stages = record.get("stages")
        if not isinstance(stages, dict):
            return True
        for name, row in stages.items():
            if not isinstance(row, dict) or "payload" not in row:
                return True
            payload = row["payload"]
            if row.get("payload_sha256") != _stable_json_sha256(payload):
                return True
            if name == "state_only_candidate":
                return True
            if (isinstance(payload, dict)
                    and ((name.endswith("_rejected") and payload.get("status") == "REJECTED")
                         or (name.endswith("_pending") and payload.get("status") == "PENDING"))):
                return True
    except Exception:
        return True
    return False


def _prune_preflight_cache(cache_dir: Path, chapter: int, keep: Path) -> None:
    matches = sorted(
        cache_dir.glob(f"chapter_{int(chapter):04d}_*.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    retained = 0
    for path in matches:
        if _preflight_cache_has_recovery_data(path):
            continue
        if path == keep or retained < PREFLIGHT_CACHE_MAX_PER_CHAPTER:
            retained += 1
            continue
        path.unlink(missing_ok=True)


def store_preflight_stage(
    project_dir: str | os.PathLike[str],
    *,
    chapter: int,
    context_fingerprint: str,
    stage: str,
    payload: object,
) -> str:
    """Preserve typed preflight work outside rollback surfaces.

    The structured_state_rejected stage is repair input, not approval; callers
    must run fresh extraction and independent audit before storing passed state.
    """

    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise FormalSuffixRewriteError(f"项目目录不存在：{project}")
    chapter_number = int(chapter)
    if chapter_number < 1 or not str(stage or "").strip():
        raise FormalSuffixRewriteError("正式尾段预检缓存章号或阶段无效")
    path, record = _load_preflight_record(
        project, chapter_number, str(context_fingerprint or "")
    )
    now = datetime.now().isoformat(timespec="seconds")
    if not record:
        record = {
            "schema_version": PREFLIGHT_CACHE_SCHEMA_VERSION,
            "chapter": chapter_number,
            "context_fingerprint": str(context_fingerprint),
            "created_at": now,
            "stages": {},
        }
    stages = dict(record.get("stages") or {})
    stages[str(stage)] = {
        "payload": deepcopy(payload),
        "payload_sha256": _stable_json_sha256(payload),
        "stored_at": now,
    }
    record["stages"] = stages
    record["updated_at"] = now
    record["record_sha256"] = _preflight_record_digest(record)
    _atomic_write_json(path, record)
    _prune_preflight_cache(path.parent, chapter_number, path)
    return str(path)


def _emit_progress(
    callback: Callable[[dict[str, object]], None] | None,
    event: str,
    **payload: object,
) -> None:
    if not callback:
        return
    row = {"event": str(event), **payload}
    try:
        callback(row)
    except Exception:
        # Progress reporting is advisory and must never weaken the transaction.
        return


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_project_path(project: Path, relative: str) -> Path:
    root = project.resolve()
    target = (project / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise FormalSuffixRewriteError(
            f"事务路径越出项目目录：{relative}"
        ) from exc
    if target == root:
        raise FormalSuffixRewriteError("事务不得把项目根目录作为删除或恢复目标")
    return target


def _tree_manifest(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    if root.is_file():
        return {".": _file_sha256(root)}
    return {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


def _official_chapters(output_dir: Path) -> dict[int, Path]:
    result: dict[int, Path] = {}
    if not output_dir.is_dir():
        return result
    for path in sorted(output_dir.rglob("第*章.txt")):
        if any(part in {".backup", "superseded"} for part in path.parts):
            continue
        match = CHAPTER_RE.match(path.name)
        if not match:
            continue
        chapter = int(match.group(1))
        if chapter in result:
            raise FormalSuffixRewriteError(f"正式区存在重复第{chapter}章文件")
        result[chapter] = path
    return result


def validate_micro_edit_candidates(
    project_dir: str | os.PathLike[str],
    candidates: Mapping[int, str | os.PathLike[str]],
    *,
    max_changed_chars: int = 800,
    max_changed_ratio: float = 0.08,
) -> dict[str, Any]:
    """Prove that a suffix request is a bounded prose edit, not a hidden rewrite.

    Micro-edit mode is intentionally opt-in.  It permits deletions and small
    wording replacements, while refusing title changes, large rewrites, and new
    date/count tokens.  The normal release and narrative gates still run for
    changed chapters; this report only authorizes reuse of already-validated
    structured evidence when that evidence remains valid against the new text.
    """

    project = Path(project_dir).resolve()
    official = _official_chapters(project / "output")
    limit = max(1, int(max_changed_chars or 0))
    ratio_limit = max(0.001, float(max_changed_ratio or 0.0))
    rows: dict[str, dict[str, Any]] = {}
    changed: list[int] = []
    unchanged: list[int] = []

    for raw_chapter, raw_path in sorted(dict(candidates or {}).items()):
        chapter = int(raw_chapter)
        official_path = official.get(chapter)
        candidate_path = Path(raw_path).resolve()
        if official_path is None or not official_path.is_file():
            raise FormalSuffixRewriteError(f"微调缺少第{chapter}章正式原稿")
        if not candidate_path.is_file():
            raise FormalSuffixRewriteError(f"微调缺少第{chapter}章候选稿")
        old_text = official_path.read_text(encoding="utf-8-sig").strip()
        new_text = candidate_path.read_text(encoding="utf-8-sig").strip()
        if not new_text:
            raise FormalSuffixRewriteError(f"第{chapter}章微调候选稿为空")
        old_title = next((line.strip() for line in old_text.splitlines() if line.strip()), "")
        new_title = next((line.strip() for line in new_text.splitlines() if line.strip()), "")
        if old_title != new_title:
            raise FormalSuffixRewriteError(f"第{chapter}章微调不能修改章标题")

        matcher = SequenceMatcher(None, old_text, new_text, autojunk=False)
        changed_old = 0
        changed_new = 0
        added_fragments: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            changed_old += i2 - i1
            changed_new += j2 - j1
            if tag in {"insert", "replace"} and j2 > j1:
                added_fragments.append(new_text[j1:j2])
        changed_chars = max(changed_old, changed_new)
        changed_ratio = changed_chars / max(1, len(old_text))
        sensitive = sorted(set(MICRO_EDIT_SENSITIVE_TOKEN_RE.findall("".join(added_fragments))))
        if changed_chars > limit or changed_ratio > ratio_limit:
            raise FormalSuffixRewriteError(
                f"第{chapter}章改动超过微调上限：{changed_chars}字符/{changed_ratio:.2%}"
            )
        if sensitive:
            raise FormalSuffixRewriteError(
                f"第{chapter}章微调新增时间或数量事实：{'、'.join(sensitive[:6])}"
            )
        row = {
            "chapter": chapter,
            "changed": bool(changed_chars),
            "changed_chars": changed_chars,
            "changed_ratio": round(changed_ratio, 6),
            "removed_chars": changed_old,
            "added_chars": changed_new,
        }
        rows[str(chapter)] = row
        (changed if changed_chars else unchanged).append(chapter)

    if not changed:
        raise FormalSuffixRewriteError("微调候选稿与正式正文完全一致，无需启动事务")
    return {
        "mode": "micro_edit",
        "changed_chapters": changed,
        "unchanged_chapters": unchanged,
        "chapters": rows,
        "limits": {
            "max_changed_chars": limit,
            "max_changed_ratio": ratio_limit,
        },
    }


def resolve_sparse_micro_edit_candidates(
    project_dir: str | os.PathLike[str],
    candidates: Mapping[int, str | os.PathLike[str]],
    *,
    start_chapter: int,
    end_chapter: int,
) -> dict[int, Path]:
    """Fill unchanged chapters from formal output while keeping explicit edits sparse."""
    project = Path(project_dir).resolve()
    start = int(start_chapter)
    end = int(end_chapter)
    if start < 1 or end < start:
        raise FormalSuffixRewriteError("微调章节范围无效")
    official = _official_chapters(project / "output")
    latest = max(official, default=0)
    if end != latest:
        raise FormalSuffixRewriteError(
            f"微调必须覆盖至当前正式末章；当前第{latest}章，请将 end_chapter 设为{latest}"
        )
    overrides = {int(chapter): Path(path).resolve() for chapter, path in dict(candidates or {}).items()}
    outside = sorted(chapter for chapter in overrides if chapter < start or chapter > end)
    if outside:
        raise FormalSuffixRewriteError(
            "候选稿包含范围外章节：" + "、".join(map(str, outside[:12]))
        )
    missing_overrides = [chapter for chapter, path in overrides.items() if not path.is_file()]
    if missing_overrides:
        raise FormalSuffixRewriteError(
            "候选稿不存在：第" + "、".join(map(str, sorted(missing_overrides))) + "章"
        )
    resolved: dict[int, Path] = {}
    for chapter in range(start, end + 1):
        source = overrides.get(chapter) or official.get(chapter)
        if source is None or not Path(source).is_file():
            raise FormalSuffixRewriteError(f"微调尾段缺少第{chapter}章正文")
        resolved[chapter] = Path(source).resolve()
    return resolved


def _normalize_candidates(
    candidates: Mapping[int, str | os.PathLike[str]],
    start_chapter: int,
    end_chapter: int,
) -> dict[int, Path]:
    normalized = {int(number): Path(path).resolve() for number, path in candidates.items()}
    expected = list(range(int(start_chapter), int(end_chapter) + 1))
    if sorted(normalized) != expected:
        raise FormalSuffixRewriteError(
            "候选稿必须完整覆盖连续尾段：" + ",".join(str(item) for item in expected)
        )
    for chapter, path in normalized.items():
        if not path.is_file():
            raise FormalSuffixRewriteError(f"第{chapter}章候选稿不存在：{path}")
    return normalized


def create_isolated_preflight_project(
    project_dir: str | os.PathLike[str],
    destination_dir: str | os.PathLike[str],
    candidates: Mapping[int, str | os.PathLike[str]],
) -> tuple[Path, dict[int, Path]]:
    """Clone a project for full suffix validation without touching live state."""

    project = Path(project_dir).resolve()
    destination = Path(destination_dir).resolve()
    if not project.is_dir():
        raise FormalSuffixRewriteError(f"项目目录不存在：{project}")
    if destination.exists():
        raise FormalSuffixRewriteError(f"隔离预审目录已存在：{destination}")
    try:
        destination.relative_to(project)
    except ValueError:
        pass
    else:
        raise FormalSuffixRewriteError("隔离预审目录不得位于原项目内部")

    def ignore_runtime(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        ignored: set[str] = set()
        if current == project:
            ignored.update({".runtime", project_job_lock.LOCK_FILENAME})
        return ignored.intersection(names)

    shutil.copytree(
        project,
        destination,
        copy_function=shutil.copy2,
        ignore=ignore_runtime,
    )
    runtime = destination / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    source_cache = project / ".runtime" / PREFLIGHT_CACHE_DIRNAME
    if source_cache.is_dir():
        shutil.copytree(
            source_cache,
            runtime / PREFLIGHT_CACHE_DIRNAME,
            copy_function=shutil.copy2,
        )

    isolated_candidates: dict[int, Path] = {}
    candidate_dir = runtime / "isolated_suffix_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    for chapter, source in sorted(
        (int(number), Path(path).resolve()) for number, path in candidates.items()
    ):
        if not source.is_file():
            raise FormalSuffixRewriteError(f"第{chapter}章候选稿不存在：{source}")
        target = candidate_dir / f"第{chapter:04d}章.txt"
        shutil.copy2(source, target)
        isolated_candidates[chapter] = target
    return destination, isolated_candidates


def promote_isolated_preflight_cache(
    isolated_project_dir: str | os.PathLike[str],
    project_dir: str | os.PathLike[str],
) -> int:
    """Merge integrity-checked work records, preserving their distinct stage names."""

    isolated = Path(isolated_project_dir).resolve()
    project = Path(project_dir).resolve()
    source_cache = isolated / ".runtime" / PREFLIGHT_CACHE_DIRNAME
    if not source_cache.is_dir():
        return 0
    target_cache = project / ".runtime" / PREFLIGHT_CACHE_DIRNAME
    target_cache.mkdir(parents=True, exist_ok=True)
    promoted = 0
    for source in sorted(source_cache.glob("chapter_*.json")):
        record = _read_json_object(source)
        if not record:
            continue
        chapter = int(record.get("chapter") or 0)
        fingerprint = str(record.get("context_fingerprint") or "")
        if chapter < 1 or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            continue
        if str(record.get("record_sha256") or "") != _preflight_record_digest(record):
            continue
        target = _preflight_cache_path(project, chapter, fingerprint)
        current = _read_json_object(target)
        if current and str(current.get("record_sha256") or "") == str(
            record.get("record_sha256") or ""
        ):
            continue
        _atomic_write_json(target, record)
        _prune_preflight_cache(target.parent, chapter, target)
        promoted += 1
    return promoted


def _copy_snapshot(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copy2)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _remove_exact_path(project: Path, target: Path) -> None:
    _safe_project_path(project, str(target.resolve().relative_to(project.resolve())))
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _restore_snapshot(
    project: Path,
    archive_dir: Path,
    tracked_paths: Iterable[str],
    existed: Mapping[str, bool],
) -> None:
    snapshot_root = archive_dir / "snapshot"
    for relative in tracked_paths:
        target = _safe_project_path(project, relative)
        if target.exists():
            _remove_exact_path(project, target)
        if existed.get(relative):
            source = snapshot_root / relative
            _copy_snapshot(source, target)


def _verify_manifest(
    project: Path,
    tracked_paths: Iterable[str],
    expected: Mapping[str, Mapping[str, str]],
) -> list[str]:
    mismatches = []
    for relative in tracked_paths:
        actual = _tree_manifest(_safe_project_path(project, relative))
        wanted = dict(expected.get(relative) or {})
        if actual != wanted:
            mismatches.append(relative)
    return mismatches


@_exclusive_rewrite
def execute_formal_suffix_rewrite(
    project_dir: str | os.PathLike[str],
    *,
    start_chapter: int,
    end_chapter: int,
    candidates: Mapping[int, str | os.PathLike[str]],
    prepare_suffix: Callable[[int, int], None],
    commit_chapter: Callable[[int, Path, str], None],
    verify_suffix: Callable[[int, int], None] | None = None,
    tracked_paths: Iterable[str] = DEFAULT_TRACKED_PATHS,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Rewrite the current formal tail using already-reviewed commit logic.

    ``prepare_suffix`` must restore derived state to immediately before
    ``start_chapter`` and remove the current formal suffix.  ``commit_chapter``
    must call the normal per-chapter validation/review/state/commit path.  The
    transaction snapshots every tracked mutable directory before either
    callback runs.
    """

    project = Path(project_dir).resolve()
    start = int(start_chapter)
    end = int(end_chapter)
    if start < 1 or end < start:
        raise FormalSuffixRewriteError("正式尾段范围无效")
    if not project.is_dir():
        raise FormalSuffixRewriteError(f"项目目录不存在：{project}")

    tracked = tuple(dict.fromkeys(str(item) for item in tracked_paths))
    if not tracked:
        raise FormalSuffixRewriteError("事务至少需要跟踪一个可变路径")
    for relative in tracked:
        _safe_project_path(project, relative)

    candidate_paths = _normalize_candidates(candidates, start, end)
    official = _official_chapters(project / "output")
    if not official:
        raise FormalSuffixRewriteError("正式区没有可重写章节")
    latest = max(official)
    if latest != end:
        raise FormalSuffixRewriteError(
            f"只能重写当前正式尾段；当前最新章为{latest}，请求末章为{end}"
        )
    missing = [chapter for chapter in range(start, end + 1) if chapter not in official]
    if missing:
        raise FormalSuffixRewriteError(
            "正式尾段不连续，缺少第" + ",".join(str(item) for item in missing) + "章"
        )

    pending_chapter = project / "plot" / "runtime" / "pending_chapter_commit.json"
    if pending_chapter.exists():
        raise FormalSuffixRewriteError("存在未完成的单章正式提交，拒绝开始尾段重写")

    runtime_dir = project / ".runtime"
    marker = runtime_dir / MARKER_NAME
    if marker.exists():
        raise FormalSuffixRewriteError("已有未决正式尾段事务，需先恢复或核验")

    transaction_id = _now_id()
    archive_dir = runtime_dir / ARCHIVE_DIRNAME / transaction_id
    snapshot_root = archive_dir / "snapshot"
    archive_dir.mkdir(parents=True, exist_ok=False)
    _emit_progress(
        progress_callback,
        "snapshot_started",
        transaction_id=transaction_id,
        start_chapter=start,
        end_chapter=end,
    )

    existed: dict[str, bool] = {}
    original_manifest: dict[str, dict[str, str]] = {}
    for relative in tracked:
        source = _safe_project_path(project, relative)
        existed[relative] = source.exists()
        original_manifest[relative] = _tree_manifest(source)
        if source.exists():
            _copy_snapshot(source, snapshot_root / relative)
    _emit_progress(
        progress_callback,
        "snapshot_completed",
        transaction_id=transaction_id,
        tracked_paths=len(tracked),
    )

    prefix_hashes = {
        str(chapter): _file_sha256(path)
        for chapter, path in official.items()
        if chapter < start
    }
    # Candidates may intentionally reuse the current formal files for unchanged
    # tail chapters.  Preload them before prepare_suffix removes that tail.
    candidate_texts = {
        chapter: path.read_text(encoding="utf-8-sig").strip()
        for chapter, path in candidate_paths.items()
    }
    candidate_hashes = {
        str(chapter): _text_sha256(text)
        for chapter, text in candidate_texts.items()
    }
    manifest = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "project": str(project),
        "start_chapter": start,
        "end_chapter": end,
        "tracked_paths": list(tracked),
        "tracked_path_existed": existed,
        "original_manifest": original_manifest,
        "prefix_hashes": prefix_hashes,
        "candidate_hashes": candidate_hashes,
        "candidate_paths": {str(key): str(value) for key, value in candidate_paths.items()},
        "status": "prepared",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest_path = archive_dir / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    _atomic_write_json(marker, manifest)

    try:
        _emit_progress(
            progress_callback,
            "prepare_started",
            transaction_id=transaction_id,
            start_chapter=start,
            end_chapter=end,
        )
        prepare_suffix(start, end)
        _emit_progress(
            progress_callback,
            "prepare_completed",
            transaction_id=transaction_id,
        )
        for chapter in range(start, end + 1):
            _emit_progress(
                progress_callback,
                "chapter_started",
                transaction_id=transaction_id,
                chapter=chapter,
                completed=chapter - start,
                total=end - start + 1,
            )
            text = candidate_texts[chapter]
            commit_chapter(chapter, official[chapter], text)
            _emit_progress(
                progress_callback,
                "chapter_completed",
                transaction_id=transaction_id,
                chapter=chapter,
                completed=chapter - start + 1,
                total=end - start + 1,
            )

        final_official = _official_chapters(project / "output")
        if max(final_official or {0: Path()} ) != end:
            raise FormalSuffixRewriteError("尾段提交后正式末章不正确")
        for chapter in range(start, end + 1):
            path = final_official.get(chapter)
            if not path:
                raise FormalSuffixRewriteError(f"尾段提交后缺少第{chapter}章")
            actual = _text_sha256(path.read_text(encoding="utf-8-sig"))
            if actual != candidate_hashes[str(chapter)]:
                raise FormalSuffixRewriteError(f"第{chapter}章正式正文与候选稿哈希不一致")
        for chapter_text, expected_hash in prefix_hashes.items():
            chapter = int(chapter_text)
            path = final_official.get(chapter)
            if not path or _file_sha256(path) != expected_hash:
                raise FormalSuffixRewriteError(f"封存前缀第{chapter}章在事务中发生变化")
        if verify_suffix:
            _emit_progress(
                progress_callback,
                "verification_started",
                transaction_id=transaction_id,
            )
            verify_suffix(start, end)
            _emit_progress(
                progress_callback,
                "verification_completed",
                transaction_id=transaction_id,
            )

        manifest["status"] = "completed"
        manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(manifest_path, manifest)
        marker.unlink(missing_ok=True)
        _emit_progress(
            progress_callback,
            "completed",
            transaction_id=transaction_id,
            start_chapter=start,
            end_chapter=end,
        )
        return {
            "status": "COMPLETED",
            "transaction_id": transaction_id,
            "archive_dir": str(archive_dir),
            "start_chapter": start,
            "end_chapter": end,
            "candidate_hashes": candidate_hashes,
        }
    except Exception as exc:
        _emit_progress(
            progress_callback,
            "rollback_started",
            transaction_id=transaction_id,
            error=str(exc),
        )
        rollback_error = ""
        try:
            _restore_snapshot(project, archive_dir, tracked, existed)
            mismatches = _verify_manifest(project, tracked, original_manifest)
            if mismatches:
                raise FormalSuffixRewriteError(
                    "回滚后仍有路径与原始清单不一致：" + ",".join(mismatches)
                )
        except Exception as restore_exc:  # pragma: no cover - catastrophic path
            rollback_error = str(restore_exc)
        manifest["status"] = "rolled_back" if not rollback_error else "rollback_failed"
        manifest["failed_at"] = datetime.now().isoformat(timespec="seconds")
        manifest["error"] = str(exc)
        manifest["rollback_error"] = rollback_error
        _atomic_write_json(manifest_path, manifest)
        marker.unlink(missing_ok=True)
        _emit_progress(
            progress_callback,
            "rollback_completed" if not rollback_error else "rollback_failed",
            transaction_id=transaction_id,
            error=str(exc),
            rollback_error=rollback_error,
        )
        if rollback_error:
            raise FormalSuffixRewriteError(
                f"正式尾段重写失败，且回滚核验失败：{rollback_error}；原错误：{exc}"
            ) from exc
        raise FormalSuffixRewriteError(
            f"正式尾段重写失败，已恢复事务前状态：{exc}"
        ) from exc


def recover_pending_formal_suffix_rewrite(
    project_dir: str | os.PathLike[str],
) -> dict[str, object]:
    """Restore one interrupted suffix transaction from its sealed snapshot.

    Recovery is intentionally explicit. It refuses a live owner, validates
    the archived manifest, restores every tracked path, verifies the original
    hashes, and only then removes the pending marker.
    """

    project = Path(project_dir).resolve()
    if not project.is_dir():
        raise FormalSuffixRewriteError(f"项目目录不存在：{project}")
    runtime_dir = project / ".runtime"
    marker = runtime_dir / MARKER_NAME
    if not marker.is_file():
        return {"status": "NO_PENDING", "project": str(project)}

    lock_path, token = _acquire_rewrite_lock(
        project,
        task="恢复中断的正式尾段事务",
        allow_pending=True,
    )
    try:
        pending = _read_json_object(marker)
        transaction_id = str(pending.get("transaction_id") or "")
        if not re.fullmatch(r"\d{8}_\d{6}_\d{6}", transaction_id):
            raise FormalSuffixRewriteError("未决正式尾段事务编号无效")
        recorded_project = Path(str(pending.get("project") or "")).resolve()
        if recorded_project != project:
            raise FormalSuffixRewriteError("未决正式尾段事务不属于当前项目")

        archive_dir = runtime_dir / ARCHIVE_DIRNAME / transaction_id
        manifest_path = archive_dir / "manifest.json"
        manifest = _read_json_object(manifest_path)
        if not manifest or manifest.get("transaction_id") != transaction_id:
            raise FormalSuffixRewriteError("中断事务缺少有效归档清单")
        tracked = tuple(str(item) for item in manifest.get("tracked_paths") or ())
        existed = dict(manifest.get("tracked_path_existed") or {})
        original_manifest = dict(manifest.get("original_manifest") or {})
        if not tracked or not (archive_dir / "snapshot").is_dir():
            raise FormalSuffixRewriteError("中断事务快照不完整")
        for relative in tracked:
            _safe_project_path(project, relative)
            if existed.get(relative) and not (archive_dir / "snapshot" / relative).exists():
                raise FormalSuffixRewriteError(f"中断事务快照缺少路径：{relative}")

        _restore_snapshot(project, archive_dir, tracked, existed)
        mismatches = _verify_manifest(project, tracked, original_manifest)
        if mismatches:
            raise FormalSuffixRewriteError(
                "恢复后仍有路径与原始清单不一致：" + ",".join(mismatches)
            )

        manifest["status"] = "recovered_after_interruption"
        manifest["recovered_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(manifest_path, manifest)
        marker.unlink()
        return {
            "status": "RECOVERED",
            "transaction_id": transaction_id,
            "archive_dir": str(archive_dir),
            "start_chapter": int(manifest.get("start_chapter") or 0),
            "end_chapter": int(manifest.get("end_chapter") or 0),
        }
    finally:
        _release_rewrite_lock(lock_path, token)


__all__ = [
    "DEFAULT_TRACKED_PATHS",
    "FormalSuffixRewriteError",
    "PREFLIGHT_CACHE_DIRNAME",
    "PREFLIGHT_CACHE_SCHEMA_VERSION",
    "REWRITE_LOCK_NAME",
    "build_preflight_fingerprint",
    "create_isolated_preflight_project",
    "execute_formal_suffix_rewrite",
    "load_preflight_stage",
    "promote_isolated_preflight_cache",
    "recover_pending_formal_suffix_rewrite",
    "store_preflight_stage",
]
