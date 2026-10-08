"""Small helpers shared by the views."""

from __future__ import annotations

from datetime import date

from flask import redirect, request
from werkzeug.wrappers import Response


def back(default: str) -> Response:
    """Redirect to the form's ``next`` (same-site paths only) or to ``default``."""
    target = request.form.get("next") or request.args.get("next") or ""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        target = default
    return redirect(target)


def clean_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def clean_tags(value: str | None) -> str:
    """'Casa, gym ,casa' -> 'casa,gym' (lowercase, unique, ordered)."""
    seen: list[str] = []
    for raw in (value or "").replace("#", " ").replace(";", ",").split(","):
        for tag in raw.split():
            tag = tag.strip().lower()[:30]
            if tag and tag not in seen:
                seen.append(tag)
    return ",".join(seen[:10])


def text(field: str, limit: int) -> str:
    return (request.form.get(field) or "").strip()[:limit]


def checked(field: str) -> int:
    return 1 if request.form.get(field) in ("1", "on", "true") else 0
