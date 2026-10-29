"""Conversões de fuso horário. O banco guarda UTC sem tzinfo; a interface usa o fuso local."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def local_to_utc(dt: datetime) -> datetime:
    """Datas sem fuso são interpretadas no fuso configurado (TIMEZONE)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(tz())


def iso_utc(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None