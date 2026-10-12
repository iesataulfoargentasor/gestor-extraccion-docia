"""Protección de formularios y límite sencillo por proceso."""

from __future__ import annotations

import hmac
import secrets
import time

from flask import session

_BUCKETS: dict[str, list[float]] = {}


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_ok(submitted: str | None) -> bool:
    expected = session.get("csrf_token")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)


def allow(key: str, limit: int, window_seconds: int) -> bool:
    now = time.monotonic()
    recent = [moment for moment in _BUCKETS.get(key, []) if now - moment < window_seconds]
    if len(recent) >= limit:
        _BUCKETS[key] = recent
        return False
    recent.append(now)
    _BUCKETS[key] = recent
    return True
