"""Authenticated encryption (AES-256-GCM) for credentials stored in PostgreSQL.

Envelope format (ASCII, safe for a TEXT column)::

    v1.<key_id>.<base64url(nonce || ciphertext || tag)>

* ``key_id`` is the first 8 hex chars of SHA-256(key) so that keys can be rotated:
  new data is always encrypted with ``CREDENTIALS_ENCRYPTION_KEY``; data encrypted with a
  key listed in ``CREDENTIALS_ENCRYPTION_PREVIOUS_KEYS`` can still be decrypted.
* An *associated data* string binds every ciphertext to its context (e.g. the mail
  account it belongs to), so a ciphertext copied to another row fails to decrypt.

The master key exists only in the environment; the database only ever sees ciphertext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Sequence
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.exceptions import ConfigurationError, EncryptionError

_VERSION = "v1"
_NONCE_BYTES = 12


def _decode_key(raw: str) -> bytes:
    raw = raw.strip()
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            key = decoder(raw + "=" * (-len(raw) % 4))
        except binascii.Error, ValueError:
            continue
        if len(key) == 32:
            return key
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) == 32:
        return key
    raise ConfigurationError(
        "CREDENTIALS_ENCRYPTION_KEY must be 32 random bytes encoded as base64 or hex"
    )


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def generate_key() -> str:
    """Return a new random key, base64url encoded (use for CREDENTIALS_ENCRYPTION_KEY)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class CredentialCipher:
    def __init__(self, primary_key: str, previous_keys: Sequence[str] = ()) -> None:
        primary = _decode_key(primary_key)
        self._primary_id = _key_id(primary)
        self._keys: dict[str, AESGCM] = {self._primary_id: AESGCM(primary)}
        for raw in previous_keys:
            key = _decode_key(raw)
            self._keys.setdefault(_key_id(key), AESGCM(key))
        self._primary_raw = primary

    def encrypt(self, plaintext: bytes, associated_data: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ct = self._keys[self._primary_id].encrypt(nonce, plaintext, associated_data.encode())
        payload = base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")
        return f"{_VERSION}.{self._primary_id}.{payload}"

    def decrypt(self, token: str, associated_data: str) -> bytes:
        try:
            version, key_id, payload = token.split(".", 2)
        except ValueError as exc:
            raise EncryptionError("Malformed encrypted value") from exc
        if version != _VERSION:
            raise EncryptionError("Unsupported encryption version")
        aead = self._keys.get(key_id)
        if aead is None:
            raise EncryptionError("Encrypted with an unknown key")
        try:
            blob = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            return aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], associated_data.encode())
        except (InvalidTag, ValueError, binascii.Error) as exc:
            # Never include the token or plaintext in the error.
            raise EncryptionError("Could not decrypt value (wrong key or tampered data)") from exc

    def encrypt_json(self, data: dict[str, Any], associated_data: str) -> str:
        return self.encrypt(json.dumps(data, separators=(",", ":")).encode(), associated_data)

    def decrypt_json(self, token: str, associated_data: str) -> dict[str, Any]:
        value = json.loads(self.decrypt(token, associated_data))
        if not isinstance(value, dict):
            raise EncryptionError("Decrypted value is not an object")
        return value

    def derive_key(self, purpose: str) -> bytes:
        """Derive an independent sub-key (HKDF-SHA256) for another purpose."""
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose.encode()).derive(
            self._primary_raw
        )

    def self_test(self) -> bool:
        probe = self.encrypt(b"probe", "self-test")
        return self.decrypt(probe, "self-test") == b"probe"


def build_cipher(primary_key: str | None, previous_keys: Sequence[str] = ()) -> CredentialCipher:
    if not primary_key:
        raise ConfigurationError("CREDENTIALS_ENCRYPTION_KEY is not configured")
    return CredentialCipher(primary_key, previous_keys)
