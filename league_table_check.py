"""Complete authored home-score matrices; bounded read only, not league rankings."""

import argparse
import hashlib
import json
from pathlib import Path
import re

MAX_BYTES = 1024 * 1024
MAX_TEAMS = 26


def cells(line):
    framed = line.strip()
    if not framed.startswith("|"):
        return []
    body = framed[1:-1] if framed.endswith("|") else framed[1:]
    return [value.strip() for value in body.split("|")]


def check_text(text, *, expected_teams=18):
    if not 2 <= expected_teams <= MAX_TEAMS:
        raise ValueError("expected_teams must be between 2 and 26")
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines)
               if line.strip().startswith("|") and cells(line)[0] == "主队"]
    report = {
        "scope": "authored_complete_score_plan_only", "read_only": True,
        "rankings_calculated": False, "confirmed_results": False,
        "formal_adoption": False, "structure_valid": False,
        "issues": [], "teams_in_source_order_not_ranking": [],
    }
    if len(headers) != 1:
        report["issues"].append({"code": "MISSING_OR_AMBIGUOUS_MATRIX"})
        return report
    start = headers[0]
    names = cells(lines[start])[1:]
    if (len(names) != expected_teams or len(set(names)) != expected_teams
            or any(not name or len(name) > 80 for name in names)):
        report["issues"].append({"code": "INVALID_TEAM_COLUMNS"})
        return report
    if start + 1 >= len(lines):
        report["issues"].append({"code": "MISSING_SEPARATOR"})
        return report
    separator = cells(lines[start + 1])
    if (len(separator) != expected_teams + 1
            or any(not re.fullmatch(r":?-{3,}:?", value) for value in separator)):
        report["issues"].append({"code": "INVALID_SEPARATOR"})
        return report
    matrix = {}
    index = start + 2
    while index < len(lines) and lines[index].strip().startswith("|"):
        row = cells(lines[index])
        index += 1
        if len(row) != expected_teams + 1:
            report["issues"].append({"code": "INVALID_ROW_WIDTH", "line": index})
            continue
        home = row[0]
        if home not in names or home in matrix:
            report["issues"].append({"code": "UNKNOWN_OR_DUPLICATE_HOME", "line": index})
            continue
        scores = []
        for away, value in zip(names, row[1:]):
            if home == away:
                if value not in ("—", "-"):
                    report["issues"].append({"code": "INVALID_DIAGONAL", "line": index})
                scores.append(None)
                continue
            match = re.fullmatch(r"([0-9]{1,2}):([0-9]{1,2})", value)
            if match is None:
                report["issues"].append({"code": "INVALID_SCORE", "line": index})
                scores.append(None)
            else:
                scores.append(tuple(map(int, match.groups())))
        matrix[home] = scores
    if set(matrix) != set(names):
        report["issues"].append({"code": "INCOMPLETE_HOME_ROWS"})
    if report["issues"]:
        return report
    totals = {name: {"team": name, "played": 0, "won": 0, "drawn": 0,
                     "lost": 0, "points": 0, "goals_for": 0, "goals_against": 0}
              for name in names}
    fixtures, draws = 0, 0
    for home in names:
        for away, score in zip(names, matrix[home]):
            if home == away:
                continue
            gf, ga = score
            fixtures += 1
            draws += gf == ga
            for name, own, other in ((home, gf, ga), (away, ga, gf)):
                row = totals[name]
                row["played"] += 1
                row["goals_for"] += own
                row["goals_against"] += other
                row["won"] += own > other
                row["drawn"] += own == other
                row["lost"] += own < other
                row["points"] += 3 if own > other else int(own == other)
    for row in totals.values():
        row["goal_difference"] = row["goals_for"] - row["goals_against"]
    report.update(
        structure_valid=True, team_count=expected_teams,
        planned_matches=fixtures, planned_rounds=2 * (expected_teams - 1),
        drawn_matches=draws, total_points=sum(v["points"] for v in totals.values()),
        goals_for_total=sum(v["goals_for"] for v in totals.values()),
        goals_against_total=sum(v["goals_against"] for v in totals.values()),
        teams_in_source_order_not_ranking=[totals[name] for name in names],
    )
    return report


def check_file(path, **kwargs):
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be a regular, non-symlink file")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("source exceeds 1 MiB")
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("source grew beyond 1 MiB")
    report = check_text(raw.decode("utf-8-sig"), **kwargs)
    report.update(source=str(source.resolve()),
                  source_sha256=hashlib.sha256(raw).hexdigest())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--teams", type=int, default=18)
    args = parser.parse_args(argv)
    try:
        report = check_file(args.source, expected_teams=args.teams)
    except (OSError, ValueError) as error:
        print(json.dumps({"read_only": True, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
