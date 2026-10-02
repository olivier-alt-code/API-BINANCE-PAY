"""Builders for SIMULATED Binance emails used by tests.

These emails follow the *simulated* templates in ``app/integrations/binance/templates.py``.
They are NOT real Binance emails. Sender domain ``binance.example`` is a reserved,
non-existent domain (RFC 2606) used on purpose so no real Binance address is hardcoded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

SENDER = "notifications@binance.example"
DOMAIN = "binance.example"


def auth_results(
    *,
    dkim: str = "pass",
    spf: str = "pass",
    dmarc: str = "pass",
    domain: str = DOMAIN,
    authserv: str = "mx.google.com",
) -> str:
    return (
        f"{authserv}; dkim={dkim} header.i=@{domain} header.s=s1 header.b=AbCdEf; "
        f"spf={spf} (google.com: domain of bounce@{domain} designates 192.0.2.1 as "
        f"permitted sender) smtp.mailfrom=bounce@{domain}; "
        f"dmarc={dmarc} (p=REJECT sp=REJECT dis=NONE) header.from={domain}"
    )


def make_email(
    *,
    code: str = "123456789ABC",
    amount: str = "25.50",
    asset: str = "USDT",
    status: str = "Completed",
    when: datetime | None = None,
    template: str = "v1",
    sender: str = SENDER,
    display_name: str = "Binance",
    dkim: str = "pass",
    spf: str = "pass",
    dmarc: str = "pass",
    dkim_domain: str | None = None,
    message_id: str | None = None,
    extra_auth_headers: list[str] | None = None,
    include_auth: bool = True,
) -> bytes:
    when = when or datetime.now(UTC).replace(microsecond=0)
    ts = when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    msg = EmailMessage()
    if include_auth:
        msg["Authentication-Results"] = auth_results(
            dkim=dkim, spf=spf, dmarc=dmarc, domain=dkim_domain or sender.rpartition("@")[2]
        )
    for header in extra_auth_headers or []:
        msg["Authentication-Results"] = header
    msg["X-Fixture-Notice"] = "SIMULATED - not a real Binance email"
    msg["From"] = f"{display_name} <{sender}>"
    msg["To"] = "merchant@gmail.com"
    msg["Date"] = format_datetime(when.astimezone(UTC))
    msg["Message-ID"] = message_id or f"<{code}.{int(when.timestamp())}@{DOMAIN}>"

    if template == "v1":
        msg["Subject"] = f"[Binance] Payment Received - {ts}(UTC)"
        msg.set_content(
            "Dear user,\n\n"
            "You have received a payment via Binance Pay.\n\n"
            f"Payment ID: {code}\n"
            f"Amount: {amount} {asset}\n"
            f"Status: {status}\n"
            f"Time: {ts} (UTC)\n\n"
            "This is an automated message, please do not reply.\n"
        )
    elif template in ("v2", "v2_html_only"):
        msg["Subject"] = f"Binance Pay - You received {amount} {asset}"
        html = (
            "<html><head><style>td{color:red}</style>"
            "<script>document.write('Order ID: FAKE0000')</script></head><body>"
            "<h2>Binance Pay transfer received</h2><table>"
            f"<tr><td>Order&nbsp;ID</td><td><b>{code}</b></td></tr>"
            f"<tr><td>Amount</td><td>+{amount}&#160;{asset}</td></tr>"
            f"<tr><td>Status</td><td>{status}</td></tr>"
            f"<tr><td>Date</td><td>{ts} (UTC)</td></tr>"
            "</table><p>Questions? Visit our &quot;Support&quot; &amp; FAQ.</p>"
            "</body></html>"
        )
        if template == "v2":
            msg.set_content(
                "Binance Pay transfer received\n"
                f"Order ID: {code}\nAmount: +{amount} {asset}\nStatus: {status}\n"
                f"Date: {ts} (UTC)\n"
            )
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content(html, subtype="html", cte="quoted-printable")
    else:  # pragma: no cover
        raise ValueError(template)
    return bytes(msg)
