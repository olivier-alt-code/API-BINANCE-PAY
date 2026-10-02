from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.exceptions import BinanceEmailParseError
from app.integrations.binance.email_parser import BinanceEmailParser, html_to_text, parse_mime
from app.integrations.binance.templates import ALL_TEMPLATES, PaymentStatus, select_templates
from tests.conftest import fixture_bytes
from tests.emails import make_email

WHEN = datetime(2026, 10, 1, 20, 15, 31, tzinfo=UTC)


@pytest.fixture
def parser() -> BinanceEmailParser:
    return BinanceEmailParser(select_templates(allow_simulated=True))


def test_plain_text_email(parser: BinanceEmailParser) -> None:
    p = parser.parse_bytes(fixture_bytes("payment_received_v1.eml"))
    assert p.payment_code == "123456789ABC"
    assert p.amount == Decimal("25.50")
    assert p.asset == "USDT"
    assert p.payment_status is PaymentStatus.PAID
    assert p.received_at == WHEN
    assert p.sender == "notifications@binance.example"
    assert p.message_id == "<123456789ABC.1790885731@binance.example>"
    assert p.template == "simulated_payment_received_v1"


def test_multipart_email_text_and_html_agree(parser: BinanceEmailParser) -> None:
    p = parser.parse_bytes(fixture_bytes("payment_received_v2.eml"))
    assert (p.payment_code, p.amount, p.asset) == ("PAY-82919381", Decimal(25), "USDT")
    assert p.template == "simulated_payment_received_v2"


def test_html_only_email_with_entities_and_thousands(parser: BinanceEmailParser) -> None:
    p = parser.parse_bytes(fixture_bytes("payment_received_v2_html_only.eml"))
    assert p.payment_code == "PAY-82919381"
    assert p.amount == Decimal("1250.00")
    assert p.payment_status is PaymentStatus.PAID


def test_html_to_text_strips_scripts_styles_and_decodes_entities() -> None:
    text = html_to_text(
        "<html><head><style>.x{}</style><script>alert('Order ID: EVIL')</script></head>"
        "<body><p>Order&nbsp;ID:</p><td>A&amp;B &quot;q&quot;</td>\n\n  <b>X</b>Y</body></html>"
    )
    assert "EVIL" not in text and "alert" not in text and ".x" not in text
    assert text == 'Order ID: A&B "q" XY'


def test_html_inline_tags_do_not_split_codes(parser: BinanceEmailParser) -> None:
    raw = make_email(template="v2_html_only", code="PAY-82919381", when=WHEN)
    raw = raw.replace(b"<b>PAY-82919381</b>", b"<b>PAY-</b><span>82919381</span>")
    assert parser.parse_bytes(raw).payment_code == "PAY-82919381"


def test_script_content_is_never_extracted(parser: BinanceEmailParser) -> None:
    # The v2 fixture contains <script>document.write('Order ID: FAKE0000')</script>.
    p = parser.parse_bytes(fixture_bytes("payment_received_v2.eml"))
    assert p.payment_code != "FAKE0000"


@pytest.mark.parametrize(
    ("raw_amount", "expected"),
    [
        ("25", Decimal(25)),
        ("25.5", Decimal("25.5")),
        ("25.50", Decimal("25.50")),
        ("0.00000001", Decimal("0.00000001")),
        ("1,234,567.891", Decimal("1234567.891")),
    ],
)
def test_amount_decimal_precision(
    parser: BinanceEmailParser, raw_amount: str, expected: Decimal
) -> None:
    p = parser.parse_bytes(make_email(amount=raw_amount, when=WHEN))
    assert isinstance(p.amount, Decimal)
    assert p.amount == expected


def test_decimal_values_with_different_scales_are_equal(parser: BinanceEmailParser) -> None:
    p = parser.parse_bytes(make_email(amount="25.500", when=WHEN))
    assert p.amount == Decimal("25.5") == Decimal("25.50") == Decimal("25.5000")


@pytest.mark.parametrize("bad_amount", ["25,50", "1,23", "-5", "0", "0.00", "abc"])
def test_ambiguous_or_invalid_amounts_are_rejected(
    parser: BinanceEmailParser, bad_amount: str
) -> None:
    with pytest.raises(BinanceEmailParseError):
        parser.parse_bytes(make_email(amount=bad_amount, when=WHEN))


def test_code_is_extracted_whole_never_a_prefix(parser: BinanceEmailParser) -> None:
    p = parser.parse_bytes(make_email(code="ABC1234567", when=WHEN))
    assert p.payment_code == "ABC1234567"


def test_code_case_preserved_unless_configured(parser: BinanceEmailParser) -> None:
    raw = make_email(code="abc123XYZ", when=WHEN)
    assert parser.parse_bytes(raw).payment_code == "abc123XYZ"
    ci = BinanceEmailParser(select_templates(allow_simulated=True), case_insensitive_code=True)
    assert ci.parse_bytes(raw).payment_code == "ABC123XYZ"


def test_conflicting_values_in_one_email_are_rejected(parser: BinanceEmailParser) -> None:
    raw = make_email(code="AAAA1111", when=WHEN).replace(
        b"Status: Completed", b"Status: Completed\nPayment ID: BBBB2222"
    )
    with pytest.raises(BinanceEmailParseError, match="ambiguous"):
        parser.parse_bytes(raw)


def test_text_and_html_disagreeing_is_rejected(parser: BinanceEmailParser) -> None:
    raw = make_email(template="v2", code="PAY-11112222", when=WHEN)
    raw = raw.replace(b"<b>PAY-11112222</b>", b"<b>PAY-99998888</b>")
    with pytest.raises(BinanceEmailParseError, match="disagree"):
        parser.parse_bytes(raw)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Completed", PaymentStatus.PAID),
        ("Pending", PaymentStatus.PENDING),
        ("Failed", PaymentStatus.FAILED),
    ],
)
def test_status_mapping(parser: BinanceEmailParser, status: str, expected: PaymentStatus) -> None:
    assert parser.parse_bytes(make_email(status=status, when=WHEN)).payment_status is expected


def test_unknown_status_is_rejected(parser: BinanceEmailParser) -> None:
    with pytest.raises(BinanceEmailParseError, match="status"):
        parser.parse_bytes(make_email(status="Weird", when=WHEN))


def test_non_binance_content_does_not_match(parser: BinanceEmailParser) -> None:
    raw = make_email(when=WHEN).replace(b"You have received a payment", b"Weekly newsletter")
    with pytest.raises(BinanceEmailParseError, match="No Binance template"):
        parser.parse_bytes(raw)


def test_simulated_templates_can_be_disabled() -> None:
    assert all(t.simulated for t in ALL_TEMPLATES)  # until a real template is added
    strict = BinanceEmailParser(select_templates(allow_simulated=False))
    with pytest.raises(BinanceEmailParseError):
        strict.parse_bytes(fixture_bytes("payment_received_v1.eml"))


def test_parser_is_independent_of_imap(parser: BinanceEmailParser) -> None:
    # Works on an already-parsed MIME object, no mailbox involved.
    message = parse_mime(fixture_bytes("payment_received_v1.eml"))
    assert parser.parse(message).payment_code == "123456789ABC"
