"""Fail-closed planning for repairing historical hard-gate failures.

The module never edits an official chapter.  It snapshots an immutable frozen
source, asks an injected callback to rebuild a continuous suffix into an
isolated shadow directory, then asks another injected callback to review the
entire range from chapter one through the gate.  A finalize plan is emitted
only when that full-range review is explicit, complete, and hash-bound.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping


SCHEMA_VERSION = 1
MAX_REPAIR_ROUNDS = 2
CHAPTER_FILENAME_RE = re.compile(
    r"(?:chapter_(\d{4})|第(\d{4})章)\.txt",
    re.IGNORECASE,
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class GateMigrationError(RuntimeError):
    """The migration cannot safely continue."""


@dataclass(frozen=True)
class FrozenSnapshot:
    root: Path
    gate_chapter: int
    paths: Mapping[int, Path]
    texts: Mapping[int, str]
    chapter_hashes: Mapping[int, str]
    snapshot_sha256: str


@dataclass(frozen=True)
class RebuildRequest:
    gate_chapter: int
    round_number: int
    problem_chapters: tuple[int, ...]
    earliest_problem_chapter: int
    rebuild_chapters: tuple[int, ...]
    source_texts: Mapping[int, str]
    source_chapter_hashes: Mapping[int, str]
    frozen_chapter_hashes: Mapping[int, str]
    frozen_snapshot_sha256: str
    shadow_dir: Path
    gate_result: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewRequest:
    gate_chapter: int
    round_number: int
    expected_chapters: tuple[int, ...]
    chapter_texts: Mapping[int, str]
    chapter_hashes: Mapping[int, str]
    shadow_dir: Path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(text: str) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    return _sha256_bytes(normalized.encode("utf-8"))


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _strict_gate_chapter(value: Any) -> int:
    if isinstance(value, bool):
        raise GateMigrationError("gate chapter must be a positive integer")
    try:
        chapter = int(value)
    except (TypeError, ValueError) as exc:
        raise GateMigrationError("gate chapter must be a positive integer") from exc
    if chapter < 1:
        raise GateMigrationError("gate chapter must be a positive integer")
    return chapter


def _discover_chapter_paths(source: os.PathLike[str] | str) -> tuple[Path, dict[int, Path]]:
    root = Path(source).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise GateMigrationError("frozen chapter source must be a real directory")
    rows: dict[int, Path] = {}
    for path in root.iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        match = CHAPTER_FILENAME_RE.fullmatch(path.name)
        if not match:
            continue
        chapter = int(match.group(1) or match.group(2))
        if chapter in rows:
            raise GateMigrationError(f"duplicate frozen chapter number: {chapter}")
        rows[chapter] = path.resolve(strict=True)
    return root, rows


def snapshot_frozen_source(
    source: os.PathLike[str] | str,
    gate_chapter: int,
) -> FrozenSnapshot:
    """Read and bind the exact frozen range ``1..gate_chapter``."""
    gate = _strict_gate_chapter(gate_chapter)
    root, discovered = _discover_chapter_paths(source)
    expected = set(range(1, gate + 1))
    present = {number for number in discovered if number <= gate}
    missing = sorted(expected - present)
    if missing:
        raise GateMigrationError(
            "frozen chapter source is discontinuous; missing chapter(s): "
            + ", ".join(str(number) for number in missing[:20])
        )
    unexpected = sorted(number for number in discovered if number < 1)
    if unexpected:
        raise GateMigrationError("frozen chapter source contains invalid numbering")

    paths: dict[int, Path] = {}
    texts: dict[int, str] = {}
    hashes: dict[int, str] = {}
    for chapter in range(1, gate + 1):
        path = discovered[chapter]
        if not _is_relative_to(path, root):
            raise GateMigrationError("frozen chapter path escapes its source directory")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise GateMigrationError(
                f"frozen chapter {chapter} is unreadable as UTF-8"
            ) from exc
        if not text.strip():
            raise GateMigrationError(f"frozen chapter {chapter} is empty")
        paths[chapter] = path
        texts[chapter] = text
        hashes[chapter] = _sha256_bytes(path.read_bytes())

    snapshot_digest = _canonical_digest(
        [{"chapter": number, "sha256": hashes[number]} for number in range(1, gate + 1)]
    )
    return FrozenSnapshot(
        root=root,
        gate_chapter=gate,
        paths=MappingProxyType(paths),
        texts=MappingProxyType(texts),
        chapter_hashes=MappingProxyType(hashes),
        snapshot_sha256=snapshot_digest,
    )


def verify_frozen_source(snapshot: FrozenSnapshot) -> None:
    """Reject any byte, path, or numbering drift since the snapshot."""
    current = snapshot_frozen_source(snapshot.root, snapshot.gate_chapter)
    if dict(current.chapter_hashes) != dict(snapshot.chapter_hashes):
        raise GateMigrationError("frozen chapter source SHA drift detected")
    if current.snapshot_sha256 != snapshot.snapshot_sha256:
        raise GateMigrationError("frozen chapter source snapshot drift detected")


def _result_is_explicit_failure(result: Mapping[str, Any]) -> bool:
    status = str(result.get("status") or result.get("overall") or "").upper()
    current = str(result.get("current") or "").upper()
    action = str(result.get("action") or "").upper()
    if status not in {"PASS", "WARN", "FAIL"}:
        raise GateMigrationError("gate result is missing a valid status")
    if current not in {"PASS", "FAIL"}:
        raise GateMigrationError("gate result is missing a valid current verdict")
    if action not in {"CONTINUE", "ADJUST", "PAUSE"}:
        raise GateMigrationError("gate result is missing a valid action")
    return not (status == "PASS" and current == "PASS" and action == "CONTINUE")


def _coerce_problem_numbers(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        raise GateMigrationError("problem_chapters must be a chapter list")
    chapters: list[int] = []
    for item in values:
        if isinstance(item, bool):
            raise GateMigrationError("problem_chapters contains an invalid value")
        match = re.fullmatch(r"\s*(?:第|chapter\s*)?(\d+)(?:章)?\s*", str(item), re.I)
        if not match:
            raise GateMigrationError("problem_chapters contains an invalid value")
        chapters.append(int(match.group(1)))
    return chapters


def locate_problem_chapters(
    gate_result: Mapping[str, Any],
    gate_chapter: int,
) -> tuple[int, ...]:
    """Return sorted problem chapters, using conservative legacy text fallback."""
    if not isinstance(gate_result, Mapping):
        raise GateMigrationError("gate result must be a mapping")
    gate = _strict_gate_chapter(gate_chapter)
    if not _result_is_explicit_failure(gate_result):
        raise GateMigrationError("gate result already passes; migration is not justified")

    chapters = _coerce_problem_numbers(gate_result.get("problem_chapters"))
    if not chapters:
        legacy_parts = [
            gate_result.get("raw"),
            gate_result.get("summary"),
            gate_result.get("gate_block_reason"),
        ]
        legacy_parts.extend(gate_result.get("risks") or [])
        legacy_parts.extend(gate_result.get("next_actions") or [])
        legacy = "\n".join(str(part or "") for part in legacy_parts)
        mentions = re.findall(
            r"(?:第\s*(\d+)\s*章|chapter\s*(\d+))", legacy, re.I
        )
        chapters = [
            int(number)
            for alternatives in mentions
            for number in alternatives
            if number
        ]
    chapters = sorted(set(chapters))
    if not chapters:
        raise GateMigrationError(
            "failed gate does not identify any problem chapter; refusing blind rewrite"
        )
    invalid = [number for number in chapters if number < 1 or number > gate]
    if invalid:
        raise GateMigrationError(
            "problem chapter is outside the reviewed gate range: "
            + ", ".join(str(number) for number in invalid)
        )
    return tuple(chapters)


def plan_suffix_rebuild(
    gate_result: Mapping[str, Any],
    gate_chapter: int,
    *,
    round_number: int,
) -> dict[str, Any]:
    """Create the minimal safe suffix plan for one repair round."""
    if isinstance(round_number, bool) or not isinstance(round_number, int):
        raise GateMigrationError("repair round must be an integer")
    if round_number < 1 or round_number > MAX_REPAIR_ROUNDS:
        raise GateMigrationError(f"repair round must be between 1 and {MAX_REPAIR_ROUNDS}")
    gate = _strict_gate_chapter(gate_chapter)
    problems = locate_problem_chapters(gate_result, gate)
    earliest = min(problems)
    return {
        "round": round_number,
        "problem_chapters": list(problems),
        "earliest_problem_chapter": earliest,
        "rebuild_chapters": list(range(earliest, gate + 1)),
    }


def _prepare_shadow_root(source_root: Path, shadow_root: os.PathLike[str] | str) -> Path:
    raw_shadow = Path(shadow_root).absolute()
    source = source_root.resolve()
    # Resolve even a not-yet-created leaf so a symlink/junction in any existing
    # parent cannot disguise that the requested output actually falls inside
    # the immutable source.  Link-like parents are rejected outright as an
    # additional Windows fail-closed boundary.
    for component in (raw_shadow, *raw_shadow.parents):
        # is_symlink remains true for a broken link, while exists is false.
        if component.is_symlink():
            raise GateMigrationError("shadow output cannot use a symlink or junction parent")
        is_junction = getattr(component, "is_junction", lambda: False)
        if component.exists() and is_junction():
            raise GateMigrationError("shadow output cannot use a symlink or junction parent")
    shadow = raw_shadow.resolve(strict=False)
    # Neither tree may contain the other: both cases risk source mutation.
    if _is_relative_to(shadow, source) or _is_relative_to(source, shadow):
        raise GateMigrationError("shadow output must be isolated from frozen source")
    if shadow.exists():
        if not shadow.is_dir() or any(shadow.iterdir()):
            raise GateMigrationError("shadow output must be absent or empty")
    else:
        shadow.mkdir(parents=True)
    return shadow.resolve(strict=True)


def _write_shadow_round(
    round_dir: Path,
    rebuild_chapters: tuple[int, ...],
    rewritten: Mapping[int, str],
) -> tuple[dict[int, str], dict[int, str]]:
    if not isinstance(rewritten, Mapping):
        raise GateMigrationError("rebuild callback must return a chapter-to-text mapping")
    normalized: dict[int, str] = {}
    for raw_number, raw_text in rewritten.items():
        if isinstance(raw_number, bool):
            raise GateMigrationError("rebuild callback returned an invalid chapter number")
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as exc:
            raise GateMigrationError(
                "rebuild callback returned an invalid chapter number"
            ) from exc
        if number in normalized:
            raise GateMigrationError("rebuild callback returned duplicate chapter numbers")
        text = str(raw_text or "")
        if not text.strip():
            raise GateMigrationError(f"rebuilt chapter {number} is empty")
        normalized[number] = text
    if set(normalized) != set(rebuild_chapters):
        missing = sorted(set(rebuild_chapters) - set(normalized))
        extra = sorted(set(normalized) - set(rebuild_chapters))
        raise GateMigrationError(
            f"rebuild callback did not return the exact continuous suffix; missing={missing}, extra={extra}"
        )
    round_dir.mkdir(parents=False)
    hashes: dict[int, str] = {}
    for number in rebuild_chapters:
        path = round_dir / f"chapter_{number:04d}.txt"
        data = normalized[number].encode("utf-8")
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        hashes[number] = _sha256_bytes(data)
    return normalized, hashes


def _review_passes_full_range(
    review: Mapping[str, Any],
    expected: tuple[int, ...],
    chapter_hashes: Mapping[int, str],
) -> bool:
    if not isinstance(review, Mapping):
        raise GateMigrationError("full-range review callback must return a mapping")
    required = ("status", "current", "action", "reviewed_chapters", "chapter_hashes")
    missing = [field for field in required if field not in review]
    if missing:
        raise GateMigrationError(
            "full-range review is missing required field(s): " + ", ".join(missing)
        )
    try:
        reviewed = tuple(int(item) for item in review.get("reviewed_chapters") or [])
        recorded_hashes = {
            int(number): str(digest).lower()
            for number, digest in dict(review.get("chapter_hashes") or {}).items()
        }
    except (TypeError, ValueError) as exc:
        raise GateMigrationError("full-range review coverage or hashes are invalid") from exc
    if reviewed != expected:
        raise GateMigrationError("full-range review did not cover every chapter in order")
    expected_hashes = {number: digest.lower() for number, digest in chapter_hashes.items()}
    if recorded_hashes != expected_hashes:
        raise GateMigrationError("full-range review chapter hashes do not match candidates")
    if any(not SHA256_RE.fullmatch(digest) for digest in recorded_hashes.values()):
        raise GateMigrationError("full-range review contains an invalid SHA-256")
    if review.get("malformed") or review.get("gate_blocked"):
        return False
    return (
        str(review.get("status") or "").upper() == "PASS"
        and str(review.get("current") or "").upper() == "PASS"
        and str(review.get("action") or "").upper() == "CONTINUE"
    )


def _require_review_verifier(
    review: Mapping[str, Any],
    request: ReviewRequest,
    review_verifier_fn: Callable[[Mapping[str, Any], ReviewRequest], bool] | None,
) -> None:
    if not callable(review_verifier_fn):
        raise GateMigrationError("an independent review_verifier_fn is required")
    try:
        verified = review_verifier_fn(MappingProxyType(dict(review)), request)
    except Exception as exc:
        raise GateMigrationError("independent review verification raised an error") from exc
    if verified is not True:
        raise GateMigrationError("independent review verification did not return explicit True")


def _materialize_full_candidate(
    shadow_root: Path,
    round_number: int,
    expected: tuple[int, ...],
    candidate_texts: Mapping[int, str],
) -> Path:
    """Write a complete, self-contained final candidate into the shadow tree."""
    candidate_dir = shadow_root / f"final_candidate_round_{round_number:02d}"
    if candidate_dir.exists() or candidate_dir.is_symlink():
        raise GateMigrationError("final candidate shadow output already exists")
    candidate_dir.mkdir(parents=False)
    for number in expected:
        path = candidate_dir / f"chapter_{number:04d}.txt"
        with path.open("xb") as handle:
            handle.write(candidate_texts[number].encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    return candidate_dir


def run_gate_migration(
    *,
    gate_result: Mapping[str, Any],
    gate_chapter: int,
    frozen_source: os.PathLike[str] | str,
    shadow_root: os.PathLike[str] | str,
    rebuild_fn: Callable[[RebuildRequest], Mapping[int, str]],
    full_review_fn: Callable[[ReviewRequest], Mapping[str, Any]],
    review_verifier_fn: Callable[[Mapping[str, Any], ReviewRequest], bool] | None = None,
    max_rounds: int = MAX_REPAIR_ROUNDS,
) -> dict[str, Any]:
    """Run at most two isolated repair rounds and return a sealed plan.

    ``READY_TO_FINALIZE`` means only that a caller may proceed to a separate,
    transactional promotion step after calling :func:`validate_finalize_plan`.
    This function never mutates or promotes the frozen source.
    """
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise GateMigrationError("max_rounds must be an integer")
    if max_rounds < 1 or max_rounds > MAX_REPAIR_ROUNDS:
        raise GateMigrationError(f"max_rounds cannot exceed {MAX_REPAIR_ROUNDS}")
    if not callable(rebuild_fn) or not callable(full_review_fn):
        raise GateMigrationError("migration callbacks must be callable")
    if not callable(review_verifier_fn):
        raise GateMigrationError("an independent review_verifier_fn is required")

    snapshot = snapshot_frozen_source(frozen_source, gate_chapter)
    shadow = _prepare_shadow_root(snapshot.root, shadow_root)
    expected = tuple(range(1, snapshot.gate_chapter + 1))
    candidate_texts = dict(snapshot.texts)
    current_result: Mapping[str, Any] = gate_result
    rounds: list[dict[str, Any]] = []

    for round_number in range(1, max_rounds + 1):
        suffix_plan = plan_suffix_rebuild(
            current_result, snapshot.gate_chapter, round_number=round_number
        )
        rebuild_chapters = tuple(suffix_plan["rebuild_chapters"])
        round_dir = shadow / f"round_{round_number:02d}"
        if round_dir.exists() or round_dir.is_symlink():
            raise GateMigrationError("repair round shadow output already exists")
        verify_frozen_source(snapshot)
        candidate_source_hashes = {
            number: _text_sha256(candidate_texts[number]) for number in expected
        }
        request = RebuildRequest(
            gate_chapter=snapshot.gate_chapter,
            round_number=round_number,
            problem_chapters=tuple(suffix_plan["problem_chapters"]),
            earliest_problem_chapter=suffix_plan["earliest_problem_chapter"],
            rebuild_chapters=rebuild_chapters,
            source_texts=MappingProxyType(dict(candidate_texts)),
            source_chapter_hashes=MappingProxyType(candidate_source_hashes),
            frozen_chapter_hashes=MappingProxyType(dict(snapshot.chapter_hashes)),
            frozen_snapshot_sha256=snapshot.snapshot_sha256,
            shadow_dir=round_dir,
            gate_result=MappingProxyType(dict(current_result)),
        )
        rewritten = rebuild_fn(request)
        verify_frozen_source(snapshot)
        normalized, shadow_hashes = _write_shadow_round(
            round_dir, rebuild_chapters, rewritten
        )
        candidate_texts.update(normalized)
        candidate_hashes = {
            number: _text_sha256(candidate_texts[number]) for number in expected
        }
        review_request = ReviewRequest(
            gate_chapter=snapshot.gate_chapter,
            round_number=round_number,
            expected_chapters=expected,
            chapter_texts=MappingProxyType(dict(candidate_texts)),
            chapter_hashes=MappingProxyType(dict(candidate_hashes)),
            shadow_dir=round_dir,
        )
        review = full_review_fn(review_request)
        verify_frozen_source(snapshot)
        passed = _review_passes_full_range(review, expected, candidate_hashes)
        _require_review_verifier(review, review_request, review_verifier_fn)
        review_sha256 = _canonical_digest(dict(review))
        round_record = {
            **suffix_plan,
            "shadow_dir": str(round_dir),
            "shadow_chapter_hashes": {
                str(number): shadow_hashes[number] for number in rebuild_chapters
            },
            "candidate_chapter_hashes": {
                str(number): candidate_hashes[number] for number in expected
            },
            "full_review": dict(review),
            "full_review_sha256": review_sha256,
            "full_review_passed": passed,
        }
        rounds.append(round_record)
        if passed:
            verify_frozen_source(snapshot)
            final_candidate = _materialize_full_candidate(
                shadow, round_number, expected, candidate_texts
            )
            verify_frozen_source(snapshot)
            plan = {
                "schema_version": SCHEMA_VERSION,
                "status": "READY_TO_FINALIZE",
                "finalize_allowed": True,
                "gate_chapter": snapshot.gate_chapter,
                "rounds_used": round_number,
                "source_root": str(snapshot.root),
                "source_snapshot_sha256": snapshot.snapshot_sha256,
                "source_chapter_hashes": {
                    str(number): snapshot.chapter_hashes[number] for number in expected
                },
                "shadow_root": str(shadow),
                "final_shadow_dir": str(final_candidate),
                "final_chapter_hashes": {
                    str(number): candidate_hashes[number] for number in expected
                },
                "final_review_sha256": review_sha256,
                "rounds": rounds,
            }
            plan["plan_sha256"] = _canonical_digest(plan)
            return plan
        current_result = review

    blocked = {
        "schema_version": SCHEMA_VERSION,
        "status": "BLOCKED",
        "finalize_allowed": False,
        "gate_chapter": snapshot.gate_chapter,
        "rounds_used": len(rounds),
        "source_root": str(snapshot.root),
        "source_snapshot_sha256": snapshot.snapshot_sha256,
        "source_chapter_hashes": {
            str(number): snapshot.chapter_hashes[number] for number in expected
        },
        "shadow_root": str(shadow),
        "final_shadow_dir": "",
        "final_chapter_hashes": {},
        "final_review_sha256": (
            rounds[-1].get("full_review_sha256") if rounds else ""
        ),
        "rounds": rounds,
        "block_reason": "hard gate still failed after the permitted repair rounds",
    }
    blocked["plan_sha256"] = _canonical_digest(blocked)
    return blocked


def validate_finalize_plan(
    plan: Mapping[str, Any],
    review_verifier_fn: Callable[[Mapping[str, Any], ReviewRequest], bool] | None = None,
) -> bool:
    """Revalidate a ready plan immediately before an external promotion step."""
    if not isinstance(plan, Mapping):
        raise GateMigrationError("finalize plan must be a mapping")
    material = dict(plan)
    recorded_plan_hash = str(material.pop("plan_sha256", ""))
    if not SHA256_RE.fullmatch(recorded_plan_hash):
        raise GateMigrationError("finalize plan digest is missing or invalid")
    if _canonical_digest(material) != recorded_plan_hash:
        raise GateMigrationError("finalize plan digest mismatch")
    if plan.get("status") != "READY_TO_FINALIZE" or plan.get("finalize_allowed") is not True:
        raise GateMigrationError("migration is not ready to finalize")
    if not callable(review_verifier_fn):
        raise GateMigrationError("an independent review_verifier_fn is required")
    gate = _strict_gate_chapter(plan.get("gate_chapter"))
    snapshot = snapshot_frozen_source(str(plan.get("source_root") or ""), gate)
    recorded_source = {
        int(number): str(digest)
        for number, digest in dict(plan.get("source_chapter_hashes") or {}).items()
    }
    if snapshot.snapshot_sha256 != plan.get("source_snapshot_sha256"):
        raise GateMigrationError("frozen source snapshot changed before finalize")
    if dict(snapshot.chapter_hashes) != recorded_source:
        raise GateMigrationError("frozen source chapter SHA changed before finalize")
    rounds = plan.get("rounds") or []
    if not rounds or not rounds[-1].get("full_review_passed"):
        raise GateMigrationError("finalize plan lacks a passing full-range review")
    expected = tuple(range(1, gate + 1))
    expected_hashes = {
        int(number): str(digest)
        for number, digest in dict(plan.get("final_chapter_hashes") or {}).items()
    }
    if set(expected_hashes) != set(expected):
        raise GateMigrationError("finalize plan lacks complete candidate hashes")
    final_shadow = Path(str(plan.get("final_shadow_dir") or "")).resolve(strict=True)
    shadow_root = Path(str(plan.get("shadow_root") or "")).resolve(strict=True)
    if final_shadow.is_symlink() or not final_shadow.is_dir() or not _is_relative_to(final_shadow, shadow_root):
        raise GateMigrationError("final shadow output is missing or unsafe")
    candidate_texts: dict[int, str] = {}
    for number in expected:
        path = final_shadow / f"chapter_{number:04d}.txt"
        if not path.is_file() or path.is_symlink():
            raise GateMigrationError(f"final shadow chapter {number} is missing")
        text = path.read_text(encoding="utf-8")
        if _text_sha256(text) != expected_hashes[number]:
            raise GateMigrationError(f"final shadow chapter {number} SHA mismatch")
        candidate_texts[number] = text
    last_round = rounds[-1]
    last_review = last_round.get("full_review")
    if not isinstance(last_review, Mapping):
        raise GateMigrationError("finalize plan lacks the last full-range review")
    review_sha256 = _canonical_digest(dict(last_review))
    if review_sha256 != last_round.get("full_review_sha256"):
        raise GateMigrationError("last full-range review digest mismatch")
    if review_sha256 != plan.get("final_review_sha256"):
        raise GateMigrationError("final review digest mismatch")
    last_shadow = Path(str(last_round.get("shadow_dir") or "")).resolve(strict=True)
    if last_shadow.is_symlink() or not _is_relative_to(last_shadow, shadow_root):
        raise GateMigrationError("last review shadow binding is unsafe")
    review_request = ReviewRequest(
        gate_chapter=gate,
        round_number=int(last_round.get("round") or 0),
        expected_chapters=expected,
        chapter_texts=MappingProxyType(candidate_texts),
        chapter_hashes=MappingProxyType(expected_hashes),
        shadow_dir=last_shadow,
    )
    if not _review_passes_full_range(last_review, expected, expected_hashes):
        raise GateMigrationError("last full-range review no longer passes")
    _require_review_verifier(last_review, review_request, review_verifier_fn)
    return True
