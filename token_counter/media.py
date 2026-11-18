"""Handlers for plain text, images and opaque binaries."""

from __future__ import annotations

import codecs
import hashlib
import io
from typing import BinaryIO

from .core import Options, Row, Source
from .detect import sniff_encoding
from .tokens import TextCounter, image_tokens
from . import clean

_pil_ready = False


class _UTF8JSONStream:
    """Expose transcoded reads without forwarding the original stream's readinto.

    ijson prefers readinto when available; StreamRecoder would otherwise pass
    that method to the unconverted UTF-16/legacy stream.
    """

    def __init__(self, stream: BinaryIO, encoding: str) -> None:
        self.stream = codecs.EncodedFile(stream, "utf-8", encoding, errors="strict")

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)


def _pil():
    global _pil_ready
    from PIL import Image

    if not _pil_ready:
        Image.MAX_IMAGE_PIXELS = None  # only the header is read, huge scans are fine
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        _pil_ready = True
    return Image


def image_size(stream: BinaryIO) -> tuple[int, int, int]:
    """Width, height and frame count, read from the header without decoding pixels."""
    with _pil().open(stream) as im:
        return im.size[0], im.size[1], getattr(im, "n_frames", 1)


def add_image(row: Row, stream: BinaryIO, opts: Options) -> bool:
    """Add one image's vision tokens to the row; False if the image could not be read."""
    try:
        w, h, _ = image_size(stream)
    except Exception:
        return False
    row.images += 1
    row.image_tokens += image_tokens(w, h, opts.image_detail)
    return True


def count_text_file(src: Source, row: Row, opts: Options) -> None:
    enc = sniff_encoding(src.head(), complete=src.size <= 1 << 16) or "utf-8"
    decoder = codecs.getincrementaldecoder(enc)(errors="replace")
    counter = TextCounter()
    checksum = hashlib.sha256()
    with src.open() as f:
        while chunk := f.read(1 << 20):
            checksum.update(chunk)
            counter.feed(decoder.decode(chunk))
    counter.feed(decoder.decode(b"", final=True))
    counter.close()
    row.method = f"whole file as text ({enc})"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.checksum = checksum.hexdigest()
    row.clean_chars, row.clean_text_tokens = counter.chars, counter.tokens

    parts = src.path.replace("\\", "/").lower().split("/")
    if ".state" in parts or src.ext in {".log", ".lock", ".done"}:
        row.clean_chars = row.clean_text_tokens = 0
        return
    structured = src.ext in {".json", ".jsonl", ".ndjson", ".ipynb", ".geojson", ".csv", ".tsv",
                             ".md", ".markdown", ".mdx", ".html", ".htm", ".xhtml", ".xml", ".svg"}
    if not structured:
        return
    content = TextCounter()
    try:
        with src.open() as f:
            if src.ext in {".json", ".jsonl", ".ndjson", ".ipynb", ".geojson"}:
                stream = f if enc == "utf-8" else _UTF8JSONStream(f, enc)
                clean.count_json(stream, content, slack="slack" in parts)
            elif src.ext in {".xml", ".svg"}:
                clean.count_xml(f, content)
            else:
                with io.TextIOWrapper(f, encoding=enc, errors="replace", newline="") as text:
                    if src.ext in {".csv", ".tsv"}:
                        clean.count_csv(text, content, "\t" if src.ext == ".tsv" else ",")
                    elif src.ext in {".html", ".htm", ".xhtml"}:
                        parser = clean.VisibleHTML()
                        while chunk := text.read(1 << 20):
                            parser.feed(chunk)
                            content.feed("".join(parser.parts))
                            parser.parts.clear()
                        parser.close()
                        content.feed("".join(parser.parts))
                    else:
                        clean.count_markdown(text, content)
        content.close()
    except Exception as error:
        # A malformed export still has a valid verbatim count; never silently
        # present that raw count as clean content.
        row.clean_chars = row.clean_text_tokens = 0
        row.clean_counted = False
        row.notes.append(f"clean text not parsed: {str(error).strip()[:160]}")
    else:
        row.clean_chars, row.clean_text_tokens = content.chars, content.tokens


def count_image(src: Source, row: Row, opts: Options) -> None:
    with src.open() as f:
        w, h, frames = image_size(f)
    row.images = 1
    row.image_tokens = image_tokens(w, h, opts.image_detail)
    row.method = f"image {w}x{h}, GPT-4o vision detail={opts.image_detail}"
    if frames > 1:
        row.notes.append(f"frames: {frames}, only the first counted")


def count_binary(src: Source, row: Row, opts: Options) -> None:
    """Opaque bytes carry no text: the row stays at zero tokens, base64 goes to its own column with --base64."""
    row.method, row.counted = "tokens not counted", False
