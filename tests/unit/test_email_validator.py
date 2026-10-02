from __future__ import annotations

import pytest

from app.integrations.binance.email_parser import parse_mime
from app.integrations.binance.email_validator import (
    EmailTrustValidator,
    TrustPolicy,
    parse_authentication_results,
)
from tests.conftest import fixture_bytes, make_settings
from tests.emails import auth_results, make_email


@pytest.fixture
def validator() -> EmailTrustValidator:
    return EmailTrustValidator(TrustPolicy.from_settings(make_settings()))


def test_valid_binance_email_is_trusted(validator: EmailTrustValidator) -> None:
    result = validator.validate(parse_mime(fixture_bytes("payment_received_v1.eml")))
    assert result.trusted
    assert result.sender_valid and result.message_id_valid
    assert result.dkim_pass and result.spf_pass and result.dmarc_pass
    assert result.reasons == ()


def test_display_name_spoof_is_rejected(validator: EmailTrustValidator) -> None:
    result = validator.validate(parse_mime(fixture_bytes("spoofed_display_name.eml")))
    assert not result.trusted
    assert not result.sender_valid
    assert "sender_not_allowed" in result.reasons


def test_spoofed_sender_with_failing_dkim_is_rejected(validator: EmailTrustValidator) -> None:
    result = validator.validate(parse_mime(fixture_bytes("spoofed_dkim_fail.eml")))
    assert result.sender_valid  # the From address *looks* right...
    assert not result.trusted  # ...but authentication failed
    assert not result.dkim_pass and not result.spf_pass and not result.dmarc_pass


def test_forged_lower_authentication_results_are_ignored(validator: EmailTrustValidator) -> None:
    result = validator.validate(parse_mime(fixture_bytes("forged_auth_results.eml")))
    assert not result.trusted
    assert not result.dkim_pass


def test_authentication_results_from_untrusted_server_is_ignored(
    validator: EmailTrustValidator,
) -> None:
    raw = make_email(include_auth=False, extra_auth_headers=[auth_results(authserv="evil.test")])
    result = validator.validate(parse_mime(raw))
    assert not result.trusted
    assert "untrusted_authserv_id" in result.reasons


def test_missing_authentication_results_is_untrusted(validator: EmailTrustValidator) -> None:
    result = validator.validate(parse_mime(make_email(include_auth=False)))
    assert not result.trusted
    assert not result.authentication_results_present


def test_dkim_must_be_aligned_with_from_domain(validator: EmailTrustValidator) -> None:
    # DKIM passes, but for an attacker's domain.
    raw = make_email(dkim_domain="attacker.test")
    result = validator.validate(parse_mime(raw))
    assert not result.dkim_pass
    assert not result.trusted


def test_lookalike_and_subdomain_senders() -> None:
    policy = TrustPolicy.from_settings(
        make_settings(
            binance_allowed_domains=["binance.example", "*.mail.binance.example"],
            binance_allowed_senders=["exact@other.example"],
        )
    )
    assert policy.sender_allowed("x@binance.example")
    assert policy.sender_allowed("x@a.mail.binance.example")
    assert policy.sender_allowed("exact@other.example")
    assert not policy.sender_allowed("x@sub.binance.example")  # subdomain not opted-in
    assert not policy.sender_allowed("x@binance.example.attacker.test")
    assert not policy.sender_allowed("x@evilbinance.example")
    assert not policy.sender_allowed("other@other.example")
    assert not policy.sender_allowed("x@bіnance.example")  # Cyrillic 'і'
    assert not policy.sender_allowed(None)


def test_no_configured_senders_trusts_nothing() -> None:
    policy = TrustPolicy.from_settings(
        make_settings(binance_allowed_domains=[], binance_allowed_senders=[])
    )
    result = EmailTrustValidator(policy).validate(
        parse_mime(fixture_bytes("payment_received_v1.eml"))
    )
    assert not result.trusted


def test_invalid_message_id_is_untrusted(validator: EmailTrustValidator) -> None:
    raw = make_email(message_id="not-a-message-id")
    result = validator.validate(parse_mime(raw))
    assert not result.message_id_valid and not result.trusted


def test_policy_is_configurable() -> None:
    relaxed = TrustPolicy.from_settings(make_settings(email_require_spf=False))
    raw = make_email(spf="softfail")
    assert EmailTrustValidator(relaxed).validate(parse_mime(raw)).trusted
    strict = TrustPolicy.from_settings(make_settings())
    assert not EmailTrustValidator(strict).validate(parse_mime(raw)).trusted


def test_parse_authentication_results_handles_comments() -> None:
    authserv, results = parse_authentication_results(
        "mx.google.com; dkim=pass (2048-bit key; secure) header.i=@binance.example "
        'header.s=s1; spf=neutral (google.com: x; y) smtp.mailfrom="a@b.example"; '
        "dmarc=pass (p=REJECT) header.from=binance.example"
    )
    assert authserv == "mx.google.com"
    methods = {(r.method, r.result) for r in results}
    assert methods == {("dkim", "pass"), ("spf", "neutral"), ("dmarc", "pass")}
    dkim = next(r for r in results if r.method == "dkim")
    assert dkim.properties["header.i"] == "@binance.example"
