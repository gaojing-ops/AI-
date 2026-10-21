"""One-pass, offline reading-copy export; never adopts or approves chapters."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_CHAPTER_BYTES = 64 * 1024
MAX_BOOK_BYTES = 12 * 1024 * 1024
HAN = re.compile(r"[\u4e00-\u9fff]")


class ExportError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ExportError(f"{label}超出允许目录：{path}") from exc
    return resolved


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ExportError(f"文件超过读取预算：{path.name}")
    return data


def _json(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ExportError(f"索引有重复JSON键：{key}")
            result[key] = value
        return result
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ExportError("索引必须是JSON对象")
    return value


@dataclass(frozen=True)
class ExportPlan:
    output: Path
    manifest_path: Path
    staging: Path
    book: bytes
    manifest: bytes
    summary: dict


def prepare_export(project, index, output, title, *, expected_end=180,
                   char_limits=(3000, 4300)) -> ExportPlan:
    """Read each bound source once. No model, retry, scan-by-mtime, or writes."""
    root = Path(project).resolve()
    staging = _inside(root / "rewrite_staging", root, "暂存目录")
    if type(expected_end) is not int or not 1 <= expected_end <= 1000:
        raise ExportError("章数须为1—1000的整数")
    low, high = char_limits
    if type(low) is not int or type(high) is not int or not 1 <= low <= high:
        raise ExportError("字数上下限无效")
    if not isinstance(title, str) or not title.strip() or len(title) > 120 or "\n" in title or "\r" in title:
        raise ExportError("书名须为单行非空文本，最多120字")
    def local(value):
        path = Path(value)
        return path if path.is_absolute() else root / path
    index_path = _inside(local(index), staging, "索引")
    target = _inside(local(output), staging, "导出")
    if target.suffix.lower() != ".txt":
        raise ExportError("候选阅读稿须为TXT")
    manifest_path = target.with_suffix(".export.json")
    index_bytes = _read(index_path, MAX_INDEX_BYTES)
    spec = _json(index_bytes)
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        raise ExportError("不支持的选稿索引版本")
    if spec.get("formal_completion") is not False:
        raise ExportError("索引须明确不是正式完成证明")
    rows = spec.get("chapters")
    if not isinstance(rows, list) or len(rows) != expected_end:
        raise ExportError("索引章数不完整")
    numbers = [row.get("chapter") if isinstance(row, dict) else None for row in rows]
    if any(type(n) is not int for n in numbers) or sorted(numbers) != list(range(1, expected_end + 1)):
        raise ExportError("章节必须唯一且从1连续到目标章")
    parts, records, seen = [], [], set()
    total = 0
    source_bytes = 0
    for row in sorted(rows, key=lambda item: item["chapter"]):
        source = _inside(local(row["source_path"]), root, "章节来源")
        if source.suffix.lower() != ".txt" or source in seen or source in (target, manifest_path):
            raise ExportError("重复或非法章节来源")
        seen.add(source)
        raw = _read(source, MAX_CHAPTER_BYTES)
        source_bytes += len(raw)
        if source_bytes > MAX_BOOK_BYTES:
            raise ExportError("全部来源超过12MiB预算")
        raw_hash = _digest(raw)
        if raw_hash != row.get("raw_sha256"):
            raise ExportError(f"第{row['chapter']}章来源已变化；请重新选稿，不自动重试")
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        number = row["chapter"]
        if not lines or lines[0] != row.get("title") or not re.match(rf"^第0*{number}章(?:\s|$)", lines[0]):
            raise ExportError(f"第{number}章标题绑定失效")
        count = len(HAN.findall(text))
        net = len(HAN.findall(text.partition("\n")[2]))
        if not low <= count <= high or net < low or count != row.get("chinese_chars"):
            raise ExportError(f"第{number}章字数或净正文不符合索引")
        prepared = text.strip()
        prepared_hash = _digest(prepared.encode("utf-8"))
        if prepared_hash != row.get("committable_chapter_sha256"):
            raise ExportError(f"第{number}章准备正文摘要不一致")
        parts.append(prepared)
        total += count
        records.append({"chapter": number, "source_path": str(source),
                        "raw_sha256": raw_hash, "prepared_sha256": prepared_hash,
                        "chinese_chars": count, "net_chinese_chars": net})
    book = (title.strip() + "\n候选阅读稿（尚未正式采用）\n\n" + "\n\n".join(parts) + "\n").encode("utf-8")
    if len(book) > MAX_BOOK_BYTES:
        raise ExportError("合稿超过12MiB预算")
    summary = {"scope": "candidate_reading_copy_only", "chapter_count": len(records),
               "source_chinese_chars": total, "book_sha256": _digest(book),
               "book_bytes": len(book), "output": str(target),
               "manifest": str(manifest_path), "index_sha256": _digest(index_bytes),
               "formal_adoption": False, "literary_acceptance": False,
               "model_calls": 0, "source_passes": 1, "retries": 0}
    manifest = json.dumps({"schema_version": 1, **summary, "sources": records},
                          ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    # Conflict detection is before creating either artifact or its parent.
    for path, payload, limit in ((target, book, MAX_BOOK_BYTES),
                                 (manifest_path, manifest, MAX_INDEX_BYTES)):
        if path.exists() and _read(path, limit) != payload:
            raise ExportError(f"拒绝覆盖不同内容的已有文件：{path.name}")
    return ExportPlan(target, manifest_path, staging.resolve(), book, manifest, summary)


def write_export(plan: ExportPlan) -> dict:
    """Exclusive creation; identical prior artifacts can be reused, never replaced."""
    written = []
    # Recheck target confinement/conflicts, not all chapter sources a second time.
    for path, data, limit in ((plan.output, plan.book, MAX_BOOK_BYTES),
                              (plan.manifest_path, plan.manifest, MAX_INDEX_BYTES)):
        _inside(path, plan.staging, "导出")
        if path.exists() and _read(path, limit) != data:
            raise ExportError(f"导出目标已变化，拒绝覆盖：{path.name}")
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    for path, data in ((plan.output, plan.book), (plan.manifest_path, plan.manifest)):
        try:
            with path.open("xb") as stream:
                stream.write(data)
            written.append(str(path))
        except FileExistsError:
            if _read(path, max(len(data), 1)) != data:
                raise ExportError(f"导出目标发生并发变化：{path.name}")
    return {"read_only": False, "status": "CANDIDATE_EXPORTED", **plan.summary,
            "written_files": written}
