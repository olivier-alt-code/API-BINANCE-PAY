from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr, ValidationError

from app.config import GmailAuthMethod
from app.core.encryption import CredentialCipher, generate_key
from app.core.exceptions import ConfigurationError, EncryptionError
from app.core.logging import REDACTED, JsonFormatter, RedactionFilter, mask_email
from tests.conftest import make_settings

# --- Encryption -------------------------------------------------------------------------


def test_encrypt_roundtrip_and_ciphertext_hides_plaintext() -> None:
    cipher = CredentialCipher(generate_key())
    token = cipher.encrypt(b"refresh-token-value", "mail_account:a@b.c:oauth2")
    assert b"refresh-token-value" not in token.encode()
    assert token.startswith("v1.")
    assert cipher.decrypt(token, "mail_account:a@b.c:oauth2") == b"refresh-token-value"


def test_nonce_is_random() -> None:
    cipher = CredentialCipher(generate_key())
    assert cipher.encrypt(b"x", "ctx") != cipher.encrypt(b"x", "ctx")


def test_tampering_wrong_key_and_wrong_context_fail() -> None:
    cipher = CredentialCipher(generate_key())
    token = cipher.encrypt(b"secret", "ctx")
    with pytest.raises(EncryptionError):
        cipher.decrypt(token, "other-ctx")  # ciphertext moved to another row
    with pytest.raises(EncryptionError):
        CredentialCipher(generate_key()).decrypt(token, "ctx")
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(EncryptionError):
        cipher.decrypt(tampered, "ctx")


def test_key_rotation_with_previous_keys() -> None:
    old = generate_key()
    token = CredentialCipher(old).encrypt(b"v", "ctx")
    rotated = CredentialCipher(generate_key(), previous_keys=[old])
    assert rotated.decrypt(token, "ctx") == b"v"


def test_invalid_master_key_rejected() -> None:
    with pytest.raises(ConfigurationError):
        CredentialCipher("too-short")


def test_encryption_errors_never_contain_secrets() -> None:
    cipher = CredentialCipher(generate_key())
    token = cipher.encrypt(b"super-secret-value", "ctx")
    with pytest.raises(EncryptionError) as info:
        cipher.decrypt(token, "bad")
    assert "super-secret-value" not in str(info.value)
    assert token not in str(info.value)


# --- Config ----------------------------------------------------------------------------


def test_normal_google_password_is_rejected_as_app_password() -> None:
    with pytest.raises(ValidationError, match="App Password"):
        make_settings(gmail_app_password=SecretStr("MyNormalPassw0rd!"))


def test_app_password_spaces_are_normalized() -> None:
    s = make_settings(gmail_app_password=SecretStr("abcd efgh ijkl mnop"))
    assert s.gmail_app_password is not None
    assert s.gmail_app_password.get_secret_value() == "abcdefghijklmnop"


def test_account_password_method_is_disabled_by_default() -> None:
    with pytest.raises(ValidationError, match="disabled"):
        make_settings(gmail_auth_method=GmailAuthMethod.ACCOUNT_PASSWORD)
    s = make_settings(
        gmail_auth_method=GmailAuthMethod.ACCOUNT_PASSWORD,
        gmail_account_password_auth_enabled=True,
    )
    assert s.gmail_auth_method is GmailAuthMethod.ACCOUNT_PASSWORD


def test_short_api_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_keys=[SecretStr("short")])


def test_settings_repr_does_not_leak_secrets() -> None:
    s = make_settings()
    assert "abcdefghijklmnop" not in repr(s)
    assert "abcdefghijklmnop" not in str(s.model_dump())


def test_simulated_templates_off_in_production_by_default() -> None:
    s = make_settings(app_env="production", binance_allow_simulated_templates=None)
    assert not s.simulated_templates_allowed
    assert not s.docs_enabled


def test_float_tolerance_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(payment_amount_tolerance="-1")


# --- Logging redaction -----------------------------------------------------------------


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_redaction_of_configured_secrets_and_sensitive_keys() -> None:
    secret = "abcdefghijklmnop"
    f = RedactionFilter([secret])
    record = _record(
        f"login failed for app password {secret}",
        password="x" * 20,
        refresh_token="tok",
        account="me@gmail.com",
    )
    f.filter(record)
    rendered = JsonFormatter().format(record)
    assert secret not in rendered
    assert '"password": "' + REDACTED in rendered
    assert '"refresh_token": "' + REDACTED in rendered
    assert "me@gmail.com" in rendered


def test_redaction_of_bearer_and_xoauth_and_tracebacks() -> None:
    f = RedactionFilter(["my-secret-api-key-value"])
    try:
        raise RuntimeError("boom my-secret-api-key-value")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", (), sys.exc_info())
    f.filter(record)
    assert record.exc_text is not None and "my-secret-api-key-value" not in record.exc_text
    r2 = _record("Authorization: Bearer eyJhbGciOi.abc user=a@b\x01auth=Bearer ya29.tok\x01")
    f.filter(r2)
    assert "eyJhbGciOi" not in r2.getMessage() and "ya29.tok" not in r2.getMessage()


def test_mask_email() -> None:
    assert mask_email("john.doe@gmail.com") == "jo******@gmail.com"
    assert mask_email("ab@x.com") == "a***@x.com"


def test_env_example_is_loadable() -> None:
    from pathlib import Path

    from app.config import Settings

    s = Settings(_env_file=Path(__file__).parents[2] / ".env.example")  # type: ignore[call-arg]
    assert s.gmail_app_password is None and s.cors_allowed_origins == []
    assert s.email_trusted_authserv_ids == ["mx.google.com"]
