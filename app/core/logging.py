"""Structured (JSON) logging with secret redaction.

Every log record passes through :class:`RedactionFilter`, which removes:

* any configured secret value (API keys, App Password, OAuth secrets, DB password...);
* values of well-known sensitive keys passed through ``extra=``;
* bearer tokens / XOAUTH2 strings that could appear in third-party messages.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|authorization|api[_-]?key|credential|cookie|"
    r"refresh|access|raw|body|xoauth)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]+")
_XOAUTH_RE = re.compile(r"(?i)auth=Bearer\s+\S+")

_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime"}
)


class RedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for secret in secrets:
            if secret and len(secret) >= 6 and secret not in self._secrets:
                self._secrets.append(secret)
        # Longest first so that overlapping secrets are fully removed.
        self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        text = _XOAUTH_RE.sub("auth=Bearer " + REDACTED, text)
        return _BEARER_RE.sub(r"\1" + REDACTED, text)

    def _redact_value(self, key: str, value: Any) -> Any:
        if _SENSITIVE_KEYS.search(key):
            return REDACTED
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self._redact_value(str(k), v) for k, v in value.items()}
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self.redact(message)
        record.args = None
        for key, value in list(vars(record).items()):
            if key not in _STANDARD_ATTRS:
                setattr(record, key, self._redact_value(key, value))
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now and redact it, so secrets in exception
            # messages never reach the handler.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self.redact(formatted)
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_") and key != "color_message":
                payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str, ensure_ascii=False)


_redaction_filter = RedactionFilter()


def get_redaction_filter() -> RedactionFilter:
    return _redaction_filter


def configure_logging(
    level: str = "INFO", json_output: bool = True, secrets: Iterable[str] = ()
) -> None:
    _redaction_filter.add_secrets(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_redaction_filter)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # imaplib debug output could include credentials; keep it silent.
    for noisy in ("imaplib", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def mask_email(email: str | None) -> str | None:
    """``john.doe@gmail.com`` -> ``jo******@gmail.com``."""
    if not email or "@" not in email:
        return email
    local, _, domain = email.partition("@")
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(len(local) - len(visible), 3)}@{domain}"
