"""Bounded, read-only date snapshots of authored cup score plans, not rankings."""

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re

MAX_BYTES = 1024 * 1024
CALENDAR_HEADER = ["组", "1", "2", "3", "4", "三个比赛日"]
SCORE_HEADER = ["组", "1-2", "3-4", "1-3", "4-2", "4-1", "2-3"]
PAIRS = ((0, 1), (2, 3), (0, 2), (3, 1), (3, 0), (1, 2))


def round_dates(value, year):
    tokens = re.split(r"[、，,;]", value)
    if len(tokens) != 3:
        raise ValueError("three round dates required")
    month = None
    result = []
    for token in tokens:
        parts = token.strip().replace("-", "/").split("/")
        if len(parts) == 3:
            listed_year, month, day = map(int, parts)
            if listed_year != year:
                raise ValueError("calendar year differs from requested year")
        elif len(parts) == 2:
            month, day = map(int, parts)
        elif len(parts) == 1 and month is not None:
            day = int(parts[0])
        else:
            raise ValueError("first round needs an explicit month")
        result.append(date(year, month, day))
    if not result[0] < result[1] < result[2]:
        raise ValueError("round dates must strictly increase")
    return result


def check_text(text, *, year, as_of, expected_groups=12):
    if not 1 <= expected_groups <= 26:
        raise ValueError("expected_groups must be between 1 and 26")
    cutoff = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if not isinstance(cutoff, date) or cutoff.year != year:
        raise ValueError("as_of must be a date in the declared year")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("source exceeds 1 MiB; no truncation allowed")
    calendars, scores, issues = {}, {}, []
    headers = Counter()
    mode = None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith("|"):
            mode = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells == CALENDAR_HEADER or cells == SCORE_HEADER:
            mode = "calendar" if cells == CALENDAR_HEADER else "score"
            headers[mode] += 1
            continue
        if not mode or all(re.fullmatch(r"[-: ]+", c) for c in cells):
            continue
        group = cells[0]
        try:
            if not re.fullmatch("[A-Z]", group):
                raise ValueError("invalid group label")
            if mode == "calendar":
                if len(cells) != 6 or any(not c for c in cells[1:5]):
                    raise ValueError("four named teams and dates required")
                if group in calendars:
                    raise ValueError("duplicate calendar group")
                calendars[group] = (cells[1:5], round_dates(cells[5], year))
            else:
                if len(cells) != 7 or group in scores:
                    raise ValueError("six scores and unique group required")
                row = []
                for value in cells[1:]:
                    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{1,2})", value)
                    if match is None:
                        raise ValueError("invalid score")
                    row.append(tuple(map(int, match.groups())))
                scores[group] = row
        except ValueError as exc:
            issues.append({"code": "MALFORMED_SOURCE_ROW", "line": line_number,
                           "reason": str(exc)})
    if headers != Counter(calendar=1, score=1):
        issues.append({"code": "MISSING_OR_DUPLICATE_SOURCE_TABLE"})
    required = {chr(65 + n) for n in range(expected_groups)}
    if set(calendars) != required or set(scores) != required:
        issues.append({"code": "INCOMPLETE_GROUP_BLOCKS"})
    names = [team for teams, _ in calendars.values() for team in teams]
    if len(set(names)) != len(names):
        issues.append({"code": "DUPLICATE_TEAM"})
    report = {"scope": "authored_score_plan_only", "read_only": True,
              "structure_valid": not issues, "year": year,
              "as_of_end_of_day": cutoff.isoformat(), "rankings_calculated": False,
              "confirmed_results": False, "formal_adoption": False,
              "issues": issues, "groups": []}
    if issues:
        return report
    included, future, finished = 0, 0, []
    for group in sorted(required):
        teams, dates = calendars[group]
        rows = [{"team": name, "played": 0, "points": 0,
                 "goals_for": 0, "goals_against": 0} for name in teams]
        group_count = 0
        for k, ((i, j), (gf, ga)) in enumerate(zip(PAIRS, scores[group])):
            if dates[k // 2] > cutoff:
                future += 1
                continue
            included += 1
            group_count += 1
            for index, own, other in ((i, gf, ga), (j, ga, gf)):
                rows[index]["played"] += 1
                rows[index]["points"] += 3 if own > other else int(own == other)
                rows[index]["goals_for"] += own
                rows[index]["goals_against"] += other
        for row in rows:
            row["goal_difference"] = row["goals_for"] - row["goals_against"]
        if group_count == 6:
            finished.append(group)
        report["groups"].append({"group": group, "included_matches": group_count,
                                  "group_complete_by_plan_date": group_count == 6,
                                  "teams_in_source_order_not_ranking": rows})
    report.update(total_planned_matches=expected_groups * 6, included_matches=included,
                  excluded_future_matches=future, completed_groups_by_plan_date=finished)
    return report


def check_file(path, **kwargs):
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be a regular, non-symlink file")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("source exceeds 1 MiB; no truncation allowed")
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("source grew beyond 1 MiB; no truncation allowed")
    report = check_text(raw.decode("utf-8-sig"), **kwargs)
    report.update(source=str(source.resolve()),
                  source_sha256=hashlib.sha256(raw).hexdigest())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--as-of", required=True, help="inclusive end-of-day YYYY-MM-DD")
    parser.add_argument("--expected-groups", type=int, default=12)
    args = parser.parse_args(argv)
    try:
        report = check_file(args.source, year=args.year, as_of=args.as_of,
                            expected_groups=args.expected_groups)
    except (ValueError, OSError) as exc:
        report = {"scope": "authored_score_plan_only", "read_only": True,
                  "structure_valid": False, "confirmed_results": False,
                  "issues": [{"code": "INPUT_ERROR", "reason": str(exc)}]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["structure_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
