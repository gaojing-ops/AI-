"""One-pass, read-only candidate text inventory. No model calls or formal gates."""

import argparse
import hashlib
import json
from pathlib import Path
import re


FILE_NAME = re.compile(r"第([0-9]{4})章\.txt\Z")
TITLE = re.compile(r"第([0-9]{1,4})章[ \t\u3000]*(\S.*?)\Z")
HAN = re.compile(r"[\u4e00-\u9fff]")
MAX_CHAPTER_BYTES = 1024 * 1024


def _ranges(numbers):
    result = []
    for number in sorted(numbers):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return result


def _stage_path(project, stage_name):
    if (not stage_name or stage_name in (".", "..")
            or re.search(r"[\\/:\x00]", stage_name)
            or stage_name.endswith((" ", "."))):
        raise ValueError("stage_name must be one directory name, not a path")
    project = Path(project).resolve(strict=True)
    staging = (project / "rewrite_staging").resolve(strict=True)
    if not staging.is_relative_to(project):
        raise ValueError("rewrite_staging resolves outside the project")
    stage = (staging / stage_name).resolve(strict=True)
    if not stage.is_relative_to(staging) or not stage.is_dir():
        raise ValueError("stage must be a directory inside rewrite_staging")
    return stage


def _inspect(path, stage, number, min_body_han):
    row = {"chapter": number, "file": path.name, "issues": []}
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(stage) or path.is_symlink():
            row["issues"].append("UNSAFE_LINK")
            return row
        if not path.is_file():
            row["issues"].append("NOT_REGULAR_FILE")
            return row
        if path.stat().st_size > MAX_CHAPTER_BYTES:
            row["issues"].append("FILE_TOO_LARGE")
            return row
        with path.open("rb") as handle:
            raw = handle.read(MAX_CHAPTER_BYTES + 1)
        if len(raw) > MAX_CHAPTER_BYTES:
            row["issues"].append("FILE_TOO_LARGE")
            return row
        row["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        row["issues"].append("INVALID_UTF8")
        return row
    except (OSError, ValueError, RuntimeError):
        row["issues"].append("READ_ERROR")
        return row

    lines = text.splitlines()
    title = TITLE.fullmatch(lines[0].strip()) if lines else None
    if title is None:
        row["issues"].append("MISSING_TITLE")
        body = text
    else:
        body = "\n".join(lines[1:])
        if int(title.group(1)) != number:
            row["issues"].append("TITLE_NUMBER_MISMATCH")
    row["han_including_title"] = len(HAN.findall(text))
    row["body_han"] = len(HAN.findall(body))
    if row["body_han"] < min_body_han:
        row["issues"].append("SHORT_BODY")
    return row


def inventory(project, stage_name, *, start=1, end=180, min_body_han=3000):
    """Inventory one stage; text checks do not establish literary/formal approval."""
    if not (1 <= start <= end <= 9999) or min_body_han < 1:
        raise ValueError("require 1 <= start <= end <= 9999 and min_body_han >= 1")
    stage = _stage_path(project, stage_name)
    rows = []
    invalid_names = []
    for path in sorted(stage.iterdir(), key=lambda item: item.name):
        match = FILE_NAME.fullmatch(path.name)
        if match is None:
            if path.name.startswith("第") and path.name.lower().endswith("章.txt"):
                invalid_names.append(path.name)
            continue
        number = int(match.group(1))
        if start <= number <= end:
            rows.append(_inspect(path, stage, number, min_body_han))

    present = {row["chapter"] for row in rows}
    clean = {row["chapter"] for row in rows if not row["issues"]}
    missing = set(range(start, end + 1)) - present
    attention = sorted(present - clean)
    return {
        "scope": "candidate_only",
        "stage": str(stage),
        "expected_range": [start, end],
        "min_body_han": min_body_han,
        "files_found": len(rows),
        "local_text_checks_met": len(clean),
        "present_ranges": _ranges(present),
        "missing_ranges": _ranges(missing),
        "attention_chapters": attention,
        "invalid_names": invalid_names,
        "range_text_complete": not missing and not attention and not invalid_names,
        "literary_quality_assessed": False,
        "formal_adoption_verified": False,
        "notice": "Only filenames, UTF-8, titles, net body Han count and raw hashes. "
                  "No model calls, retries, semantic/state/receipt/commercial/debt "
                  "verification or whole-book completion claim.",
        "chapters": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--stage-name", required=True)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=180)
    parser.add_argument("--min-body-han", type=int, default=3000)
    args = parser.parse_args(argv)
    try:
        report = inventory(args.project, args.stage_name, start=args.start,
                           end=args.end, min_body_han=args.min_body_han)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"scope": "candidate_only", "error": str(error)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["range_text_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
