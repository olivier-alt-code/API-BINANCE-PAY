"""Authenticity checks for (supposed) Binance emails.

We never trust an email because its subject or display name says "Binance". A message is
*trusted* only when, according to the configured :class:`TrustPolicy`:

* the ``From`` header holds exactly one ASCII address that is an allowed sender or
  belongs to an allowed domain (the display name is ignored);
* it has a syntactically valid ``Message-ID``;
* the TOPMOST ``Authentication-Results`` header was added by a trusted receiving server
  (``mx.google.com`` for Gmail). Lower headers can be forged by the sender, so they are
  ignored;
* DKIM passed for a domain aligned with the ``From`` domain, SPF passed and DMARC passed.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from typing import Any

from app.config import Settings
from app.integrations.binance.email_parser import sender_address

_MESSAGE_ID_RE = re.compile(r"^<[^<>\s@]+@[^<>\s@]+>$")
_RESINFO_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*([A-Za-z0-9_-]+)")
_PROP_RE = re.compile(r"([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\s*=\s*(\"[^\"]*\"|[^\s;]+)")


@dataclass(frozen=True)
class TrustPolicy:
    allowed_senders: frozenset[str]
    allowed_domains: frozenset[str]
    trusted_authserv_ids: frozenset[str]
    require_authentication_results: bool = True
    require_dkim: bool = True
    require_dkim_alignment: bool = True
    require_spf: bool = True
    require_dmarc: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> TrustPolicy:
        return cls(
            allowed_senders=frozenset(settings.binance_allowed_senders),
            allowed_domains=frozenset(settings.binance_allowed_domains),
            trusted_authserv_ids=frozenset(a.lower() for a in settings.email_trusted_authserv_ids),
            require_authentication_results=settings.email_require_authentication_results,
            require_dkim=settings.email_require_dkim,
            require_dkim_alignment=settings.email_require_dkim_alignment,
            require_spf=settings.email_require_spf,
            require_dmarc=settings.email_require_dmarc,
        )

    def sender_allowed(self, address: str | None) -> bool:
        if not address or not address.isascii() or address.count("@") != 1:
            return False
        address = address.lower()
        if address in self.allowed_senders:
            return True
        domain = address.rpartition("@")[2]
        for allowed in self.allowed_domains:
            if allowed.startswith("*."):
                # "*.binance.com" allows subdomains (e.g. mail.binance.com) explicitly.
                base = allowed[2:]
                if domain == base or domain.endswith("." + base):
                    return True
            elif domain == allowed:
                return True
        return False


@dataclass(frozen=True)
class AuthResult:
    method: str
    result: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailTrustResult:
    trusted: bool
    sender_valid: bool
    message_id_valid: bool
    authentication_results_present: bool
    dkim_pass: bool
    spf_pass: bool
    dmarc_pass: bool
    sender: str | None = None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = list(self.reasons)
        return data


def _strip_comments(value: str) -> str:
    out: list[str] = []
    depth = 0
    in_quote = False
    for ch in value:
        if ch == '"' and depth == 0:
            in_quote = not in_quote
        if not in_quote:
            if ch == "(":
                depth += 1
                continue
            if ch == ")" and depth:
                depth -= 1
                continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def parse_authentication_results(value: str) -> tuple[str, list[AuthResult]]:
    """Parse an RFC 8601 Authentication-Results header into (authserv_id, results)."""
    cleaned = _strip_comments(" ".join(value.split()))
    segments = [s.strip() for s in cleaned.split(";")]
    authserv_id = segments[0].split()[0].lower() if segments and segments[0] else ""
    results: list[AuthResult] = []
    for segment in segments[1:]:
        match = _RESINFO_RE.match(segment)
        if not match:
            continue
        props = {k.lower(): v.strip('"').lower() for k, v in _PROP_RE.findall(segment)}
        results.append(AuthResult(match.group(1).lower(), match.group(2).lower(), props))
    return authserv_id, results


def _domain_of(value: str) -> str:
    return value.rpartition("@")[2].strip().lower().rstrip(".")


def _aligned(a: str, b: str) -> bool:
    """Relaxed (organizational) alignment approximation: equal or one is a subdomain."""
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


class EmailTrustValidator:
    def __init__(self, policy: TrustPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> TrustPolicy:
        return self._policy

    def is_candidate_sender(self, message: EmailMessage) -> bool:
        """Cheap check: does the From address belong to an allowed Binance sender?"""
        return self._policy.sender_allowed(sender_address(message))

    def validate(self, message: EmailMessage) -> EmailTrustResult:
        reasons: list[str] = []
        policy = self._policy

        sender = sender_address(message)
        sender_valid = policy.sender_allowed(sender)
        if not (policy.allowed_senders or policy.allowed_domains):
            reasons.append("no_allowed_senders_configured")
        if not sender_valid:
            reasons.append("sender_not_allowed")
        sender_domain = _domain_of(sender or "")

        message_ids = message.get_all("Message-ID") or []
        message_id_valid = len(message_ids) == 1 and bool(
            _MESSAGE_ID_RE.fullmatch(str(message_ids[0]).strip())
        )
        if not message_id_valid:
            reasons.append("invalid_message_id")

        dkim_pass = spf_pass = dmarc_pass = False
        headers = message.get_all("Authentication-Results") or []
        ar_present = False
        if headers:
            # Only the topmost header is added by our own receiving server.
            authserv_id, results = parse_authentication_results(str(headers[0]))
            if authserv_id in policy.trusted_authserv_ids:
                ar_present = True
                dkim_pass = self._dkim_ok(results, sender_domain)
                spf_pass = any(r.method == "spf" and r.result == "pass" for r in results)
                dmarc_pass = any(
                    r.method == "dmarc"
                    and r.result == "pass"
                    and _aligned(r.properties.get("header.from", ""), sender_domain)
                    for r in results
                )
            else:
                reasons.append("untrusted_authserv_id")
        if not ar_present:
            reasons.append("authentication_results_missing")

        trusted = sender_valid and message_id_valid
        if policy.require_authentication_results and not ar_present:
            trusted = False
        if policy.require_dkim and not dkim_pass:
            trusted = False
            reasons.append("dkim_not_pass")
        if policy.require_spf and not spf_pass:
            trusted = False
            reasons.append("spf_not_pass")
        if policy.require_dmarc and not dmarc_pass:
            trusted = False
            reasons.append("dmarc_not_pass")

        return EmailTrustResult(
            trusted=trusted,
            sender_valid=sender_valid,
            message_id_valid=message_id_valid,
            authentication_results_present=ar_present,
            dkim_pass=dkim_pass,
            spf_pass=spf_pass,
            dmarc_pass=dmarc_pass,
            sender=sender,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _dkim_ok(self, results: list[AuthResult], sender_domain: str) -> bool:
        for r in results:
            if r.method != "dkim" or r.result != "pass":
                continue
            if not self._policy.require_dkim_alignment:
                return True
            signing = r.properties.get("header.d") or _domain_of(r.properties.get("header.i", ""))
            if _aligned(signing, sender_domain):
                return True
        return False
