"""Dispatch a file to its handler; anything that cannot be parsed stays in the report with zero tokens."""

from __future__ import annotations

from . import containers as c
from .core import Options, Row, Source
from .detect import category, detect
from .docs import count_ole_strings, count_pdf, count_xls, count_xlsx, count_zip_document
from .media import count_binary, count_image, count_text_file
from .tokens import count_base64

_PIP_NAMES = {"PIL": "Pillow", "pypdf": "pypdf", "openpyxl": "openpyxl", "xlrd": "xlrd",
              "olefile": "olefile", "extract_msg": "extract-msg", "py7zr": "py7zr"}


def _empty(src: Source, row: Row, opts: Options) -> None:
    row.method = "empty file"


def _media(src: Source, row: Row, opts: Options) -> None:
    count_binary(src, row, opts)
    row.notes.append("audio/video: no text, needs transcription")


def _rar(src: Source, row: Row, opts: Options) -> None:
    count_binary(src, row, opts)
    row.notes.append("RAR cannot be unpacked without unrar, contents not counted")


HANDLERS = {
    "empty": _empty,
    "text": count_text_file,
    "image": count_image,
    "media": _media,
    "binary": count_binary,
    "rar": _rar,
    "pdf": count_pdf,
    "xlsx": count_xlsx,
    "xls": count_xls,
    "docx": lambda s, r, o: count_zip_document(s, r, o, "docx"),
    "pptx": lambda s, r, o: count_zip_document(s, r, o, "pptx"),
    "odf": lambda s, r, o: count_zip_document(s, r, o, "odf"),
    "epub": lambda s, r, o: count_zip_document(s, r, o, "epub"),
    "doc": lambda s, r, o: count_ole_strings(s, r, o, "doc"),
    "ppt": lambda s, r, o: count_ole_strings(s, r, o, "ppt"),
}

# Containers yield one row per member; e-mail also keeps a row for its own headers and text.
CONTAINERS = {
    "zip": lambda s, o, r: c.scan_zip(s, o, scan),
    "tar": lambda s, o, r: c.scan_tar(s, o, scan),
    "gzip": lambda s, o, r: c.scan_compressed(s, o, scan, "gzip"),
    "bzip2": lambda s, o, r: c.scan_compressed(s, o, scan, "bzip2"),
    "xz": lambda s, o, r: c.scan_compressed(s, o, scan, "xz"),
    "7z": lambda s, o, r: c.scan_7z(s, o, scan),
    "mbox": lambda s, o, r: c.scan_mbox(s, o, scan),
    "eml": lambda s, o, r: c.scan_eml(s, o, scan, r),
    "msg": lambda s, o, r: c.scan_msg(s, o, scan, r),
}


def _describe(error: Exception) -> str:
    if isinstance(error, ImportError) and error.name:
        pkg = _PIP_NAMES.get(error.name.split(".")[0], error.name)
        return f"missing package {pkg} (pip install {pkg})"
    text = str(error).strip() or type(error).__name__
    return text[:200]


def _fallback(src: Source, row: Row, opts: Options, error: Exception) -> Row:
    row.chars, row.text_tokens, row.images, row.image_tokens = 0, 0, 0, 0
    row.notes.append(f"not parsed: {_describe(error)}")
    count_binary(src, row, opts)
    return row


def scan(src: Source, opts: Options) -> list[Row]:
    try:
        kind = detect(src)
    except OSError as e:  # unreadable file: report it, don't stop the scan
        return [Row(src.path, "error", src.ext.lstrip(".") or "?", "error", src.size, counted=False,
                    notes=[f"unreadable: {_describe(e)}"])]
    except Exception:
        kind = "binary"
    row = Row(src.path, category(kind, src), src.ext.lstrip(".") or kind, "", src.size)
    try:
        if kind in CONTAINERS:
            if src.depth >= opts.max_depth:
                raise ValueError("containers nested too deep")
            rows = CONTAINERS[kind](src, opts, row)
            if not rows:
                row.category, row.method = "archive", "empty archive"
                rows = [row]
        else:
            HANDLERS[kind](src, row, opts)
            rows = [row]
    except Exception as e:
        rows = [_fallback(src, row, opts, e)]
    if opts.base64 and rows[0] is row:
        try:
            with src.open() as f:
                row.base64_tokens = count_base64(f)[0]
        except OSError as e:
            row.notes.append(f"base64 not counted: {_describe(e)}")
    return rows
