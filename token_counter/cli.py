"""Command line: walk a folder, count tokens for every file in parallel, print and save the report."""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import tokens
from .containers import is_junk
from .core import Options, Row, Source
from .report import print_summary, write_report
from .scanner import scan


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="token_counter",
        description="Counts o200k_base tokens (GPT-4o / GPT-4.1 / o-series) for every file in a folder: "
        "text, JSON/JSONL, spreadsheets, documents, PDF, images, archives and e-mail. "
        "Audio, video and other binaries are listed in the report with zero tokens.",
    )
    p.add_argument("--path", required=True, type=Path, help="folder (walked recursively) or a single file")
    p.add_argument("--out", type=Path, help="report file: .xlsx, .csv, .json or .md; a folder means token_report.xlsx inside it")
    p.add_argument("--image-detail", choices=["high", "low"], default="high",
                   help="image token mode (default: high)")
    p.add_argument("--base64", action="store_true",
                   help="add a base64_tokens column: each whole file as base64 (as in a raw API request); "
                   "not included in total_tokens")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip files and folders matching a glob, repeatable: --exclude '*.log'")
    p.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4), help="number of threads")
    p.add_argument("--max-member-mb", type=int, default=2048,
                   help="do not extract archive members larger than N MB (zip bomb guard)")
    p.add_argument("--top", type=int, default=25, help="how many of the heaviest files and sources to show in the console")
    return p.parse_args(argv)


def _excluded(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def iter_files(root: Path, patterns: list[str], skip: set[Path]):
    if root.is_file():
        yield root, root.name
        return
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d != "__MACOSX" and not _excluded((base / d).relative_to(root).as_posix(), patterns)
        )
        for name in sorted(filenames):
            file = base / name
            rel = file.relative_to(root).as_posix()
            if is_junk(name) or _excluded(rel, patterns) or file.resolve() in skip or not file.is_file():
                continue
            yield file, rel


def _scan_file(file: Path, rel: str, opts: Options) -> list[Row]:
    try:
        src = Source.from_file(file, rel)
    except OSError as e:
        return [Row(rel, "error", file.suffix.lstrip(".").lower(), "error", 0, counted=False,
                    notes=[f"unreadable: {e}"])]
    return scan(src, opts)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root: Path = args.path.expanduser()
    if not root.exists():
        print(f"Path not found: {root}", file=sys.stderr)
        return 2
    out: Path | None = args.out.expanduser() if args.out else None
    if out is not None and (out.is_dir() or not out.suffix):
        out = out / "token_report.xlsx"

    # Parser warnings about damaged files would garble the output; failures end up in the "note" column.
    for noisy in ("pypdf", "extract_msg", "olefile", "py7zr"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    try:
        tokens.encoding()
    except Exception as e:
        print(f"Failed to load the {tokens.ENCODING_NAME} vocabulary: {e}\n"
              "On first run tiktoken downloads it from the internet and caches it "
              "(TIKTOKEN_CACHE_DIR sets the cache folder).", file=sys.stderr)
        return 1

    opts = Options(image_detail=args.image_detail, base64=args.base64,
                   max_member_bytes=args.max_member_mb << 20)
    skip = {out.resolve()} if out else set()
    files = list(iter_files(root, args.exclude, skip))
    if not files:
        print("No files found.", file=sys.stderr)
        return 1

    started = time.monotonic()
    results: list[list[Row]] = [[] for _ in files]
    show_progress = sys.stderr.isatty()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_scan_file, f, rel, opts): i for i, (f, rel) in enumerate(files)}
        for done, fut in enumerate(as_completed(futures), 1):
            i = futures[fut]
            results[i] = fut.result()
            if show_progress:
                print(f"\r\033[K[{done}/{len(files)}] {files[i][1][-80:]}", end="", file=sys.stderr, flush=True)
    if show_progress:
        print("\r\033[K", end="", file=sys.stderr)

    rows = [row for file_rows in results for row in file_rows]
    print_summary(rows, top=args.top)
    print(f"Files: {len(files)}, report rows: {len(rows)}, time: {time.monotonic() - started:.1f} s")
    if out is not None:
        write_report(rows, out, with_base64=args.base64)
        print(f"Report: {out}")
    return 0
