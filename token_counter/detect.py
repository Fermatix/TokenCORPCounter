"""Format detection (magic bytes, container contents, extension) and text encoding sniffing."""

from __future__ import annotations

import zipfile

from .core import Source

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

IMAGE_EXT = {
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".webp", ".bmp", ".dib", ".tif", ".tiff",
    ".ico", ".cur", ".icns", ".heic", ".heif", ".avif", ".tga", ".ppm", ".pgm", ".pbm", ".pnm",
    ".pcx", ".psd", ".jp2", ".j2k", ".jpx", ".dds", ".emf", ".wmf", ".xbm", ".sgi", ".qoi",
}
MEDIA_EXT = {
    ".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".wma", ".aiff", ".amr",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg",
}
TEXT_CATEGORY = {
    ".json": "json", ".jsonl": "json", ".ndjson": "json", ".ipynb": "json", ".geojson": "json",
    ".csv": "table", ".tsv": "table",
    ".md": "markdown", ".markdown": "markdown", ".mdx": "markdown",
    ".html": "markup", ".htm": "markup", ".xhtml": "markup", ".xml": "markup", ".svg": "markup",
    ".rtf": "document",
}
CATEGORY = {
    "pdf": "pdf", "docx": "document", "doc": "document", "epub": "document",
    "xlsx": "table", "xls": "table", "pptx": "presentation", "ppt": "presentation",
    "image": "image", "eml": "email", "mbox": "email", "msg": "email", "media": "media",
}
ODF_CATEGORY = {"text": "document", "spreadsheet": "table", "presentation": "presentation"}

_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis")
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def detect(src: Source) -> str:
    """Return a handler key: pdf, docx, xlsx, pptx, odf, epub, zip, tar, gzip, bzip2, xz, 7z,
    doc, xls, ppt, msg, image, eml, mbox, media, text, binary or empty."""
    if src.size == 0:
        return "empty"
    head = src.head(4096)
    ext = src.ext
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _zip_kind(src)
    if head.startswith(OLE_MAGIC):
        return _ole_kind(src)
    if _is_image(head, ext):
        return "image"
    for magic, kind in (
        (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
        (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"),
    ):
        if head.startswith(magic):
            return kind
    if head[257:262] == b"ustar":
        return "tar"
    if ext in (".eml", ".emlx", ".mht", ".mhtml"):
        return "eml"
    if ext == ".mbox":
        return "mbox"
    if ext in IMAGE_EXT:
        return "image"
    if ext in MEDIA_EXT or _is_media(head):
        return "media"
    return "text" if sniff_encoding(head, complete=src.size <= len(head)) else "binary"


def category(kind: str, src: Source) -> str:
    if kind in ("text", "empty"):
        return TEXT_CATEGORY.get(src.ext, "text")
    return CATEGORY.get(kind, "binary")


def _is_image(head: bytes, ext: str) -> bool:
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"8BPS", b"icns", b"qoif")):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return True
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return True
    if head.startswith(b"\x00\x00\x00\x0cjP  "):
        return True
    # Short or ambiguous signatures only count together with a matching extension.
    return (head.startswith(b"BM") and ext in (".bmp", ".dib")) or (
        head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00") and ext in (".ico", ".cur")
    )


def _is_media(head: bytes) -> bool:
    return (
        head.startswith((b"ID3", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3", b"FLV", b"#!AMR"))
        or (head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI "))
        or head[4:8] == b"ftyp"
    )


def _zip_kind(src: Source) -> str:
    try:
        with src.open() as f, zipfile.ZipFile(f) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptx"
            if "mimetype" in names:
                mime = zf.read("mimetype")[:100].decode("ascii", "replace").strip()
                if mime == "application/epub+zip":
                    return "epub"
                if mime.startswith("application/vnd.oasis.opendocument."):
                    return "odf"
    except (zipfile.BadZipFile, OSError, ValueError, KeyError):
        return "binary"
    return "zip"


def _ole_kind(src: Source) -> str:
    by_ext = {".doc": "doc", ".dot": "doc", ".xls": "xls", ".xlt": "xls", ".ppt": "ppt", ".pps": "ppt", ".msg": "msg"}
    try:
        import olefile

        with src.open() as f:
            ole = olefile.OleFileIO(f)
            streams = {"/".join(e) for e in ole.listdir()}
            ole.close()
    except Exception:
        return by_ext.get(src.ext, "binary")
    if "WordDocument" in streams:
        return "doc"
    if "Workbook" in streams or "Book" in streams:
        return "xls"
    if "PowerPoint Document" in streams:
        return "ppt"
    if any(s.startswith("__substg1.0_") for s in streams):
        return "msg"
    return by_ext.get(src.ext, "binary")


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f\v\b\x1b")
    return bad / len(text)


def _utf16_without_bom(head: bytes) -> str | None:
    sample = head[: len(head) // 2 * 2]
    if len(sample) < 4:
        return None
    even = sample[0::2].count(0) / (len(sample) / 2)
    odd = sample[1::2].count(0) / (len(sample) / 2)
    if odd > 0.4 and even < 0.05:
        return "utf-16-le"
    if even > 0.4 and odd < 0.05:
        return "utf-16-be"
    return None


def sniff_encoding(head: bytes, complete: bool) -> str | None:
    """Codec name if `head` looks like text, otherwise None (binary)."""
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc
    if b"\x00" in head:
        enc = _utf16_without_bom(head)
        if enc and _control_ratio(head.decode(enc, "replace")) < 0.05:
            return enc
        return None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte character cut by the sniff window is still valid UTF-8.
        if not complete and e.reason == "unexpected end of data" and e.start >= len(head) - 3:
            text = head[: e.start].decode("utf-8")
        else:
            text = None
    if text is not None:
        return "utf-8" if _control_ratio(text) < 0.05 else None
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        return "cp1251" if _control_ratio(head.decode("cp1251", "replace")) < 0.05 else None
    best = from_bytes(head).best()
    if best is None or _control_ratio(str(best)) >= 0.05:
        return None
    return best.encoding
