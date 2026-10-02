"""Binance notification email templates — the ONLY place with Binance-format regexes.

⚠️  IMPORTANT: no real Binance payment notification was available when this project was
written. Every template marked ``simulated=True`` describes an INVENTED format used for
fixtures and tests. Simulated templates are refused in production unless
``BINANCE_ALLOW_SIMULATED_TEMPLATES=true`` is set explicitly.

To support the real format:

1. Save a real notification as ``.eml`` in ``tests/fixtures/binance/`` (see README).
2. Add a new :class:`BinanceEmailTemplate` below with ``simulated=False`` that matches it.
3. Add a test in ``tests/unit/test_binance_email_parser.py`` for that fixture.

Nothing else in the application needs to change: the rest of the code only consumes
:class:`app.integrations.binance.email_parser.ParsedBinancePayment`.

Pattern rules (enforced by the parser):

* patterns are applied to *normalized text* (HTML stripped, entities decoded,
  whitespace collapsed to single spaces, NFKC-normalized);
* ``payment_code`` must define the named group ``code``;
* ``amount`` must define the named groups ``amount`` and ``asset``;
* ``status`` (optional) must define ``status``; ``timestamp`` (optional) ``ts``;
* if a pattern matches several DIFFERENT values in one email, the email is rejected as
  ambiguous — the parser never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class PaymentStatus(StrEnum):
    PAID = "PAID"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


# --- Reusable building blocks -------------------------------------------------------------

# A payment identifier: letters, digits, '-' and '_' (4..64 chars). The trailing negative
# lookahead guarantees we capture the WHOLE token and never a prefix of a longer one.
CODE = r"(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{3,63})(?![A-Za-z0-9_-])"

# An amount: either plain digits with an optional decimal part, or digits grouped by
# commas in thousands (1,234.56). Locale-ambiguous forms like "25,50" do NOT match (the
# lookahead rejects a trailing comma/digit), so they can never be misread as 2550.
AMOUNT = r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d,.])"

# An asset ticker such as USDT, BTC, FDUSD, 1INCH.
ASSET = r"(?P<asset>[A-Z0-9]{2,15})(?![A-Za-z0-9])"

TIMESTAMP = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\(?UTC\)?"


@dataclass(frozen=True)
class BinanceEmailTemplate:
    name: str
    simulated: bool
    payment_code: re.Pattern[str]
    amount: re.Pattern[str]
    # Every marker must be present in the normalized body for the template to apply.
    required_markers: tuple[re.Pattern[str], ...] = ()
    subject: re.Pattern[str] | None = None
    status: re.Pattern[str] | None = None
    # Lower-cased raw status text -> normalized status. Unknown raw values are rejected.
    status_map: dict[str, PaymentStatus] = field(default_factory=dict)
    # Used only when the template has no status pattern (e.g. "payment received" emails
    # whose very existence means the payment completed).
    implied_status: PaymentStatus | None = None
    timestamp: re.Pattern[str] | None = None
    timestamp_format: str = "%Y-%m-%d %H:%M:%S"


_I = re.IGNORECASE

# =========================================================================================
# SIMULATED TEMPLATES — invented formats, NOT confirmed against real Binance emails.
# =========================================================================================

SIMULATED_PAYMENT_RECEIVED_V1 = BinanceEmailTemplate(
    name="simulated_payment_received_v1",
    simulated=True,
    subject=re.compile(r"\bPayment Received\b", _I),
    required_markers=(re.compile(r"You have received a payment", _I),),
    payment_code=re.compile(r"\bPayment ID\s*:\s*" + CODE),
    amount=re.compile(r"\bAmount\s*:\s*\+?" + AMOUNT + r"\s*" + ASSET),
    status=re.compile(r"\bStatus\s*:\s*(?P<status>[A-Za-z]+)"),
    status_map={
        "completed": PaymentStatus.PAID,
        "successful": PaymentStatus.PAID,
        "success": PaymentStatus.PAID,
        "pending": PaymentStatus.PENDING,
        "processing": PaymentStatus.PENDING,
        "failed": PaymentStatus.FAILED,
    },
    timestamp=re.compile(r"\bTime\s*:\s*" + TIMESTAMP),
)

SIMULATED_PAYMENT_RECEIVED_V2 = BinanceEmailTemplate(
    name="simulated_payment_received_v2",
    simulated=True,
    subject=re.compile(r"\bBinance Pay\b.*\breceived\b", _I),
    required_markers=(re.compile(r"Binance Pay transfer received", _I),),
    payment_code=re.compile(r"\bOrder ID\s*:?\s*" + CODE),
    amount=re.compile(r"\bAmount\s*:?\s*\+?" + AMOUNT + r"\s*" + ASSET),
    status=re.compile(r"\bStatus\s*:?\s*(?P<status>[A-Za-z]+)"),
    status_map={
        "success": PaymentStatus.PAID,
        "successful": PaymentStatus.PAID,
        "completed": PaymentStatus.PAID,
        "pending": PaymentStatus.PENDING,
        "failed": PaymentStatus.FAILED,
        "refunded": PaymentStatus.REFUNDED,
    },
    timestamp=re.compile(r"\bDate\s*:?\s*" + TIMESTAMP),
)

# =========================================================================================
# REAL TEMPLATES — add templates derived from real Binance emails here (simulated=False).
# =========================================================================================

REAL_TEMPLATES: tuple[BinanceEmailTemplate, ...] = ()

ALL_TEMPLATES: tuple[BinanceEmailTemplate, ...] = (
    *REAL_TEMPLATES,
    SIMULATED_PAYMENT_RECEIVED_V1,
    SIMULATED_PAYMENT_RECEIVED_V2,
)


def select_templates(
    *, allow_simulated: bool, enabled_names: list[str] | None = None
) -> tuple[BinanceEmailTemplate, ...]:
    enabled = set(enabled_names or [])
    return tuple(
        t
        for t in ALL_TEMPLATES
        if (allow_simulated or not t.simulated) and (not enabled or t.name in enabled)
    )
