"""Report output: XLSX / CSV / JSON / Markdown files and a console summary."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .core import Row
from .tokens import ENCODING_NAME

TOKEN_COLUMNS = ["clean_tokens", "dirty_tokens", "clean_attachment_text_tokens", "attachment_text_tokens",
                 "image_tokens", "attachment_tokens",
                 "clean_with_attachments", "dirty_with_attachments", "unique_clean_with_attachments", "duplicate_tokens"]
FILE_COLUMNS = ["file", "category", "format", "size_bytes", "method", "is_attachment", "clean_chars", "chars",
                *TOKEN_COLUMNS, "images", "counted", "error", "clean_counted", "duplicate_of", "base64_tokens", "note"]
SUMMARY_COLUMNS = ["files", "attachment_files", "size_bytes", *TOKEN_COLUMNS, "images", "duplicate_files",
                   "errors", "clean_errors", "not_counted", "text_tokens", "total_tokens"]
DEFINITIONS = {
    "clean_tokens": "Main text without export service fields, JSON keys, HTML/Markdown markup or document metadata. "
                    "Heuristic extraction; meaningful numbers and business fields remain. Attachments excluded.",
    "dirty_tokens": "Main text with service fields and markup. JSON/text is counted verbatim; documents as extracted "
                    "text, tables and metadata. Attachments excluded.",
    "attachment_text_tokens": "Full extracted text of attachment files, including their service fields.",
    "clean_attachment_text_tokens": "Attachment text after removing service fields and markup.",
    "image_tokens": "Estimated vision tokens of standalone and embedded images, not OCR text.",
    "attachment_tokens": "attachment_text_tokens + image_tokens. Image cost is added exactly once.",
    "clean_with_attachments": "clean_tokens + clean_attachment_text_tokens + image_tokens.",
    "dirty_with_attachments": "dirty_tokens + attachment_tokens; the complete counted input, including duplicates.",
    "unique_clean_with_attachments": "clean_with_attachments after removing byte-identical files within each source. "
                                     "Compared within the same content type and main/attachment role. Different "
                                     "JSON/Markdown representations of the same records remain distinct.",
    "duplicate_tokens": "dirty_with_attachments belonging to byte-identical copies within the same source. "
                        "Already included in dirty_with_attachments; do not add again.",
    "attachment_files": "Number of files classified as attachments, including unsupported attachments.",
    "duplicate_files": "Number of byte-identical copies within the source, content type and main/attachment role.",
    "is_attachment": "Attachment directories/archives and e-mail attachments (including nested files); "
                     "standalone images, media and opaque binaries are also attachments. Other documents are main content.",
    "errors": "Files whose content could not be read. Recovered XLS files are counted and carry a note.",
    "error": "True for a file read/parse error; False for unsupported or deliberately skipped content.",
    "clean_errors": "Files with a valid full text count whose clean content could not be parsed; clean count is zero.",
    "not_counted": "All files without a content count: read errors, unsupported binary/media, or explicitly skipped files.",
    "text_tokens": "Compatibility total: dirty_tokens + attachment_text_tokens.",
    "total_tokens": "Compatibility alias of dirty_with_attachments.",
}


def source_of(path: str) -> str:
    """Top-level folder ("Drive/") or file ("slack.zip") of a report path; archive members belong to their archive."""
    head, sep, _ = path.split("!/", 1)[0].partition("/")
    return head + sep


_GROUP_KEYS = {"source": lambda row: source_of(row.path), "category": lambda row: row.category}


def mark_duplicates(rows: list[Row]) -> None:
    """Stable per-source deduplication; keep all rows and all verbatim totals."""
    seen: dict[tuple[str, str, str, bool], str] = {}
    for row in rows:
        row.duplicate_of = ""
        if row.checksum and row.size:
            key = (source_of(row.path), row.checksum, row.category, row.is_attachment)
            row.duplicate_of = seen.setdefault(key, row.path)
            if row.duplicate_of == row.path:
                row.duplicate_of = ""


def _token_record(row: Row) -> dict:
    return {
        "clean_tokens": row.clean_tokens, "dirty_tokens": row.dirty_tokens,
        "clean_attachment_text_tokens": row.clean_attachment_text_tokens,
        "attachment_text_tokens": row.attachment_text_tokens, "image_tokens": row.image_tokens,
        "attachment_tokens": row.attachment_tokens, "clean_with_attachments": row.clean_with_attachments,
        "dirty_with_attachments": row.dirty_with_attachments,
        "unique_clean_with_attachments": 0 if row.duplicate_of else row.clean_with_attachments,
        "duplicate_tokens": row.total_tokens if row.duplicate_of else 0,
    }


def _file_record(row: Row) -> dict:
    return {
        "file": row.path, "category": row.category, "format": row.fmt, "size_bytes": row.size,
        "method": row.method, "chars": row.chars, "clean_chars": row.clean_chars, "images": row.images,
        **_token_record(row), "is_attachment": row.is_attachment, "counted": row.counted, "error": row.error,
        "clean_counted": row.clean_counted, "duplicate_of": row.duplicate_of,
        "base64_tokens": row.base64_tokens, "note": row.note,
    }


def summarize(rows: list[Row], by: str) -> tuple[list[dict], dict]:
    """Totals per source or per category, heaviest first, plus the grand total."""
    group_key = _GROUP_KEYS[by]
    groups: dict[str, dict] = defaultdict(lambda: dict.fromkeys(SUMMARY_COLUMNS, 0))
    for row in rows:
        g = groups[group_key(row)]
        g["files"] += 1
        g["attachment_files"] += row.is_attachment
        g["size_bytes"] += row.size
        g["text_tokens"] += row.text_tokens
        for key, value in _token_record(row).items():
            g[key] += value
        g["images"] += row.images
        g["total_tokens"] += row.total_tokens
        g["not_counted"] += not row.counted
        g["errors"] += row.error
        g["clean_errors"] += row.counted and not row.clean_counted
        g["duplicate_files"] += bool(row.duplicate_of)
    summary = [{by: k, **v} for k, v in sorted(groups.items(), key=lambda kv: -kv[1]["total_tokens"])]
    total = {by: "TOTAL", **{c: sum(s[c] for s in summary) for c in SUMMARY_COLUMNS}}
    return summary, total


def _columns(with_base64: bool) -> list[str]:
    return [c for c in FILE_COLUMNS if with_base64 or c != "base64_tokens"]


def _files_total(total: dict) -> dict:
    return {"file": "TOTAL", **{c: total[c] for c in ("size_bytes", "images", *TOKEN_COLUMNS)}}


def write_report(rows: list[Row], out: Path, with_base64: bool) -> None:
    mark_duplicates(rows)
    records = [_file_record(r) for r in rows]
    sources = summarize(rows, "source")
    categories = summarize(rows, "category")
    columns = _columns(with_base64)
    suffix = out.suffix.lower()
    out.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".json":
        payload = {"encoding": ENCODING_NAME, "definitions": DEFINITIONS,
                   "total": {c: sources[1][c] for c in SUMMARY_COLUMNS},
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


@lru_cache(maxsize=2)
def _xlsx_font(bold: bool):
    from openpyxl.styles import Font

    return Font(name="Arial", size=10, bold=bold)


def _append_xlsx(ws, values, header: bool = False, total: bool = False) -> None:
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.comments import Comment

    cells = []
    for value in values:
        cell = WriteOnlyCell(ws, value=value)
        if isinstance(value, str):
            cell.data_type = "s"  # Paths and notes are literal data, even when they start with '='.
        cell.font = _xlsx_font(header or total)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            cell.number_format = "#,##0"
        if header and value in DEFINITIONS:
            cell.comment = Comment(DEFINITIONS[value], "Token counter")
        cells.append(cell)
    ws.append(cells)


def _summary_sheet(ws, by: str, summary: list[dict], total: dict) -> None:
    header = [by] + SUMMARY_COLUMNS
    ws.column_dimensions["A"].width = 40
    _append_xlsx(ws, header, header=True)
    for s in summary:
        _append_xlsx(ws, [s[c] for c in header])
    _append_xlsx(ws, [total[c] for c in header], total=True)


def _write_xlsx(records, sources, categories, columns, out: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    by_source = wb.create_sheet("sources")
    files = wb.create_sheet("files")
    by_category = wb.create_sheet("categories")
    for sheet, width in ((by_source, len(SUMMARY_COLUMNS) + 1), (files, len(columns)),
                         (by_category, len(SUMMARY_COLUMNS) + 1)):
        sheet.freeze_panes = "B2"
        for column in range(2, width + 1):
            sheet.column_dimensions[get_column_letter(column)].width = 22
    files.column_dimensions["A"].width = 70
    files.column_dimensions["E"].width = 50
    for column in ("note", "duplicate_of"):
        files.column_dimensions[get_column_letter(columns.index(column) + 1)].width = 70
    files.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(records) + 1}"
    _summary_sheet(by_source, "source", *sources)
    _append_xlsx(files, columns, header=True)
    for r in records:
        _append_xlsx(files, [r[c] for c in columns])
    total = _files_total(sources[1])
    _append_xlsx(files, [total.get(c) for c in columns], total=True)
    _summary_sheet(by_category, "category", *categories)
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
        f"**Full input with attachments: {_n(total['dirty_with_attachments'])}**; "
        f"clean with attachments: {_n(total['clean_with_attachments'])}; "
        f"unique clean with attachments: {_n(total['unique_clean_with_attachments'])}.\n\n"
        "## By source\n\n"
        + _md_summary("source", *sources)
        + "\n\n## By category\n\n"
        + _md_summary("category", *categories)
        + "\n\n## Files\n\n"
        + _md_table(columns, [[r[c] for c in columns] for r in records])
        + "\n\n## Column definitions\n\n"
        + "\n".join(f"- `{key}`: {value}" for key, value in DEFINITIONS.items()) + "\n"
    )


def _n(value: int) -> str:
    return f"{value:,}"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _print_groups(title: str, by: str, summary: list[dict], total: dict, top: int) -> None:
    shown = summary[:top]
    width = min(40, max([len(s[by]) for s in shown] + [len(by), len("TOTAL")]))
    print(f"\n{title}:")
    print(f"{by:<{width}} {'files':>7} {'clean':>14} {'dirty':>14} {'attachments':>14} "
          f"{'clean+attach':>14} {'dirty+attach':>14} {'unique clean+att':>16} {'errors':>7} {'not counted':>12}")
    for s in shown + [total]:
        if s is total and len(summary) > top:
            print(f"… {len(summary) - top} more in the report")
        print(f"{_clip(s[by], width):<{width}} {s['files']:>7} {_n(s['clean_tokens']):>14} "
              f"{_n(s['dirty_tokens']):>14} {_n(s['attachment_tokens']):>14} "
              f"{_n(s['clean_with_attachments']):>14} {_n(s['dirty_with_attachments']):>14} "
              f"{_n(s['unique_clean_with_attachments']):>16} "
              f"{s['errors']:>7} {s['not_counted']:>12}")


def print_summary(rows: list[Row], top: int = 25) -> None:
    mark_duplicates(rows)
    shown = sorted(rows, key=lambda r: -r.total_tokens)[:top]
    width = min(70, max([len(r.path) for r in shown] + [4]))
    title = "Files" if len(rows) <= top else f"Top {top} files by tokens (report rows: {len(rows)})"
    if shown:
        print(f"\n{title}:")
        print(f"{'file':<{width}}  {'category':<12} {'clean+attach':>14} {'dirty+attach':>14}  note")
    for r in shown:
        print(f"{_clip(r.path, width):<{width}}  {r.category:<12} {_n(r.clean_with_attachments):>14} "
              f"{_n(r.dirty_with_attachments):>14}  {r.note}")
    _print_groups("By source", "source", *summarize(rows, "source"), top)
    summary, total = summarize(rows, "category")
    _print_groups("By category", "category", summary, total, len(summary))
    print(f"\nTotal {ENCODING_NAME}: {_n(total['total_tokens'])} tokens "
          f"(text {_n(total['text_tokens'])}, images {_n(total['image_tokens'])})")
    print(f"Clean text: {_n(total['clean_tokens'])}; clean with attachments: {_n(total['clean_with_attachments'])}; "
          f"unique clean with attachments: {_n(total['unique_clean_with_attachments'])}")
    print(f"Exact duplicate files: {total['duplicate_files']} ({_n(total['duplicate_tokens'])} full tokens); "
          f"read errors: {total['errors']}; clean extraction errors: {total['clean_errors']}")
