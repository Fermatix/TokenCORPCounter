"""o200k_base token counting: exact for text of any size, plus GPT-4o image token estimate."""

from __future__ import annotations

import base64
import math
import re
from typing import BinaryIO

import tiktoken

ENCODING_NAME = "o200k_base"

# Text is encoded in chunks so a multi-gigabyte file never becomes one giant token list.
CHUNK_CHARS = 1 << 20

# Cut points where o200k_base pre-tokenization is guaranteed to start a new piece,
# so encoding the halves separately gives exactly the same token count:
#  - right after "\n" followed by a non-space char other than "/" (punctuation pieces absorb "[\r\n/]*");
#  - between a letter/digit and a space or ASCII punctuation other than "'" (contractions glue to letters).
_NEWLINE_CUT = re.compile(r"\n(?=[^\s/])")
_PUNCT_CUT = re.compile(r"[^\W_](?=[ !-&(-/:-@\[-`{-~])")

_encoding: tiktoken.Encoding | None = None


def encoding() -> tiktoken.Encoding:
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding(ENCODING_NAME)
    return _encoding


def _cut_point(text: str, start: int, end: int) -> int:
    """Best exact cut in text[start:end], preferring the far half; falls back to `end`."""
    for lo in (start + (end - start) // 2, start):
        for rx in (_NEWLINE_CUT, _PUNCT_CUT):
            last = None
            for last in rx.finditer(text, lo, end):
                pass
            if last is not None and last.end() > start:
                return last.end()
    # Pathological input (e.g. megabytes of one letter): an inexact cut costs at most a token.
    return end


class TextCounter:
    """Incremental token counter: feed text in arbitrary pieces, get the exact total."""

    def __init__(self) -> None:
        self.tokens = 0
        self.chars = 0
        self._parts: list[str] = []
        self._pending = 0

    def feed(self, text: str) -> None:
        if not text:
            return
        self.chars += len(text)
        self._parts.append(text)
        self._pending += len(text)
        if self._pending <= CHUNK_CHARS:
            # JSON fields and table rows arrive in small pieces. Avoid copying
            # the entire pending megabyte on each field.
            if len(self._parts) >= 8192:
                self._parts = ["".join(self._parts)]
            return
        buf = "".join(self._parts)
        start = 0
        enc = encoding()
        while len(buf) - start > CHUNK_CHARS:
            cut = _cut_point(buf, start, start + CHUNK_CHARS)
            self.tokens += len(enc.encode_ordinary(buf[start:cut]))
            start = cut
        tail = buf[start:]
        self._parts = [tail] if tail else []
        self._pending = len(tail)

    def close(self) -> int:
        if self._parts:
            self.tokens += len(encoding().encode_ordinary("".join(self._parts)))
            self._parts.clear()
            self._pending = 0
        return self.tokens


def count_text(text: str) -> int:
    counter = TextCounter()
    counter.feed(text)
    return counter.close()


def count_base64(stream: BinaryIO, block: int = 3 * (1 << 20)) -> tuple[int, int]:
    """Tokens and chars of the file's bytes encoded as base64 (how binaries travel inside JSON/API payloads)."""
    counter = TextCounter()
    while data := stream.read(block):
        # Blocks are multiples of 3 bytes, so concatenated encodings equal the whole-file encoding.
        while len(data) % 3 and (more := stream.read(3 - len(data) % 3)):
            data += more
        counter.feed(base64.b64encode(data).decode("ascii"))
    counter.close()
    return counter.tokens, counter.chars


def image_tokens(width: int, height: int, detail: str = "high") -> int:
    """GPT-4o / GPT-4.1 / o-series vision cost: 85 base + 170 per 512px tile after resizing."""
    if detail == "low" or width <= 0 or height <= 0:
        return 85
    scale = min(1.0, 2048 / max(width, height))
    w, h = width * scale, height * scale
    scale = min(1.0, 768 / min(w, h))
    w, h = w * scale, h * scale
    return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)
