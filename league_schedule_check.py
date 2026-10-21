"""Single-pass authored league ledger checks; no models, repairs or formal writes."""

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re

MAX_BYTES = 1024 * 1024


def blackout_range(value):
    start, end = value.split(":", 1)
    result = (date.fromisoformat(start), date.fromisoformat(end))
    if result[0] > result[1]:
        raise ValueError("blackout starts after it ends")
    return result


def check_text(text, *, expected_opponents=17, require_complete=False, blackouts=()):
    if not 1 <= expected_opponents <= 100:
        raise ValueError("expected_opponents must be between 1 and 100")
    issues = []
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not re.match(r"\s*\|\s*R", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        try:
            if len(cells) != 8 or not re.fullmatch(r"R[0-9]{2}", cells[0]):
                raise ValueError("expected eight cells and RNN")
            round_number = int(cells[0][1:])
            played_on = date.fromisoformat(cells[1].replace("/", "-"))
            if (not 1 <= round_number <= expected_opponents * 2
                    or cells[2] not in ("主", "客") or not cells[3]
                    or cells[6] not in ("已发生", "待写")):
                raise ValueError("invalid round, venue, opponent or state")
            score = re.fullmatch(r"([0-9]{1,2}):([0-9]{1,2})", cells[4])
            if score is None or not cells[5].isdigit() or not 0 <= int(cells[5]) <= 120:
                raise ValueError("invalid score or personal minutes")
            gf, ga = map(int, score.groups())
            rows.append({"round": round_number, "date": played_on.isoformat(),
                         "venue": cells[2], "opponent": cells[3],
                         "gf": gf, "ga": ga, "minutes": int(cells[5]),
                         "played": cells[6] == "已发生"})
            if any(start <= played_on <= end for start, end in blackouts):
                issues.append({"code": "BLACKOUT_CONFLICT", "round": round_number})
        except ValueError:
            issues.append({"code": "MALFORMED_ROW", "line": line_number})

    if not rows:
        issues.append({"code": "NO_FIXTURES"})
    round_counts = Counter(row["round"] for row in rows)
    duplicate_rounds = sorted(n for n, count in round_counts.items() if count > 1)
    if duplicate_rounds:
        issues.append({"code": "DUPLICATE_ROUND", "rounds": duplicate_rounds})
    numbers = [row["round"] for row in rows]
    if numbers != sorted(numbers):
        issues.append({"code": "ROUND_ORDER"})
    dates = [row["date"] for row in rows]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        issues.append({"code": "DATE_ORDER_OR_DUPLICATE"})
    counts = Counter((row["venue"], row["opponent"]) for row in rows)
    for (venue, opponent), count in sorted(counts.items()):
        if count > 1:
            issues.append({"code": "DUPLICATE_FIXTURE", "venue": venue,
                           "opponent": opponent, "count": count})
    venues = "".join(row["venue"] for row in rows)
    if "主主主" in venues or "客客客" in venues:
        issues.append({"code": "THREE_CONSECUTIVE_SAME_VENUE"})
    opponents = sorted({row["opponent"] for row in rows})
    expected_rounds = set(range(1, expected_opponents * 2 + 1))
    missing = sorted(expected_rounds - set(numbers))
    complete = (not missing and not duplicate_rounds
                and len(opponents) == expected_opponents
                and all(counts[("主", name)] == counts[("客", name)] == 1
                        for name in opponents))
    if require_complete and not complete:
        issues.append({"code": "INCOMPLETE_DOUBLE_ROUND_ROBIN",
                       "missing_rounds": missing})
    if len(opponents) > expected_opponents:
        issues.append({"code": "TOO_MANY_OPPONENTS"})

    def points(row):
        return 3 if row["gf"] > row["ga"] else int(row["gf"] == row["ga"])

    played = [row for row in rows if row["played"]]
    pending = [row for row in rows if not row["played"]]
    return {"scope": "authored_ledger_only", "read_only": True,
            "structure_valid": not issues, "complete_structure": complete,
            "all_results_played": bool(rows) and not pending,
            "fixture_count": len(rows), "opponent_count": len(opponents),
            "played_count": len(played), "pending_count": len(pending),
            "played_points": sum(map(points, played)),
            "pending_target_points": sum(map(points, pending)),
            "played_personal_minutes": sum(row["minutes"] for row in played),
            "pending_target_minutes": sum(row["minutes"] for row in pending),
            "issues": issues, "rows": rows,
            "notice": "Checks only the supplied authored ledger. Pending targets "
                      "are not played results. No official schedule, standings, "
                      "literary quality, formal adoption or whole-book approval."}


def check_file(path, **kwargs):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("ledger must be a file")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("ledger exceeds 1 MiB")
    report = check_text(raw.decode("utf-8-sig"), **kwargs)
    report["ledger"] = str(path)
    report["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--expected-opponents", type=int, default=17)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--blackout", action="append", default=[],
                        help="Author-declared league blackout: YYYY-MM-DD:YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        report = check_file(args.ledger, expected_opponents=args.expected_opponents,
                            require_complete=args.require_complete,
                            blackouts=[blackout_range(x) for x in args.blackout])
    except (OSError, ValueError) as error:
        print(json.dumps({"scope": "authored_ledger_only", "error": str(error)},
                         ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
