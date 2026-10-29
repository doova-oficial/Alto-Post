"""Biblioteca de temas, plano de lançamento e calendário."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
TEXT_FIELDS = ("tag", "title", "subtitle", "body", "items", "cta")


@lru_cache
def load_themes() -> dict[str, Any]:
    with open(CONTENT_DIR / "themes.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def themes() -> dict[str, dict[str, Any]]:
    return load_themes()["themes"]


def get_theme(key: str) -> dict[str, Any]:
    t = themes().get(key)
    if t is None:
        raise KeyError(f"tema desconhecido: {key}")
    return t


def theme_overrides(theme: dict[str, Any]) -> dict[str, Any]:
    """Textos do tema que viram parâmetros da arte."""
    out = {k: theme[k] for k in TEXT_FIELDS if k in theme}
    out.setdefault("items", [])
    return out


def launch_plan() -> list[tuple[str, dict[str, Any]]]:
    items = [(k, t) for k, t in themes().items() if t.get("launch_order")]
    return sorted(items, key=lambda kv: kv[1]["launch_order"])


@lru_cache
def load_dates() -> dict[str, Any]:
    with open(CONTENT_DIR / "dates.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_rule(rule: str, year: int) -> date:
    if rule.startswith("fixed:"):
        m, d = rule.split(":", 1)[1].split("-")
        return date(year, int(m), int(d))
    if rule.startswith("nth_weekday:"):
        _, month, weekday, n = rule.split(":")
        month, weekday, n = int(month), int(weekday), int(n)
        days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == weekday]
        return date(year, month, days[n - 1])
    if rule == "black_friday":
        thursdays = [d for d in range(1, 31) if date(year, 11, d).weekday() == 3]
        return date(year, 11, thursdays[3]) + timedelta(days=1)
    raise ValueError(f"regra de data inválida: {rule}")


def upcoming_dates(today: date, days: int = 45) -> list[dict[str, Any]]:
    data = load_dates()
    out = []
    for entry in (data.get("national") or []) + (data.get("regional") or []):
        for y in (today.year, today.year + 1):
            d = resolve_rule(entry["rule"], y)
            if today <= d <= today + timedelta(days=days):
                out.append({**entry, "date": d.isoformat(), "days_until": (d - today).days})
    return sorted(out, key=lambda e: e["days_until"])


def faq() -> list[str]:
    return list(load_themes().get("faq") or [])


def trend_keywords() -> list[str]:
    return [k.lower() for k in load_themes().get("trend_keywords") or []]