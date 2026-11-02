"""Handlers for plain text, images and opaque binaries."""

from __future__ import annotations

import codecs
from typing import BinaryIO

from .core import Options, Row, Source
from .detect import sniff_encoding
from .tokens import TextCounter, image_tokens

_pil_ready = False


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
    with src.open() as f:
        while chunk := f.read(1 << 20):
            counter.feed(decoder.decode(chunk))
    counter.feed(decoder.decode(b"", final=True))
    counter.close()
    row.method = f"whole file as text ({enc})"
    row.chars, row.text_tokens = counter.chars, counter.tokens


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
