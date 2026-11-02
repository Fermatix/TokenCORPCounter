"""Report output: XLSX / CSV / JSON / Markdown files and a console summary."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from .core import Row
from .tokens import ENCODING_NAME

FILE_COLUMNS = ["file", "category", "format", "size_bytes", "method", "chars",
                "text_tokens", "images", "image_tokens", "total_tokens", "base64_tokens", "note"]
SUMMARY_COLUMNS = ["files", "size_bytes", "text_tokens", "images", "image_tokens", "total_tokens", "not_counted"]


def source_of(path: str) -> str:
    """Top-level folder ("Drive/") or file ("slack.zip") of a report path; archive members belong to their archive."""
    head, sep, _ = path.split("!/", 1)[0].partition("/")
    return head + sep


_GROUP_KEYS = {"source": lambda row: source_of(row.path), "category": lambda row: row.category}


def _file_record(row: Row) -> dict:
    return {
        "file": row.path, "category": row.category, "format": row.fmt, "size_bytes": row.size,
        "method": row.method, "chars": row.chars, "text_tokens": row.text_tokens, "images": row.images,
        "image_tokens": row.image_tokens, "total_tokens": row.total_tokens,
        "base64_tokens": row.base64_tokens, "note": row.note,
    }


def summarize(rows: list[Row], by: str) -> tuple[list[dict], dict]:
    """Totals per source or per category, heaviest first, plus the grand total."""
    key = _GROUP_KEYS[by]
    groups: dict[str, dict] = defaultdict(lambda: dict.fromkeys(SUMMARY_COLUMNS, 0))
    for row in rows:
        g = groups[key(row)]
        g["files"] += 1
        g["size_bytes"] += row.size
        g["text_tokens"] += row.text_tokens
        g["images"] += row.images
        g["image_tokens"] += row.image_tokens
        g["total_tokens"] += row.total_tokens
        g["not_counted"] += not row.counted
    summary = [{by: k, **v} for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["total_tokens"])]
    total = {by: "TOTAL", **{c: sum(s[c] for s in summary) for c in SUMMARY_COLUMNS}}
    return summary, total


def _columns(with_base64: bool) -> list[str]:
    return [c for c in FILE_COLUMNS if with_base64 or c != "base64_tokens"]


def _files_total(total: dict) -> dict:
    return {"file": "TOTAL", **{c: total[c] for c in ("size_bytes", "text_tokens", "images", "image_tokens", "total_tokens")}}


def write_report(rows: list[Row], out: Path, with_base64: bool) -> None:
    records = [_file_record(r) for r in rows]
    sources = summarize(rows, "source")
    categories = summarize(rows, "category")
    columns = _columns(with_base64)
    suffix = out.suffix.lower()
    out.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".json":
        payload = {"encoding": ENCODING_NAME, "total": {c: sources[1][c] for c in SUMMARY_COLUMNS},
                   "sources": sources[0], "categories": categories[0],
                   "files": [{c: r[c] for c in columns} for r in records]}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    elif suffix == ".md":
        out.write_text(_markdown(records, sources, categories, columns), encoding="utf-8")
    elif suffix == ".xlsx":
        _write_xlsx(records, sources, categories, columns, out)
    else:
        # A CSV holds one table, so it gets the files; utf-8-sig so Excel opens non-ASCII paths correctly
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
            writer.writerow(_files_total(sources[1]))


def _summary_sheet(ws, by: str, summary: list[dict], total: dict) -> None:
    header = [by] + SUMMARY_COLUMNS
    ws.append(header)
    for s in summary + [total]:
        ws.append([s[c] for c in header])
    ws.column_dimensions["A"].width = 40


def _write_xlsx(records, sources, categories, columns, out: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    by_source = wb.active
    by_source.title = "sources"
    _summary_sheet(by_source, "source", *sources)
    files = wb.create_sheet("files")
    files.append(columns)
    for r in records:
        files.append([r[c] for c in columns])
    total = _files_total(sources[1])
    files.append([total.get(c) for c in columns])
    files.auto_filter.ref = f"A1:{files.cell(1, len(columns)).column_letter}{len(records) + 1}"
    files.column_dimensions["A"].width = 60
    files.column_dimensions["E"].width = 40
    by_category = wb.create_sheet("categories")
    _summary_sheet(by_category, "category", *categories)
    for sh in (by_source, files, by_category):
        sh.freeze_panes = "A2"
        for cell in sh[1]:
            cell.font = Font(bold=True)
        for cell in sh[sh.max_row]:
            cell.font = Font(bold=True)
    wb.save(out)


def _md_table(header: list[str], rows: list[list]) -> str:
    def cell(v):
        return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def _md_summary(by: str, summary: list[dict], total: dict) -> str:
    header = [by] + SUMMARY_COLUMNS
    return _md_table(header, [[s[c] for c in header] for s in summary + [total]])


def _markdown(records, sources, categories, columns) -> str:
    total = sources[1]
    return (
        f"# Tokens ({ENCODING_NAME})\n\n"
        f"**Total: {_n(total['total_tokens'])}** (text {_n(total['text_tokens'])}, images {_n(total['image_tokens'])})\n\n"
        "## By source\n\n"
        + _md_summary("source", *sources)
        + "\n\n## By category\n\n"
        + _md_summary("category", *categories)
        + "\n\n## Files\n\n"
        + _md_table(columns, [[r[c] for c in columns] for r in records])
        + "\n"
    )


def _n(value: int) -> str:
    return f"{value:,}"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _print_groups(title: str, by: str, summary: list[dict], total: dict, top: int) -> None:
    shown = summary[:top]
    width = min(40, max([len(s[by]) for s in shown] + [len(by), len("TOTAL")]))
    print(f"\n{title}:")
    print(f"{by:<{width}} {'files':>7} {'text':>14} {'images':>14} {'total':>14} {'not counted':>12}")
    for s in shown + [total]:
        if s is total and len(summary) > top:
            print(f"… {len(summary) - top} more in the report")
        print(f"{_clip(s[by], width):<{width}} {s['files']:>7} {_n(s['text_tokens']):>14} "
              f"{_n(s['image_tokens']):>14} {_n(s['total_tokens']):>14} {s['not_counted']:>12}")


def print_summary(rows: list[Row], top: int = 25) -> None:
    shown = sorted(rows, key=lambda r: -r.total_tokens)[:top]
    width = min(70, max([len(r.path) for r in shown] + [4]))
    title = "Files" if len(rows) <= top else f"Top {top} files by tokens (report rows: {len(rows)})"
    if shown:
        print(f"\n{title}:")
        print(f"{'file':<{width}}  {'category':<12} {'text':>12} {'images':>12} {'total':>12}  note")
    for r in shown:
        print(f"{_clip(r.path, width):<{width}}  {r.category:<12} {_n(r.text_tokens):>12} "
              f"{_n(r.image_tokens):>12} {_n(r.total_tokens):>12}  {r.note}")
    _print_groups("By source", "source", *summarize(rows, "source"), top)
    summary, total = summarize(rows, "category")
    _print_groups("By category", "category", summary, total, len(summary))
    print(f"\nTotal {ENCODING_NAME}: {_n(total['total_tokens'])} tokens "
          f"(text {_n(total['text_tokens'])}, images {_n(total['image_tokens'])})")
