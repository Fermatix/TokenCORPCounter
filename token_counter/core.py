"""Shared data structures: a file to scan, scan options and a report row."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


@dataclass
class Options:
    image_detail: str = "high"
    base64: bool = False
    max_depth: int = 8
    max_member_bytes: int = 2 << 30


@dataclass
class Source:
    """A file on disk or a member extracted from an archive / email."""

    path: str  # display path, nested members look like "archive.zip!/dir/file.txt"
    name: str  # file name used for extension detection
    size: int
    file: Path | None = None
    data: bytes | None = None
    depth: int = 0
    attachment: bool = False

    @classmethod
    def from_file(cls, file: Path, display: str) -> Source:
        return cls(path=display, name=file.name, size=file.stat().st_size, file=file)

    @classmethod
    def from_bytes(cls, parent: Source, name: str, data: bytes) -> Source:
        name = name.replace("\\", "/").strip("/") or "unnamed"
        return cls(
            path=f"{parent.path}!/{name}",
            name=os.path.basename(name),
            size=len(data),
            data=data,
            depth=parent.depth + 1,
            attachment=parent.attachment,
        )

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    def open(self) -> BinaryIO:
        if self.data is not None:
            return io.BytesIO(self.data)
        return open(self.file, "rb")

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        return self.file.read_bytes()

    def head(self, n: int = 1 << 16) -> bytes:
        if self.data is not None:
            return self.data[:n]
        with open(self.file, "rb") as f:
            return f.read(n)


@dataclass
class Row:
    path: str
    category: str
    fmt: str
    method: str
    size: int
    chars: int = 0
    text_tokens: int = 0
    images: int = 0
    image_tokens: int = 0
    base64_tokens: int | None = None
    counted: bool = True  # False when the content could not be turned into tokens (binary, skipped, error)
    notes: list[str] = field(default_factory=list)
    clean_text_tokens: int = 0
    clean_chars: int = 0
    clean_counted: bool = True
    is_attachment: bool = False
    checksum: str = ""
    duplicate_of: str = ""
    error: bool = False

    @property
    def clean_tokens(self) -> int:
        return 0 if self.is_attachment else self.clean_text_tokens

    @property
    def dirty_tokens(self) -> int:
        return 0 if self.is_attachment else self.text_tokens

    @property
    def attachment_text_tokens(self) -> int:
        return self.text_tokens if self.is_attachment else 0

    @property
    def clean_attachment_text_tokens(self) -> int:
        return self.clean_text_tokens if self.is_attachment else 0

    @property
    def attachment_tokens(self) -> int:
        return self.attachment_text_tokens + self.image_tokens

    @property
    def clean_with_attachments(self) -> int:
        return self.clean_text_tokens + self.image_tokens

    @property
    def dirty_with_attachments(self) -> int:
        return self.text_tokens + self.image_tokens

    @property
    def total_tokens(self) -> int:
        return self.text_tokens + self.image_tokens

    @property
    def note(self) -> str:
        return "; ".join(self.notes)
