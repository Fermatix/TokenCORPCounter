"""Handlers for documents: PDF, OOXML (docx/xlsx/pptx), OpenDocument, EPUB and legacy OLE files."""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from .core import Options, Row, Source
from .detect import IMAGE_EXT, ODF_CATEGORY
from .media import add_image
from .tokens import TextCounter, image_tokens

_ODF_C = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}c"
# Property blocks (pPr, rPr, ...), field codes, deleted revisions and duplicate fallback markup hold no visible text.
_SKIP_TAGS = {"instrText", "delText", "delInstrText", "tabs", "lstStyle", "Fallback"}

DOCX_PARTS = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments\w*)\.xml$")
PPTX_PARTS = re.compile(r"ppt/(slides/slide\d+|notesSlides/notesSlide\d+|comments/[^/]+)\.xml$")
MEDIA_DIRS = {"docx": "word/", "pptx": "ppt/", "xlsx": "xl/", "odf": "Pictures/", "epub": ""}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _walk(el, out: list[str], odf: bool, in_cell: bool) -> None:
    tag = _local(el.tag)
    if tag in _SKIP_TAGS or (not odf and tag.endswith("Pr")):
        return
    if odf:
        if el.text:
            out.append(el.text)
    elif tag == "t" and el.text:
        out.append(el.text)
    if tag == "tab":
        out.append("\t")
    elif tag in ("br", "cr", "line-break"):
        out.append("\n")
    elif odf and tag == "s":
        out.append(" " * int(el.get(_ODF_C) or 1))
    cell = in_cell or tag in ("tc", "table-cell")
    for child in el:
        _walk(child, out, odf, cell)
        if odf and child.tail:
            out.append(child.tail)
    if tag in ("p", "h"):
        out.append(" " if in_cell else "\n")
    elif tag in ("tc", "table-cell"):
        out.append("\t")
    elif tag in ("tr", "table-row"):
        out.append("\n")


def xml_text(data: bytes, odf: bool = False) -> str:
    """Visible text of a WordprocessingML / DrawingML / OpenDocument part: paragraphs, tables, breaks."""
    out: list[str] = []
    _walk(ET.fromstring(data), out, odf, in_cell=False)
    return "".join(out)


def props_text(data: bytes) -> str:
    """Document metadata (docProps/core.xml, meta.xml, *.opf) as "field: value" lines."""
    root = ET.fromstring(data)
    lines = [
        f"{_local(el.tag)}: {el.text.strip()}"
        for el in root.iter()
        if el is not root and el.text and el.text.strip() and not len(el)
    ]
    return "\n".join(lines) + "\n" if lines else ""


