"""Containers scanned recursively: archives and e-mail. Every member becomes its own report row."""

from __future__ import annotations

import bz2
import email
import email.policy
import gzip
import lzma
import mimetypes
import os
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from .core import Options, Row, Source
from .tokens import TextCounter

ScanFn = Callable[[Source, Options], list[Row]]

JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def is_junk(name: str) -> bool:
    base = os.path.basename(name.rstrip("/"))
    return base in JUNK_NAMES or base.startswith("._") or "__MACOSX/" in name


def _skipped(parent: Source, name: str, size: int, reason: str) -> Row:
    fmt = os.path.splitext(name)[1].lstrip(".").lower() or "?"
    return Row(f"{parent.path}!/{name}", "skipped", fmt, "skipped", size, counted=False, notes=[reason])


def _zip_name(info: zipfile.ZipInfo) -> str:
    # Without the UTF-8 flag names are cp437 by spec, but Russian Windows archivers write cp866.
    if info.flag_bits & 0x800:
        return info.filename
    try:
        fixed = info.filename.encode("cp437").decode("cp866")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename
    return fixed if re.search("[А-Яа-яЁё]", fixed) else info.filename


def scan_zip(src: Source, opts: Options, scan: ScanFn) -> list[Row]:
    rows: list[Row] = []
    with src.open() as f, zipfile.ZipFile(f) as zf:
        for info in zf.infolist():
            name = _zip_name(info)
            if info.is_dir() or is_junk(name):
                continue
            if info.flag_bits & 0x1:
                rows.append(_skipped(src, name, info.file_size, "encrypted archive member"))
            elif info.file_size > opts.max_member_bytes:
                rows.append(_skipped(src, name, info.file_size, "larger than --max-member-mb"))
            else:
                rows += scan(Source.from_bytes(src, name, zf.read(info)), opts)
    return rows


def scan_tar(src: Source, opts: Options, scan: ScanFn) -> list[Row]:
    rows: list[Row] = []
    with src.open() as f, tarfile.open(fileobj=f, mode="r:*") as tf:
        for member in tf:
            if not member.isfile() or is_junk(member.name):
                continue
            if member.size > opts.max_member_bytes:
                rows.append(_skipped(src, member.name, member.size, "larger than --max-member-mb"))
                continue
            with tf.extractfile(member) as m:
                rows += scan(Source.from_bytes(src, member.name, m.read()), opts)
    return rows


def scan_compressed(src: Source, opts: Options, scan: ScanFn, kind: str) -> list[Row]:
    """.gz / .bz2 / .xz: a compressed tarball, or a single compressed file (data.jsonl.gz)."""
    try:
        return scan_tar(src, opts, scan)
    except tarfile.ReadError:
        pass
    opener = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
    name = os.path.splitext(src.name)[0] or "data"
    with src.open() as raw, opener(raw) as f:
        data = f.read(opts.max_member_bytes + 1)
    if len(data) > opts.max_member_bytes:
        return [_skipped(src, name, len(data), "larger than --max-member-mb")]
    return scan(Source.from_bytes(src, name, data), opts)


def scan_7z(src: Source, opts: Options, scan: ScanFn) -> list[Row]:
    import py7zr

    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="token_counter_") as tmp:
        with src.open() as f, py7zr.SevenZipFile(f, mode="r") as archive:
            archive.extractall(path=tmp)
        root = Path(tmp)
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = file.relative_to(root).as_posix()
            if is_junk(rel):
                continue
            child = Source(f"{src.path}!/{rel}", file.name, file.stat().st_size, file=file, depth=src.depth + 1)
            rows += scan(child, opts)
    return rows


def _attachment_name(part, index: int) -> str:
    name = part.get_filename()
    if name:
        return name
    ext = mimetypes.guess_extension(part.get_content_type()) or ".bin"
    return f"part{index}{ext}"


def scan_eml(src: Source, opts: Options, scan: ScanFn, row: Row) -> list[Row]:
    """Headers and text bodies go into the e-mail row; attachments and inline images are scanned separately."""
    msg = email.message_from_bytes(src.read(), policy=email.policy.default)
    counter = TextCounter()
    counter.feed("".join(f"{k}: {v}\n" for k, v in msg.items()))
    children: list[Row] = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        is_text = part.get_content_maintype() == "text"
        if part.get_filename() or part.get_content_disposition() == "attachment" or not is_text:
            payload = part.get_payload(decode=True) or b""
            children += scan(Source.from_bytes(src, _attachment_name(part, index), payload), opts)
            continue
        try:
            text = part.get_content()
        except (LookupError, ValueError):
            text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        counter.feed(text + "\n")
    counter.close()
    row.method = "e-mail headers and text (attachments as separate rows)"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    return [row] + children


_MBOX_FROM = re.compile(rb"^From .*\r?\n", re.M)


def scan_mbox(src: Source, opts: Options, scan: ScanFn) -> list[Row]:
    data = src.read()
    starts = [m.start() for m in _MBOX_FROM.finditer(data)] or [0]
    if starts[0] != 0:
        starts.insert(0, 0)
    rows: list[Row] = []
    for i, (a, b) in enumerate(zip(starts, starts[1:] + [len(data)]), 1):
        chunk = _MBOX_FROM.sub(b"", data[a:b], count=1)
        rows += scan(Source.from_bytes(src, f"{i:05d}.eml", chunk), opts)
    return rows


def scan_msg(src: Source, opts: Options, scan: ScanFn, row: Row) -> list[Row]:
    import extract_msg

    msg = extract_msg.openMsg(src.read())
    try:
        counter = TextCounter()
        header = msg.header
        if header is not None and len(header):
            counter.feed(str(header))
        else:
            fields = {"From": msg.sender, "To": msg.to, "Cc": msg.cc, "Date": msg.date, "Subject": msg.subject}
            counter.feed("".join(f"{k}: {v}\n" for k, v in fields.items() if v))
        counter.feed((msg.body or "") + "\n")
        children: list[Row] = []
        for i, att in enumerate(msg.attachments, 1):
            data = att.data
            name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or f"attachment{i}"
            if isinstance(data, bytes):
                children += scan(Source.from_bytes(src, name, data), opts)
            else:
                row.notes.append(f"nested e-mail not parsed: {name}")
    finally:
        msg.close()
    counter.close()
    row.method = "Outlook e-mail headers and text (attachments as separate rows)"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    return [row] + children
