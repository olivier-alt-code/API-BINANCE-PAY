"""Parser for Binance payment notification emails.

Input: raw RFC 822 / MIME bytes. Output: :class:`ParsedBinancePayment`.

This module is independent of IMAP, of the database and of trust validation. It never
executes scripts or fetches external content: HTML is converted to text with the stdlib
:class:`html.parser.HTMLParser`, which only tokenizes markup.

All Binance-specific regexes live in :mod:`app.integrations.binance.templates`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from app.core.exceptions import BinanceEmailParseError
from app.integrations.binance.templates import BinanceEmailTemplate, PaymentStatus

MAX_EMAIL_BYTES = 5 * 1024 * 1024
_MAX_TEXT_CHARS = 200_000

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b‌‍⁠﻿­"))
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedBinancePayment:
    payment_code: str
    amount: Decimal
    asset: str
    received_at: datetime
    payment_status: PaymentStatus
    sender: str
    message_id: str | None
    template: str


# --- MIME helpers -------------------------------------------------------------------------


def parse_mime(raw: bytes) -> EmailMessage:
    if len(raw) > MAX_EMAIL_BYTES:
        raise BinanceEmailParseError("Email too large")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    return message


def sender_address(message: EmailMessage) -> str | None:
    """The single address in ``From``; ``None`` if missing or ambiguous."""
    # Parse the RAW header strictly. The lenient header registry "repairs" malformed values
    # such as ``Binance <real@binance.com> <attacker@evil.test>`` by silently dropping the
    # attacker's address, which would make a spoofed sender look legitimate.
    raw_values = [str(v) for k, v in message.raw_items() if k.lower() == "from"]
    if len(raw_values) != 1:
        return None
    raw = re.sub(r"\r?\n[ \t]", " ", raw_values[0]).strip()
    pairs = getaddresses([raw], strict=True)
    if len(pairs) != 1:
        return None
    address = pairs[0][1].strip()
    if not address or address.count("@") != 1 or any(c in address for c in "<>,;\"' \t"):
        return None
    # Defense in depth: the lenient parser must agree with the strict one.
    lenient = [a for _, a in getaddresses([str(message.get("From", ""))]) if a]
    if [a.lower() for a in lenient] != [address.lower()]:
        return None
    return address.lower()


def header_datetime(message: EmailMessage) -> datetime | None:
    value = message.get("Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # a timestamp without timezone is ambiguous
    return parsed.astimezone(UTC)


class _HTMLToText(HTMLParser):
    # Block-level tags become whitespace; inline tags (span, b, a...) do not, so that
    # "PAY-<b>123</b>" stays a single token instead of being split into "PAY-" and "123".
    _SKIP = frozenset({"script", "style", "head", "title", "noscript", "template", "svg"})
    _BLOCK = frozenset(
        {
            "br",
            "p",
            "div",
            "tr",
            "td",
            "th",
            "li",
            "table",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "section",
            "article",
            "header",
            "footer",
            "ul",
            "ol",
            "hr",
            "tbody",
            "thead",
            "dl",
            "dt",
            "dd",
            "blockquote",
            "center",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    parser.close()
    return normalize_text(parser.text())


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return _WS_RE.sub(" ", text).strip()[:_MAX_TEXT_CHARS]


def body_texts(message: EmailMessage) -> list[str]:
    """Normalized text of every text/plain and text/html body part (attachments skipped)."""
    texts: list[str] = []
    parts: Iterable[EmailMessage] = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except LookupError, ValueError, UnicodeDecodeError:
            continue
        if not isinstance(content, str):
            continue
        texts.append(html_to_text(content) if ctype == "text/html" else normalize_text(content))
    return [t for t in texts if t]


# --- Parser -------------------------------------------------------------------------------


def _unique(pattern: re.Pattern[str], text: str, group: str | Sequence[str]) -> tuple[str, ...]:
    """Return the single distinct match; raise if none or several distinct ones."""
    groups = (group,) if isinstance(group, str) else tuple(group)
    values = {tuple(m.group(g) for g in groups) for m in pattern.finditer(text)}
    if not values:
        raise BinanceEmailParseError(f"Field '{groups[0]}' not found")
    if len(values) > 1:
        raise BinanceEmailParseError(f"Field '{groups[0]}' is ambiguous")
    return next(iter(values))


def parse_amount(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise BinanceEmailParseError("Invalid amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise BinanceEmailParseError("Invalid amount")
    return amount


class BinanceEmailParser:
    def __init__(
        self,
        templates: Sequence[BinanceEmailTemplate],
        *,
        case_insensitive_code: bool = False,
    ) -> None:
        self._templates = tuple(templates)
        self._case_insensitive = case_insensitive_code

    @property
    def templates(self) -> tuple[BinanceEmailTemplate, ...]:
        return self._templates

    def normalize_code(self, code: str) -> str:
        code = code.strip()
        return code.upper() if self._case_insensitive else code

    def parse_bytes(self, raw: bytes) -> ParsedBinancePayment:
        return self.parse(parse_mime(raw))

    def parse(self, message: EmailMessage) -> ParsedBinancePayment:
        sender = sender_address(message)
        if sender is None:
            raise BinanceEmailParseError("Missing or ambiguous From header")
        subject = normalize_text(str(message.get("Subject", "")))
        texts = body_texts(message)
        if not texts:
            raise BinanceEmailParseError("Email has no readable text body")

        results: list[ParsedBinancePayment] = []
        matched_templates: set[str] = set()
        for template in self._templates:
            if template.subject is not None and not template.subject.search(subject):
                continue
            for text in texts:
                if not all(marker.search(text) for marker in template.required_markers):
                    continue
                matched_templates.add(template.name)
                results.append(self._extract(template, text, message, sender))

        if not results:
            raise BinanceEmailParseError("No Binance template matches this email")
        if len(matched_templates) > 1:
            raise BinanceEmailParseError("Email matches several templates")
        first = results[0]
        # text/plain and text/html alternatives must agree exactly.
        if any(r != first for r in results[1:]):
            raise BinanceEmailParseError("Body alternatives disagree")
        return first

    def _extract(
        self,
        template: BinanceEmailTemplate,
        text: str,
        message: EmailMessage,
        sender: str,
    ) -> ParsedBinancePayment:
        (code,) = _unique(template.payment_code, text, "code")
        amount_raw, asset = _unique(template.amount, text, ("amount", "asset"))

        if template.status is not None:
            (raw_status,) = _unique(template.status, text, "status")
            status = template.status_map.get(raw_status.lower())
            if status is None:
                raise BinanceEmailParseError("Unknown payment status")
        elif template.implied_status is not None:
            status = template.implied_status
        else:
            raise BinanceEmailParseError("Template defines no status")

        received_at: datetime | None = None
        if template.timestamp is not None:
            (ts,) = _unique(template.timestamp, text, "ts")
            try:
                received_at = datetime.strptime(ts, template.timestamp_format).replace(tzinfo=UTC)
            except ValueError as exc:
                raise BinanceEmailParseError("Invalid timestamp") from exc
        if received_at is None:
            received_at = header_datetime(message)
        if received_at is None:
            raise BinanceEmailParseError("Missing or invalid timestamp")

        message_id = message.get("Message-ID")
        return ParsedBinancePayment(
            payment_code=self.normalize_code(code),
            amount=parse_amount(amount_raw),
            asset=asset.upper(),
            received_at=received_at,
            payment_status=status,
            sender=sender,
            message_id=str(message_id).strip() if message_id else None,
            template=template.name,
        )
