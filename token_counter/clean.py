from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from decimal import Decimal
from typing import BinaryIO

from .tokens import TextCounter

# CSV exports can contain a long message or JSON value in one quoted cell.
csv.field_size_limit(1 << 30)

_SERVICE = {
    "id", "uuid", "guid", "type", "object", "subtype", "version", "etag", "hash", "checksum",
    "url", "uri", "href", "self", "links", "permalink", "avatar", "avatar_urls", "icon", "cover",
    "created", "updated", "modified", "created_at", "updated_at", "deleted_at", "timestamp", "ts",
    "created_time", "last_edited_time", "page_last_edited_at", "exported_at", "downloaded_at",
    "metadata", "meta", "statistics", "stats", "pagination", "paging", "cursor", "next_cursor",
    "has_more", "total_count", "request_id", "response_metadata", "headers", "headers_raw",
    "author", "user", "creator", "editor", "assignee", "reporter", "responsible_user_id",
    "permissions", "permission", "access", "acl", "verification", "full_width", "archived",
    "attachments", "files", "reactions", "mentions", "subscribed", "read", "is_read", "redacted",
    "waiting_since", "snoozed_until", "delivered_as", "part_type", "external_id", "email_message_metadata",
    "thread_ts", "parent_user_id", "reply_users", "reply_users_count", "reply_count", "latest_reply",
    "client_msg_id", "team", "channel", "bot_id", "app_id", "bot_profile", "display_as_bot",
    "notified_at", "assigned_to", "event_details", "app_package_code", "first_contact_reply",
    "teammates", "contacts", "ai_agent", "ai_agent_participated", "conversation_rating",
    "sla_applied", "linked_objects", "field_id", "field_type", "enum_id", "sort", "account_id",
    "expand", "schema", "names", "rendered_fields", "changelog", "audit_log", "base64", "data_base64",
    "export", "export_metadata",
}
_SERVICE_SUFFIX = re.compile(r"(?:^|_)(?:id|ids|uuid|url|uri|timestamp)$|_(?:created|updated|deleted|edited)_at$")
_HTML = re.compile(r"</?(?:p|div|br|span|a|table|tr|td|body|html|head|script|style|ul|li|h[1-6]|strong|em)\b", re.I)
_LABEL = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:id|uuid|url|notion|путь|изменено|создано|источник|"
    r"created(?: at)?|updated(?: at)?|modified|exported|timestamp|page id|channel id|"
    r"responsible|автор|author)(?:\*\*)?\s*:", re.I,
)
_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\([^\n)]*\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^\n)]*\)")


def service_key(key: str) -> bool:
    key = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", key).lower().strip("_ ").replace("-", " ").replace(" ", "_")
    return key in _SERVICE or bool(_SERVICE_SUFFIX.search(key))


class VisibleHTML(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        elif tag in self._BLOCK and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.hidden:
            self.hidden -= 1
        elif tag in self._BLOCK and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_html(text: str) -> str:
    parser = VisibleHTML()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def content_text(text: str) -> str:
    if _HTML.search(text):
        text = visible_html(text)
    return " ".join(text.split())


def feed_content(counter: TextCounter, text: str) -> None:
    text = content_text(text)
    if text:
        counter.feed(text + "\n")


def count_json(stream: BinaryIO, counter: TextCounter, *, slack: bool = False) -> None:
    import ijson

    if slack:
        # Slack's text and blocks are alternative representations. Work one
        # message at a time so blocks-only messages are retained, in any key order.
        first = stream.read(1)
        while first and first.isspace():
            first = stream.read(1)
        stream.seek(0)
        prefix = "item" if first == b"[" else ""

        def visit(value):
            if isinstance(value, dict):
                has_text = isinstance(value.get("text"), str) and bool(value["text"].strip())
                for key, child in value.items():
                    if not service_key(key) and not (key == "blocks" and has_text):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                if not value.startswith("data:"):
                    feed_content(counter, value)
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                feed_content(counter, str(value))

        for record in ijson.items(stream, prefix, multiple_values=True):
            visit(record)
        return

    stack: list[bool] = []
    blocked = False
    for _, event, value in ijson.parse(stream, multiple_values=True):
        if event == "map_key":
            blocked = (stack[-1] if stack else False) or service_key(value)
        elif event in ("start_map", "start_array"):
            stack.append(blocked)
        elif event in ("end_map", "end_array"):
            stack.pop()
            blocked = stack[-1] if stack else False
        elif not blocked:
            if event == "string":
                # Links in a message are content; link fields are filtered by key.
                if not value.startswith("data:"):
                    feed_content(counter, value)
            elif event == "number":
                feed_content(counter, str(value))


def count_csv(stream: io.TextIOBase, counter: TextCounter, delimiter: str) -> None:
    sample = stream.read(8192)
    stream.seek(0)
    if delimiter == ",":
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            pass
    reader = csv.reader(stream, delimiter=delimiter)
    header = next(reader, None)
    if header is None:
        return
    excluded = {index for index, cell in enumerate(header) if service_key(cell)}
    feed_content(counter, "\t".join(value for index, value in enumerate(header) if index not in excluded))
    for values in reader:
        feed_content(counter, "\t".join(value for index, value in enumerate(values) if index not in excluded))


def count_xml(stream: BinaryIO, counter: TextCounter) -> None:
    class ContentTarget:
        def __init__(self):
            self.blocked: list[bool] = []

        def start(self, tag, attrs):
            self.blocked.append((self.blocked[-1] if self.blocked else False) or service_key(tag.rsplit("}", 1)[-1]))

        def data(self, text):
            if self.blocked and not self.blocked[-1]:
                counter.feed(text)

        def end(self, tag):
            if not self.blocked.pop():
                counter.feed("\n")

        def close(self):
            return None

    parser = ET.XMLParser(target=ContentTarget())
    while chunk := stream.read(1 << 20):
        parser.feed(chunk)
    parser.close()


def count_markdown(stream: io.TextIOBase, counter: TextCounter) -> None:
    frontmatter = False
    fence = False
    for index, line in enumerate(stream):
        stripped = line.strip()
        if index == 0 and stripped == "---":
            frontmatter = True
            continue
        if frontmatter:
            if stripped in ("---", "..."):
                frontmatter = False
            continue
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            continue
        if not fence:
            if _LABEL.match(line) or re.fullmatch(r"[\s|:\-]+", line):
                continue
            line = _IMAGE.sub("", line)
            line = _LINK.sub(r"\1", line)
            line = re.sub(r"^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)", "", line)
            line = line.replace("**", "").replace("__", "").replace("`", "")
        feed_content(counter, line)
