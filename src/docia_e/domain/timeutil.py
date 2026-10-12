"""Instantes técnicos en UTC y fechas civiles sin convertirlas a medianoche."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Europe/Madrid")


def now_utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_civil_date(value: str | None, field: str) -> str | None:
    """Acepta YYYY-MM-DD o vacío. No inventa una hora."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} debe ser una fecha civil YYYY-MM-DD") from exc
    return parsed.isoformat()


def exclusive_end(declared_inclusive: str) -> str:
    """«Válido hasta el día D inclusive» se guarda como límite exclusivo D+1."""
    parsed = date.fromisoformat(declared_inclusive)
    return (parsed + timedelta(days=1)).isoformat()


def format_instant_madrid(value: str | None) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def is_iso_instant(value: str | None) -> bool:
    if not value:
        return False
    if not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
