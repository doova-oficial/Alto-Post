"""Carregamento da configuração de templates e validação dos parâmetros de arte."""
from __future__ import annotations

import copy
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

TEMPLATES_FILE = Path(__file__).resolve().parent.parent / "templates" / "templates.yaml"
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class TemplateParams(BaseModel):
    """Parâmetros completos (já resolvidos) de uma arte."""

    model_config = ConfigDict(extra="forbid")

    template: str
    title: str = Field(min_length=1, max_length=160)
    subtitle: str | None = Field(default=None, max_length=240)
    body: str | None = Field(default=None, max_length=600)
    items: list[str] = Field(default_factory=list, max_length=4)
    cta: str | None = Field(default=None, max_length=60)
    tag: str | None = Field(default=None, max_length=48)

    primary_color: str = "#FF00D0"
    accent_color: str = "#FF0000"
    text_color: str = "auto"
    background: Literal["gradient", "solid", "light", "dark"] = "gradient"

    title_position: Literal["top", "center", "bottom"] = "center"
    subtitle_position: Literal["above_title", "below_title"] = "below_title"
    text_align: Literal["left", "center"] = "left"
    logo_position: Literal["top_left", "top_right"] = "top_left"
    cta_align: Literal["left", "center"] = "left"

    title_size: int = Field(default=96, ge=40, le=180)
    subtitle_size: int = Field(default=44, ge=24, le=90)
    body_size: int = Field(default=34, ge=22, le=70)
    cta_size: int = Field(default=34, ge=22, le=60)
    tag_size: int = Field(default=28, ge=20, le=48)
    margin: int = Field(default=84, ge=48, le=160)

    shapes: bool = True
    show_logo: bool = True
    show_location: bool = True
    app_ui: bool = True   # elementos gráficos do app (chips, stories, ícones, pins, navegação...)
    item_style: Literal["cards", "numbered", "checks"] = "checks"
    # foto (somente nos modelos com requires_photo); o crédito é preenchido pelo sistema
    photo_id: str | None = Field(default=None, max_length=40)
    photo_credit: str | None = Field(default=None, max_length=160)
    photo_focus_y: float = Field(default=0.5, ge=0.0, le=1.0)  # 0 = topo da foto, 1 = base

    @field_validator("items")
    @classmethod
    def _items(cls, v: list[str]) -> list[str]:
        out = [i.strip() for i in v if i and i.strip()]
        for i in out:
            if len(i) > 110:
                raise ValueError("cada item deve ter no máximo 110 caracteres")
        return out

    @field_validator("title", "subtitle", "body", "cta", "tag")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX_RE.match(v):
            raise ValueError(f"cor inválida: {v!r} (use #RRGGBB ou um nome da paleta)")
        return v.upper()

    @field_validator("text_color")
    @classmethod
    def _text_color(cls, v: str) -> str:
        if v != "auto" and not HEX_RE.match(v):
            raise ValueError("text_color deve ser 'auto' ou #RRGGBB")
        return v.upper() if v != "auto" else v

    def text_amount(self) -> str:
        n = len(self.title) + len(self.subtitle or "") + len(self.body or "") + sum(len(i) for i in self.items)
        return "curto" if n <= 90 else ("medio" if n <= 220 else "longo")


class TemplateConfig:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.brand: dict[str, Any] = data["brand"]
        self.defaults: dict[str, Any] = data.get("defaults", {})
        self.templates: dict[str, Any] = data["templates"]

    @property
    def palette(self) -> dict[str, str]:
        return {k: v.upper() for k, v in self.brand["colors"].items()}

    @property
    def canvas(self) -> tuple[int, int]:
        c = self.brand.get("canvas", {})
        return int(c.get("width", 1080)), int(c.get("height", 1350))

    def color(self, value: str) -> str:
        if value in ("auto",):
            return value
        if isinstance(value, str) and value.lower() in self.palette:
            return self.palette[value.lower()]
        return value

    def keys(self) -> list[str]:
        return list(self.templates)

    def requires_photo(self, template_key: str) -> bool:
        return bool(self.templates.get(template_key, {}).get("requires_photo"))

    def resolve(self, template_key: str, overrides: dict[str, Any] | None = None) -> TemplateParams:
        if template_key not in self.templates:
            raise KeyError(f"template desconhecido: {template_key}")
        tpl = self.templates[template_key]
        merged: dict[str, Any] = copy.deepcopy(self.defaults)
        merged.update(copy.deepcopy(tpl.get("defaults", {})))
        if "item_style" in tpl:
            merged.setdefault("item_style", tpl["item_style"])
        for k, v in (overrides or {}).items():
            if v is not None or k in ("subtitle", "body", "cta", "tag"):
                merged[k] = v
        merged["template"] = template_key
        for ck in ("primary_color", "accent_color", "text_color"):
            if ck in merged and merged[ck] is not None:
                merged[ck] = self.color(merged[ck])
        return TemplateParams(**merged)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"key": k, "name": v.get("name"), "description": v.get("description"),
             "message_type": v.get("message_type"), "requires_photo": bool(v.get("requires_photo")),
             "defaults": v.get("defaults", {})}
            for k, v in self.templates.items()
        ]


def load_template_config(path: Path | None = None) -> TemplateConfig:
    with open(path or TEMPLATES_FILE, encoding="utf-8") as f:
        return TemplateConfig(yaml.safe_load(f))


@lru_cache
def get_template_config() -> TemplateConfig:
    return load_template_config()


# ----------------------------------------------------------------- cores
def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def best_text_color(backgrounds: list[tuple[int, int, int]], light: tuple[int, int, int],
                    dark: tuple[int, int, int]) -> tuple[int, int, int]:
    """Escolhe a cor com maior contraste mínimo contra todos os fundos informados."""
    cl = min(contrast_ratio(light, b) for b in backgrounds)
    cd = min(contrast_ratio(dark, b) for b in backgrounds)
    return light if cl >= cd else dark