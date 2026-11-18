"""Handlers for documents: PDF, OOXML (docx/xlsx/pptx), OpenDocument, EPUB and legacy OLE files."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from .core import Options, Row, Source
from .detect import IMAGE_EXT, ODF_CATEGORY
from .media import add_image
from .tokens import TextCounter, image_tokens
from .clean import feed_content, service_key

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
    content = TextCounter()
    with src.open() as f, zipfile.ZipFile(f) as zf:
        names = sorted(zf.namelist(), key=_natural)
        for meta in ("docProps/core.xml", "meta.xml"):
            if meta in names:
                counter.feed(props_text(zf.read(meta)))
        if kind in ("docx", "pptx"):
            rx = DOCX_PARTS if kind == "docx" else PPTX_PARTS
            for name in names:
                if rx.match(name):
                    text = xml_text(zf.read(name)) + "\n"
                    counter.feed(text)
                    content.feed(text)
        elif kind == "odf":
            mime = zf.read("mimetype").decode("ascii", "replace").strip()
            row.category = ODF_CATEGORY.get(mime.rsplit(".", 1)[-1], "document")
            text = xml_text(zf.read("content.xml"), odf=True)
            counter.feed(text)
            content.feed(text)
        elif kind == "epub":
            for name in names:
                low = name.lower()
                if low.endswith(".opf"):
                    counter.feed(props_text(zf.read(name)))
                elif low.endswith((".xhtml", ".html", ".htm")):
                    text = html_text(zf.read(name))
                    counter.feed(text)
                    content.feed(text)
        _zip_images(zf, MEDIA_DIRS[kind], row, opts)
    counter.close()
    content.close()
    row.method = "document text, tables, notes and metadata"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.clean_chars, row.clean_text_tokens = content.chars, content.tokens


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


class _Formula(str):
    """A formula for which the spreadsheet has no cached result."""


def _table_row(counter: TextCounter, content: TextCounter, values, excluded: set[int] | None) -> set[int] | None:
    _feed_row(counter, values)
    if not any(value is not None and value != "" for value in values):
        return excluded
    if excluded is None:
        excluded = {i for i, value in enumerate(values) if isinstance(value, str) and service_key(value)}
    feed_content(content, "\t".join(_cell(value) for i, value in enumerate(values)
                                   if i not in excluded and not isinstance(value, _Formula)))
    return excluded


def _xlsx_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings = []
    with zf.open("xl/sharedStrings.xml") as stream:
        events = ET.iterparse(stream, events=("start", "end"))
        _, root = next(events)
        for event, el in events:
            if event == "end" and _local(el.tag) == "si":
                strings.append(_xlsx_rich_text(el))
                root.clear()
    return strings


def _xlsx_rich_text(element) -> str:
    # Phonetic guides (rPh) are not extra visible cell content.
    parts = []
    for child in element:
        if _local(child.tag) == "t":
            parts.append(child.text or "")
        elif _local(child.tag) == "r":
            parts.extend(node.text or "" for node in child if _local(node.tag) == "t")
    return "".join(parts)


def _xlsx_date_styles(zf: zipfile.ZipFile) -> dict[int, bool]:
    from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format, is_timedelta_format

    if "xl/styles.xml" not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read("xl/styles.xml"))
    formats = dict(BUILTIN_FORMATS)
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            formats[int(el.attrib["numFmtId"])] = el.attrib["formatCode"]
    styles = {}
    for el in root:
        if _local(el.tag) == "cellXfs":
            for index, xf in enumerate(el):
                fmt = formats.get(int(xf.get("numFmtId", "0")), "")
                if is_date_format(fmt):
                    styles[index] = is_timedelta_format(fmt)
    return styles


def _xlsx_rows(zf, path, strings, date_styles, epoch, issues):
    from openpyxl.utils.datetime import from_excel

    with zf.open(path) as stream:
        events = ET.iterparse(stream, events=("start", "end"))
        _, root = next(events)
        for event, el in events:
            if event != "end" or _local(el.tag) != "row":
                continue
            values = []
            for cell in el:
                if _local(cell.tag) != "c":
                    continue
                coordinate = cell.get("r", "")
                column = 0
                for ch in re.match(r"[A-Za-z]*", coordinate).group().upper():
                    column = column * 26 + ord(ch) - ord("A") + 1
                column = column or len(values) + 1
                if column > 16384:
                    raise ValueError(f"invalid spreadsheet column: {coordinate}")
                kind = cell.get("t", "n")
                children = {_local(child.tag): child for child in cell}
                raw = children.get("v")
                value = raw.text if raw is not None else None
                if kind == "inlineStr":
                    inline = children.get("is")
                    value = _xlsx_rich_text(inline) if inline is not None else ""
                elif value is not None:
                    if kind == "s":
                        value = strings[int(value)]
                    elif kind == "b":
                        value = bool(int(value))
                    elif kind == "n":
                        value = float(value) if any(ch in value for ch in ".eE") else int(value)
                        style = int(cell.get("s", "0"))
                        if style in date_styles:
                            try:
                                value = from_excel(value, epoch, timedelta=date_styles[style])
                            except (OverflowError, ValueError):
                                issues["dates"] += 1  # Keep the original number, not #VALUE!.
                elif "f" in children:
                    value = _Formula("=" + (children["f"].text or ""))
                    issues["formulas"] += 1
                if value is not None:
                    if len(values) < column:
                        values.extend([None] * (column - len(values)))
                    values[column - 1] = value
            yield values
            el.clear()
            root.clear()


def count_xlsx(src: Source, row: Row, opts: Options) -> None:
    """Stream actual cells; preserve numbers with invalid date styles and cached formulas."""
    from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900

    counter, content = TextCounter(), TextCounter()
    issues = {"dates": 0, "formulas": 0}
    with src.open() as stream, zipfile.ZipFile(stream) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        epoch = CALENDAR_WINDOWS_1900
        for el in workbook.iter():
            if _local(el.tag) == "workbookPr" and el.get("date1904") in ("1", "true"):
                epoch = CALENDAR_MAC_1904
        relationships = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        targets = {el.get("Id"): el.get("Target") for el in relationships}
        strings = _xlsx_strings(zf)
        date_styles = _xlsx_date_styles(zf)
        for sheet in workbook.iter():
            if _local(sheet.tag) != "sheet":
                continue
            title = sheet.get("name", "")
            counter.feed(title + "\n")
            if not re.fullmatch(r"(?:Sheet|Лист)\d+", title, re.I):
                feed_content(content, title)
            rel_id = next((value for key, value in sheet.attrib.items() if _local(key) == "id"), None)
            target = targets[rel_id]
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            if not path.startswith("xl/worksheets/"):
                continue  # Chart and dialog sheets have no table cells.
            excluded = None
            for values in _xlsx_rows(zf, path, strings, date_styles, epoch, issues):
                excluded = _table_row(counter, content, values, excluded)
        if "docProps/core.xml" in zf.namelist():
            counter.feed(props_text(zf.read("docProps/core.xml")))
        _zip_images(zf, MEDIA_DIRS["xlsx"], row, opts)
    counter.close()
    content.close()
    row.method = "all actual cells (TSV) + metadata; clean content without service columns"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.clean_chars, row.clean_text_tokens = content.chars, content.tokens
    if issues["dates"]:
        row.notes.append(f"invalid Excel dates kept as raw numbers: {issues['dates']}")
    if issues["formulas"]:
        row.notes.append(f"formulas without cached results (clean count excludes them): {issues['formulas']}")


def count_xls(src: Source, row: Row, opts: Options) -> None:
    import xlrd

    counter, content = TextCounter(), TextCounter()
    data = src.read()
    try:
        book = xlrd.open_workbook(file_contents=data, on_demand=True, logfile=io.StringIO())
    except xlrd.compdoc.CompDocError:
        book = xlrd.open_workbook(file_contents=data, on_demand=True, logfile=io.StringIO(),
                                  ignore_workbook_corruption=True)
        row.notes.append("XLS read after ignoring OLE sector overlap; verify against the original")
    try:
        for sheet in book.sheets():
            counter.feed(f"{sheet.name}\n")
            if not re.fullmatch(r"(?:Sheet|Лист)\d+", sheet.name, re.I):
                feed_content(content, sheet.name)
            excluded = None
            for r in range(sheet.nrows):
                excluded = _table_row(counter, content, sheet.row_values(r), excluded)
    finally:
        book.release_resources()
    counter.close()
    content.close()
    row.method = "all sheets and cells (TSV)"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.clean_chars, row.clean_text_tokens = content.chars, content.tokens


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
    content = TextCounter()
    with src.open() as f:
        reader = PdfReader(f)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF is password-protected")
        meta = reader.metadata or {}
        counter.feed("".join(f"{k.lstrip('/')}: {v}\n" for k, v in meta.items() if v not in (None, "")))
        failed = 0
        for page in reader.pages:
            try:
                text = (page.extract_text() or "") + "\n"
                counter.feed(text)
                content.feed(text)
            except Exception:
                failed += 1
        try:
            dims = _pdf_image_dims(reader)
        except Exception:
            dims = []
            row.notes.append("could not list images")
        pages = len(reader.pages)
    counter.close()
    content.close()
    for w, h in dims:
        row.images += 1
        row.image_tokens += image_tokens(w, h, opts.image_detail)
    row.method = f"text of {pages} pages + metadata + embedded images"
    row.chars, row.text_tokens = counter.chars, counter.tokens
    row.clean_chars, row.clean_text_tokens = content.chars, content.tokens
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
    row.clean_chars, row.clean_text_tokens = counter.chars, counter.tokens
    row.notes.append("legacy format: approximate count, resave as docx/pptx for an exact one")
