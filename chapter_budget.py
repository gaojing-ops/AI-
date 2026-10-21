"""Durable admission budget. No model calls, prose, or credentials in this file.

The identity is project + chapter, deliberately NOT batch/model/prompt hash.
Restarting, changing providers or editing a candidate cannot renew the budget.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
import re
from pathlib import Path
import tempfile
import time
import uuid


class BudgetStopped(RuntimeError):
    pass


def bounded_attempts(value=2):
    try:
        return max(1, min(2, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 2


class ChapterBudget:
    def __init__(self, project, chapter, calls=16, seconds=1200):
        self.chapter = int(chapter)
        if self.chapter < 1:
            raise ValueError("chapter must be positive")
        self.calls = max(1, int(calls))
        self.seconds = max(1, int(seconds))
        self.path = Path(project).resolve() / "runtime" / "chapter_budgets" / f"chapter_{self.chapter:04d}.json"

    def _new(self):
        return dict(schema_version=1, chapter=self.chapter, epoch=uuid.uuid4().hex,
                    calls=0, repairs=0, started_at=None, last_seen=0,
                    call_limit=self.calls, seconds_limit=self.seconds, stop_reason="")

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise BudgetStopped("CHAPTER_BUDGET_STOPPED：预算被占用或不可写；暂停，不自动抢锁") from exc
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink()

    def _read(self):
        if not self.path.exists():
            return self._new()
        try:
            row = json.loads(self.path.read_text(encoding="utf-8"))
            if row["schema_version"] != 1 or row["chapter"] != self.chapter:
                raise ValueError("identity")
            if not isinstance(row["epoch"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["epoch"]):
                raise ValueError("epoch")
            for key in ("calls", "repairs", "call_limit", "seconds_limit"):
                if type(row[key]) is not int or row[key] < 0:
                    raise ValueError(key)
            if row["call_limit"] <= 0 or row["seconds_limit"] <= 0 or row["repairs"] > 1:
                raise ValueError("limits")
            if row["started_at"] is not None and not math.isfinite(float(row["started_at"])):
                raise ValueError("started_at")
            if not math.isfinite(float(row["last_seen"])) or not isinstance(row["stop_reason"], str):
                raise ValueError("metadata")
            if row["calls"] and row["started_at"] is None:
                raise ValueError("missing start")
            return row
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise BudgetStopped("CHAPTER_BUDGET_STOPPED：预算记录损坏；禁止归零重试") from exc

    def _write(self, row, path=None):
        target = path or self.path
        fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def transact(self, action="status", *, now=None):
        now = time.time() if now is None else float(now)
        try:
            with self._lock():
                row = self._read()
                # Raising a config slider is not an implicit renewal.
                row["call_limit"] = min(row["call_limit"], self.calls)
                row["seconds_limit"] = min(row["seconds_limit"], self.seconds)
                if action == "status":
                    return row
                reason = row["stop_reason"]
                if now < row["last_seen"]:
                    reason = reason or "系统时钟回退，需人工核对"
                row["last_seen"] = max(now, row["last_seen"])
                started = row["started_at"]
                if started is not None and now - started >= row["seconds_limit"]:
                    reason = reason or "同章总时限耗尽（包含等待及恢复间隔）"
                if action == "call" and row["calls"] >= row["call_limit"]:
                    reason = reason or "同章总调用额度耗尽"
                if action == "repair" and row["repairs"] >= 1:
                    reason = reason or "本章已自动修复一轮，需人工查看候选与问题清单"
                if reason:
                    row["stop_reason"] = reason
                    self._write(row)
                    raise BudgetStopped("CHAPTER_BUDGET_STOPPED：" + reason)
                if action == "call":
                    row["started_at"] = now if started is None else started
                    row["calls"] += 1  # Reserve BEFORE sending; failures are not refunded.
                elif action == "repair":
                    row["repairs"] += 1
                elif action != "check":
                    raise ValueError("unknown budget action")
                self._write(row)
                return row
        except BudgetStopped:
            raise
        except OSError as exc:
            raise BudgetStopped("CHAPTER_BUDGET_STOPPED：预算无法持久保存；禁止调用") from exc

    def remaining(self, maximum):
        now = time.time()
        row = self.transact("check", now=now)
        self._stream_last_seen = now
        self._stream_deadline = (row["started_at"] + row["seconds_limit"]
                                 if row["started_at"] is not None else None)
        if row["started_at"] is None:
            return float(maximum)
        return max(0.001, min(float(maximum), row["seconds_limit"] - (now - row["started_at"])))

    def check_stream(self):
        """Cheap per-chunk deadline check; never fsync for each generated token."""
        now = time.time()
        deadline = getattr(self, "_stream_deadline", None)
        last_seen = getattr(self, "_stream_last_seen", now)
        if now < last_seen:
            raise BudgetStopped("CHAPTER_BUDGET_STOPPED：流式调用期间系统时钟回退")
        self._stream_last_seen = now
        if deadline is not None and now >= deadline:
            self.transact("check", now=now)

    def renew(self):
        """Explicit human action only. Preserve the previous epoch as evidence."""
        with self._lock():
            old = self._read()
            if self.path.exists():
                self._write(old, self.path.with_name(self.path.stem + "." + old["epoch"] + ".json"))
            self._write(self._new())


def main():
    parser = argparse.ArgumentParser(description="查看预算；--renew 显式追加新预算并保留旧记录，不启动模型")
    parser.add_argument("--project", required=True)
    parser.add_argument("--chapter", required=True, type=int)
    parser.add_argument("--renew", action="store_true")
    args = parser.parse_args()
    budget = ChapterBudget(args.project, args.chapter)
    if args.renew:
        budget.renew()
    print(json.dumps(budget.transact(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