class _HtmlText(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_text(data: bytes) -> str:
    parser = _HtmlText()
    parser.feed(data.decode("utf-8", "replace"))
    parser.close()
    return "".join(parser.out)


def _zip_images(zf: zipfile.ZipFile, prefix: str, row: Row, opts: Options) -> None:
    failed = 0
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or not name.startswith(prefix) or name[name.rfind("."):].lower() not in IMAGE_EXT:
            continue
        with zf.open(info) as f:
            if not add_image(row, io.BytesIO(f.read()), opts):
                failed += 1
    if failed:
        row.notes.append(f"embedded images not read: {failed}")


def count_zip_document(src: Source, row: Row, opts: Options, kind: str) -> None:
    """docx, pptx, OpenDocument and EPUB: visible text of every content part + metadata + embedded images."""
    counter = TextCounter()
    with src.open() as f, zipfile.ZipFile(f) as zf:
        names = sorted(zf.namelist(), key=_natural)
        for meta in ("docProps/core.xml", "meta.xml"):
            if meta in names:
                counter.feed(props_text(zf.read(meta)))
        if kind in ("docx", "pptx"):
            rx = DOCX_PARTS if kind == "docx" else PPTX_PARTS
            for name in names:
                if rx.match(name):
                    counter.feed(xml_text(zf.read(name)) + "\n")
        elif kind == "odf":
            mime = zf.read("mimetype").decode("ascii", "replace").strip()
            row.category = ODF_CATEGORY.get(mime.rsplit(".", 1)[-1], "document")
            counter.feed(xml_text(zf.read("content.xml"), odf=True))
        elif kind == "epub":
            for name in names:
                low = name.lower()
                if low.endswith(".opf"):
                    counter.feed(props_text(zf.read(name)))
                elif low.endswith((".xhtml", ".html", ".htm")):
                    counter.feed(html_text(zf.read(name)))
        _zip_images(zf, MEDIA_DIRS[kind], row, opts)
    counter.close()
    row.method = "document text, tables, notes and metadata"
    row.chars, row.text_tokens = counter.chars, counter.tokens


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _feed_row(counter: TextCounter, values) -> None:
    cells = [_cell(v) for v in values]
    while cells and not cells[-1]:
        cells.pop()
    if cells:
        counter.feed("\t".join(cells) + "\n")


def count_xlsx(src: Source, row: Row, opts: Options) -> None:
    """Every sheet, every non-empty row as tab-separated values (cached formula results)."""
    from openpyxl import load_workbook

    counter = TextCounter()
    with src.open() as f:
        wb = load_workbook(f, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                counter.feed(f"{ws.title}\n")
                for values in ws.iter_rows(values_only=True):
                    _feed_row(counter, values)
        finally:
            wb.close()
        f.seek(0)
        with zipfile.ZipFile(f) as zf:
            if "docProps/core.xml" in zf.namelist():
                counter.feed(props_text(zf.read("docProps/core.xml")))
            _zip_images(zf, MEDIA_DIRS["xlsx"], row, opts)
    counter.close()
    row.method = "all sheets and cells (TSV) + metadata"
    row.chars, row.text_tokens = counter.chars, counter.tokens


def count_xls(src: Source, row: Row, opts: Options) -> None:
    import xlrd

    counter = TextCounter()
    book = xlrd.open_workbook(file_contents=src.read(), on_demand=True)
    try:
        for sheet in book.sheets():
            counter.feed(f"{sheet.name}\n")
            for r in range(sheet.nrows):
                _feed_row(counter, sheet.row_values(r))
    finally:
        book.release_resources()
    counter.close()
    row.method = "all sheets and cells (TSV)"
    row.chars, row.text_tokens = counter.chars, counter.tokens


def _pdf_image_dims(reader) -> list[tuple[int, int]]:
    """Unique image XObjects across pages, including those nested in form XObjects."""
    from pypdf.generic import IndirectObject

    seen: set = set()
    dims: list[tuple[int, int]] = []

    def walk(resources) -> None:
        resources = resources.get_object() if resources is not None else None
        xobjects = resources.get("/XObject") if resources else None
        if xobjects is None:
            return
        xobjects = xobjects.get_object()
        for name in xobjects:
            ref = xobjects.raw_get(name)
            key = (ref.idnum, ref.generation) if isinstance(ref, IndirectObject) else id(ref)
            if key in seen:
                continue
            seen.add(key)
            obj = ref.get_object()
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                dims.append((int(obj.get("/Width", 0)), int(obj.get("/Height", 0))))
            elif subtype == "/Form":
                walk(obj.get("/Resources"))

    for page in reader.pages:
        walk(page.get("/Resources"))
    return dims


def count_pdf(src: Source, row: Row, opts: Options) -> None:
    from pypdf import PdfReader

    counter = TextCounter()
    with src.open() as f:
        reader = PdfReader(f)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF is password-protected")
        meta = reader.metadata or {}
        counter.feed("".join(f"{k.lstrip('/')}: {v}\n" for k, v in meta.items() if v not in (None, "")))
        failed = 0
        for page in reader.pages:
            try:
                counter.feed((page.extract_text() or "") + "\n")
            except Exception:
                failed += 1
        try:
            dims = _pdf_image_dims(reader)
        except Exception:
            dims = []
            row.notes.append("could not list images")
        pages = len(reader.pages)
    counter.close()
    for w, h in dims:
        row.images += 1
        row.image_tokens += image_tokens(w, h, opts.image_detail)
    row.method = f"text of {pages} pages + metadata + embedded images"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    if failed:
        row.notes.append(f"pages without extracted text: {failed}")
    if dims and row.chars < 50 * pages:
        row.notes.append("almost no text layer (scan?), OCR not performed")


# UTF-16LE runs of Latin-1, Cyrillic and general punctuation; 8-bit ASCII runs.
_UTF16_RUN = re.compile(rb"(?:[\t\n\r\x20-\x7e\xa0-\xff]\x00|[\x00-\xff][\x04\x20]){4,}")
_ASCII_RUN = re.compile(rb"[\t\n\r\x20-\x7e]{6,}")
_OLE_TEXT_STREAM = {"doc": "WordDocument", "ppt": "PowerPoint Document"}


def count_ole_strings(src: Source, row: Row, opts: Options, kind: str) -> None:
    """Legacy .doc/.ppt: no pure-Python parser, so text runs are pulled out of the main stream."""
    import olefile

    with src.open() as f:
        ole = olefile.OleFileIO(f)
        try:
            data = ole.openstream(_OLE_TEXT_STREAM[kind]).read()
        finally:
            ole.close()
    parts = [m.group().decode("utf-16-le", "replace") for m in _UTF16_RUN.finditer(data)]
    parts += [m.group().decode("ascii") for m in _ASCII_RUN.finditer(data)]
    counter = TextCounter()
    counter.feed("\n".join(parts))
    counter.close()
    row.method = "text strings from a binary format"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.notes.append("legacy format: approximate count, resave as docx/pptx for an exact one")
