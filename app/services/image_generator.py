"""Geração das artes do Doova com Pillow — sem IA generativa.

Cada template desenha fundo e elementos geométricos próprios e posiciona os
textos com um motor de layout comum que:

* mede o texto em pixels reais da fonte escolhida;
* quebra linhas e hifeniza palavras longas;
* reduz proporcionalmente os tamanhos até caber na área disponível;
* recusa a arte (``TextOverflowError``) se nem o tamanho mínimo legível couber,
  em vez de cortar texto importante.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageOps

from app.services.fonts import FontResolver, get_font_resolver
from app.services.template_config import (
    TemplateConfig,
    TemplateParams,
    best_text_color,
    contrast_ratio,
    get_template_config,
    hex_to_rgb,
    luminance,
)
from app.services.text_layout import TextBlock, TextOverflowError, draw_block, make_block, text_width

SCALES = [1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64, 0.58, 0.52, 0.46]
RGB = tuple[int, int, int]


class PhotoRequiredError(ValueError):
    """O modelo escolhido precisa de uma foto e nenhuma foi informada."""


@dataclass
class Element:
    name: str                     # title | subtitle | body | items | tag | wordmark
    text: Any                     # str ou list[str] (items)
    weight: str
    base_size: int
    color: RGB
    gap_after: int = 28
    line_spacing: float = 1.15
    pill_bg: RGB | None = None    # para tag
    item_style: str = "checks"
    item_colors: dict[str, RGB] = field(default_factory=dict)
    min_key: str | None = None


@dataclass
class Laid:
    el: Element
    height: int
    size: int
    blocks: list[TextBlock]
    width: int = 0


@dataclass
class RenderResult:
    image: Image.Image
    report: dict[str, Any]


class Renderer:
    def __init__(self, params: TemplateParams, cfg: TemplateConfig, fonts: FontResolver,
                 photo: Image.Image | None = None):
        self.p = params
        self.photo = photo
        self.overlay_alpha: int | None = None
        self.cfg = cfg
        self.fonts = fonts
        self.W, self.H = cfg.canvas
        pal = {k: hex_to_rgb(v) for k, v in cfg.palette.items()}
        self.white: RGB = pal.get("white", (255, 255, 255))
        self.ink: RGB = pal.get("ink", (28, 16, 32))
        self.soft: RGB = pal.get("soft", (255, 242, 252))
        self.muted: RGB = pal.get("muted", (107, 90, 104))
        self.night: RGB = pal.get("night", (22, 0, 15))
        self.primary: RGB = hex_to_rgb(params.primary_color)
        self.accent: RGB = hex_to_rgb(params.accent_color)
        self.img = Image.new("RGBA", (self.W, self.H), self.white + (255,))
        self.draw = ImageDraw.Draw(self.img)
        self.m = params.margin
        self.min_sizes = cfg.brand.get("min_font_sizes", {})
        self.max_lines = cfg.brand.get("max_lines", {})
        self.placed: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.bg_colors: list[RGB] = []

    # ------------------------------------------------------------ primitivas
    def font(self, weight: str, size: int):
        return self.fonts.get(weight, size)

    def fill_background(self, kind: str | None = None) -> list[RGB]:
        kind = kind or self.p.background
        if kind == "gradient":
            self.gradient(self.primary, self.accent)
            self.bg_colors = [self.primary, self.accent]
        elif kind == "solid":
            self.rect((0, 0, self.W, self.H), self.primary)
            self.bg_colors = [self.primary]
        elif kind == "light":
            self.rect((0, 0, self.W, self.H), self.soft)
            self.bg_colors = [self.soft]
        else:  # dark
            self.rect((0, 0, self.W, self.H), self.night)
            self.bg_colors = [self.night]
        return self.bg_colors

    def gradient(self, c1: RGB, c2: RGB, box: tuple[int, int, int, int] | None = None) -> None:
        x0, y0, x1, y1 = box or (0, 0, self.W, self.H)
        w, h = x1 - x0, y1 - y0
        side = int(math.hypot(w, h)) + 4
        mask = Image.linear_gradient("L").resize((side, side)).rotate(45, resample=Image.BICUBIC)
        left, top = (side - w) // 2, (side - h) // 2
        mask = mask.crop((left, top, left + w, top + h))
        a = Image.new("RGBA", (w, h), c1 + (255,))
        b = Image.new("RGBA", (w, h), c2 + (255,))
        self.img.paste(Image.composite(b, a, mask), (x0, y0))

    def rect(self, box, color: RGB, radius: int = 0, alpha: int = 255, outline: RGB | None = None,
             width: int = 0) -> None:
        if alpha >= 255:
            if radius:
                self.draw.rounded_rectangle(box, radius=radius, fill=color, outline=outline, width=width)
            else:
                self.draw.rectangle(box, fill=color, outline=outline, width=width)
            return
        ov = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        if radius:
            d.rounded_rectangle(box, radius=radius, fill=color + (alpha,))
        else:
            d.rectangle(box, fill=color + (alpha,))
        self.img.alpha_composite(ov)

    def shape_layer(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        ov = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        return ov, ImageDraw.Draw(ov)

    def commit(self, ov: Image.Image) -> None:
        self.img.alpha_composite(ov)

    def text_on_bg(self) -> RGB:
        if self.p.text_color != "auto":
            return hex_to_rgb(self.p.text_color)
        return best_text_color(self.bg_colors or [self.white], self.white, self.ink)

    def secondary_on_bg(self, main: RGB) -> RGB:
        if main == self.ink:
            return self.muted
        return main

    # -------------------------------------------------------------- marca
    def logo_row(self, color: RGB, dot: RGB | None = None) -> int:
        """Desenha logotipo (texto ou imagem) e localização. Retorna a coordenada y abaixo da linha."""
        y = self.m - 20
        row_h = 60
        if not self.p.show_logo and not self.p.show_location:
            return self.m
        left_is_logo = self.p.logo_position == "top_left"
        if self.p.show_logo:
            logo_img = self.cfg.brand.get("logo_image")
            if logo_img and Path(logo_img).is_file():
                lg = Image.open(logo_img).convert("RGBA")
                ratio = row_h / lg.height
                lg = lg.resize((max(1, int(lg.width * ratio)), row_h))
                x = self.m if left_is_logo else self.W - self.m - lg.width
                self.img.alpha_composite(lg, (int(x), y))
            else:
                text = self.cfg.brand.get("logo_text", "doova")
                f = self.font("extrabold", 52)
                w = text_width(f, text)
                dot_r = 9
                total = w + dot_r * 2 + 6
                x = self.m if left_is_logo else self.W - self.m - total
                self.draw.text((x, y + row_h / 2), text, font=f, fill=color, anchor="lm")
                cx = x + w + 6 + dot_r
                cy = y + row_h / 2 + 12
                self.draw.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=dot or self.accent)
            self._place("logo", (self.m, y, self.W - self.m, y + row_h), 52, 1)
        loc = self.cfg.brand.get("handle") or (self.cfg.brand.get("location") if self.p.show_location else None)
        if loc:
            f = self.font("semibold", 28)
            w = text_width(f, loc)
            x = self.W - self.m - w if left_is_logo else self.m
            self.draw.text((x, y + row_h / 2), loc, font=f, fill=color, anchor="lm")
        return y + row_h

    def content_top(self, logo_bottom: int) -> int:
        return logo_bottom + 56

    # --------------------------------------------------------------- pílula
    def measure_pill(self, text: str, size: int, weight: str = "bold", pad_x: int | None = None,
                     max_width: int | None = None) -> tuple[int, int, int]:
        min_size = self.min_sizes.get("cta", 24)
        max_width = max_width or (self.W - 2 * self.m)
        s = size
        while True:
            f = self.font(weight, s)
            px = pad_x if pad_x is not None else int(s * 1.1)
            w = int(text_width(f, text)) + 2 * px
            h = int(s * 2.1)
            if w <= max_width:
                return w, h, s
            if s <= min_size:
                raise TextOverflowError(f"texto do botão/selo não cabe: {text!r}")
            s -= 2

    def pill(self, x: float, y: float, text: str, size: int, bg: RGB | None, fg: RGB, weight: str = "bold",
             outline: RGB | None = None, name: str = "pill", max_width: int | None = None) -> tuple[int, int]:
        w, h, s = self.measure_pill(text, size, weight, max_width=max_width)
        box = (int(x), int(y), int(x + w), int(y + h))
        self.draw.rounded_rectangle(box, radius=h // 2, fill=bg, outline=outline, width=4 if outline else 0)
        self.draw.text((x + w / 2, y + h / 2), text, font=self.font(weight, s), fill=fg, anchor="mm")
        self._place(name, box, s, 1)
        return w, h

    def cta(self, bg: RGB | None, fg: RGB, outline: RGB | None = None, bottom: int | None = None,
            full_width: bool = False) -> int:
        """Desenha a chamada para ação. Retorna o topo ocupado (para limitar a área de conteúdo)."""
        bottom = bottom if bottom is not None else self.H - self.m
        if not self.p.cta:
            return bottom
        size = self.p.cta_size
        max_w = self.W - 2 * self.m
        w, h, s = self.measure_pill(self.p.cta, size, max_width=max_w)
        if full_width:
            w = max_w
        y = bottom - h
        x = self.m if (self.p.cta_align == "left" and not full_width) else (self.W - w) / 2
        box = (int(x), int(y), int(x + w), int(y + h))
        self.draw.rounded_rectangle(box, radius=h // 2, fill=bg, outline=outline, width=4 if outline else 0)
        self.draw.text((x + w / 2, y + h / 2), self.p.cta, font=self.font("bold", s), fill=fg, anchor="mm")
        self._place("cta", box, s, 1)
        return int(y)

    # ------------------------------------------------------- motor de layout
    def standard_elements(self, title_color: RGB, sub_color: RGB, body_color: RGB, tag_bg: RGB | None,
                          tag_fg: RGB, item_colors: dict[str, RGB] | None = None,
                          include_tag: bool = True) -> list[Element]:
        p = self.p
        els: list[Element] = []
        if include_tag and p.tag:
            els.append(Element("tag", p.tag, "bold", p.tag_size, tag_fg, gap_after=34, pill_bg=tag_bg))
        sub = Element("subtitle", p.subtitle, "medium", p.subtitle_size, sub_color, gap_after=30,
                      line_spacing=1.22) if p.subtitle else None
        if sub and p.subtitle_position == "above_title":
            sub.gap_after = 22
            els.append(sub)
        els.append(Element("title", p.title, "extrabold", p.title_size, title_color, gap_after=34,
                           line_spacing=1.08))
        if sub and p.subtitle_position == "below_title":
            els.append(sub)
        if p.body:
            els.append(Element("body", p.body, "regular", p.body_size, body_color, gap_after=30, line_spacing=1.32))
        if p.items:
            els.append(Element("items", p.items, "semibold", p.body_size, body_color, gap_after=0,
                               line_spacing=1.25, item_style=p.item_style, item_colors=item_colors or {},
                               min_key="item"))
        if els:
            els[-1].gap_after = 0
        return els

    def _min_size(self, el: Element) -> int:
        return int(self.min_sizes.get(el.min_key or el.name, 24))

    def _layout_element(self, el: Element, scale: float, width: int) -> Laid | None:
        size = max(self._min_size(el), round(el.base_size * scale))
        if el.name == "tag":
            try:
                w, h, s = self.measure_pill(el.text, size, max_width=width)
            except TextOverflowError:
                return None
            return Laid(el, h, s, [], width=w)
        font = self.font(el.weight, size)
        if el.name == "items":
            blocks = []
            total = 0
            badge = int(size * 1.55)
            if el.item_style == "cards":
                inner_w = width - 2 * 28 - badge - 24
            else:
                inner_w = width - badge - 24
            for item in el.text:
                b = make_block(item, font, size, inner_w, el.line_spacing)
                if b.width > inner_w or len(b.lines) > int(self.max_lines.get("item", 3)):
                    return None
                blocks.append(b)
                row = max(b.height, badge)
                total += row + (2 * 24 if el.item_style == "cards" else 0)
            total += (len(blocks) - 1) * (18 if el.item_style == "cards" else 22)
            return Laid(el, total, size, blocks)
        b = make_block(el.text, font, size, width, el.line_spacing)
        max_l = int(self.max_lines.get(el.name, 99))
        if b.width > width or len(b.lines) > max_l:
            return None
        return Laid(el, b.height, size, [b], width=b.width)

    def stack(self, elements: list[Element], region: tuple[int, int, int, int], valign: str | None = None,
              align: str | None = None) -> tuple[int, int]:
        """Posiciona os elementos verticalmente na região. Retorna (y_inicial, y_final)."""
        valign = valign or self.p.title_position
        align = align or self.p.text_align
        x0, y0, x1, y1 = region
        width, avail = x1 - x0, y1 - y0
        if avail <= 0 or width <= 0:
            raise TextOverflowError("área de conteúdo inexistente")
        chosen: list[Laid] | None = None
        used_scale = None
        for scale in SCALES:
            laid = [self._layout_element(e, scale, width) for e in elements]
            if any(item is None for item in laid):
                continue
            total = sum(item.height for item in laid) + sum(item.el.gap_after for item in laid[:-1])
            if total <= avail:
                chosen, used_scale = laid, scale  # type: ignore[assignment]
                break
        if chosen is None:
            raise TextOverflowError(
                "o texto não cabe na arte nem no menor tamanho legível; reduza título, subtítulo ou itens"
            )
        total = sum(item.height for item in chosen) + sum(item.el.gap_after for item in chosen[:-1])
        if valign == "top":
            y = y0
        elif valign == "bottom":
            y = y1 - total
        else:
            y = y0 + (avail - total) // 2
        start = y
        for item in chosen:
            self._draw_laid(item, x0, y, width, align)
            y += item.height + item.el.gap_after
        self.scale_used = used_scale
        return start, y

    def _draw_laid(self, item: Laid, x0: int, y: int, width: int, align: str) -> None:
        el = item.el
        if el.name == "tag":
            x = x0 if align == "left" else x0 + (width - item.width) / 2
            self.pill(x, y, el.text, item.size, el.pill_bg, el.color, name="tag", max_width=width)
            return
        if el.name == "items":
            self._draw_items(item, x0, y, width)
            return
        b = item.blocks[0]
        draw_block(self.draw, b, x0, y, el.color, align=align, box_width=width)
        bx = x0 if align == "left" else x0 + (width - b.width) // 2
        self._place(el.name, (int(bx), int(y), int(bx + b.width), int(y + b.height)), item.size, len(b.lines))

    def _draw_items(self, item: Laid, x0: int, y: int, width: int) -> None:
        el = item.el
        size = item.size
        badge = int(size * 1.55)
        badge_bg = el.item_colors.get("badge_bg", self.primary)
        badge_fg = el.item_colors.get("badge_fg", self.white)
        card_bg = el.item_colors.get("card_bg", self.white)
        text_c = el.item_colors.get("text", el.color)
        cy = y
        for i, b in enumerate(item.blocks, start=1):
            row = max(b.height, badge)
            if el.item_style == "cards":
                h = row + 48
                self.draw.rounded_rectangle((x0, cy, x0 + width, cy + h), radius=28, fill=card_bg)
                bx, by = x0 + 28, cy + 24 + (row - badge) // 2
                tx, ty = bx + badge + 24, cy + 24 + (row - b.height) // 2
                step = h + 18
            else:
                h = row
                bx, by = x0, cy + (row - badge) // 2
                tx, ty = x0 + badge + 24, cy + (row - b.height) // 2
                step = h + 22
            if el.item_style in ("cards", "numbered"):
                self.draw.ellipse((bx, by, bx + badge, by + badge), fill=badge_bg)
                self.draw.text((bx + badge / 2, by + badge / 2), str(i), font=self.font("extrabold", int(size * 0.9)),
                               fill=badge_fg, anchor="mm")
            else:
                s = int(badge * 0.62)
                ox, oy = bx + (badge - s) / 2, by + (badge - s) / 2
                self.draw.rounded_rectangle((ox, oy, ox + s, oy + s), radius=s // 4, fill=badge_bg)
                # marca de verificação desenhada com linhas
                self.draw.line([(ox + s * 0.25, oy + s * 0.52), (ox + s * 0.43, oy + s * 0.7),
                                (ox + s * 0.76, oy + s * 0.32)], fill=badge_fg, width=max(3, s // 8))
            draw_block(self.draw, b, tx, ty, text_c)
            self._place(f"item_{i}", (int(x0), int(cy), int(x0 + width), int(cy + h)), size, len(b.lines))
            cy += step

    # ---------------------------------------------------------- relatório
    def _place(self, name: str, box, size: int, lines: int) -> None:
        self.placed.append({"name": name, "box": [int(v) for v in box], "font_size": int(size), "lines": int(lines)})

    # ---------------------------------------------------------------- fotos
    def paste_photo(self, box: tuple[int, int, int, int]) -> None:
        """Recorta a foto para preencher a caixa (sem distorcer), respeitando photo_focus_y."""
        if self.photo is None:
            raise PhotoRequiredError(f"o modelo '{self.p.template}' exige uma foto")
        x0, y0, x1, y1 = box
        fitted = ImageOps.fit(self.photo.convert("RGB"), (x1 - x0, y1 - y0), method=Image.LANCZOS,
                              centering=(0.5, self.p.photo_focus_y))
        self.img.paste(fitted.convert("RGBA"), (x0, y0))

    def fade(self, y0: int, y1: int, a0: int, a1: int, color: RGB | None = None) -> None:
        """Véu em degradê vertical: opacidade a0 em y0 até a1 em y1."""
        color = color or self.night
        ov, d = self.shape_layer()
        span = max(1, y1 - y0)
        for y in range(y0, y1):
            a = int(round(a0 + (a1 - a0) * (y - y0) / span))
            d.line([(0, y), (self.W, y)], fill=color + (max(0, min(255, a)),))
        self.commit(ov)

    def bright_point(self, box: tuple[int, int, int, int], pct: float = 0.98) -> RGB:
        """Cor de um ponto claro da região (percentil de luminância) — mede o contraste sobre a foto."""
        x0, y0, x1, y1 = box
        small = self.img.crop(box).convert("RGB").resize((max(1, (x1 - x0) // 6), max(1, (y1 - y0) // 6)),
                                                          Image.BOX)
        raw = small.tobytes()
        px = sorted(((raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)), key=luminance)
        return px[min(len(px) - 1, int(len(px) * pct))]

    def photo_credit_label(self, right: int, bottom: int) -> None:
        """Crédito curto da foto em uma pílula escura (o crédito completo vai na legenda)."""
        text = (self.p.photo_credit or "").strip()
        if not text:
            return
        max_w = self.W - 2 * self.m
        size = 22
        f = self.font("semibold", size)
        while text_width(f, text) + 32 > max_w and size > 16:
            size -= 1
            f = self.font("semibold", size)
        while text_width(f, text) + 32 > max_w and len(text) > 8:
            text = text[:-2].rstrip() + "…"
        w = int(text_width(f, text)) + 32
        h = int(size * 1.9)
        box = (int(right - w), int(bottom - h), int(right), int(bottom))
        self.rect(box, self.night, radius=h // 2, alpha=185)
        self.draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), text, font=f, fill=(242, 242, 242),
                       anchor="mm")
        self._place("photo_credit", box, size, 1)

    def check_bounds(self) -> None:
        for el in self.placed:
            x0, y0, x1, y1 = el["box"]
            if x0 < 0 or y0 < 0 or x1 > self.W or y1 > self.H:
                raise TextOverflowError(f"elemento {el['name']} ultrapassa os limites da arte")
        safe = self.m - 24
        for el in self.placed:
            x0, y0, x1, y1 = el["box"]
            if x0 < safe or x1 > self.W - safe:
                self.warnings.append(f"{el['name']} encosta na margem lateral")

    def contrast_report(self, fg: RGB) -> dict[str, float]:
        return {"min_contrast_text_vs_background": round(min(contrast_ratio(fg, b) for b in (self.bg_colors or [self.white])), 2)}


# ============================================================== templates
def _region(r: Renderer, top: int, bottom: int, left: int | None = None, right: int | None = None):
    return (left if left is not None else r.m, top, right if right is not None else r.W - r.m, bottom)


def t_beneficios(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    if r.p.shapes:
        ov, d = r.shape_layer()
        d.ellipse((r.W - 340, -260, r.W + 300, 380), fill=(255, 255, 255, 34))
        d.ellipse((-220, r.H - 420, 220, r.H + 20), fill=(255, 255, 255, 26))
        d.ellipse((r.W - 210, r.H - 560, r.W - 90, r.H - 440), outline=(255, 255, 255, 90), width=8)
        r.commit(ov)
    top = r.content_top(r.logo_row(fg, dot=r.white if fg == r.white else r.accent))
    cta_top = r.cta(r.white, r.primary)
    els = r.standard_elements(fg, fg, fg, r.white, r.primary,
                              item_colors={"card_bg": r.white, "text": r.ink, "badge_bg": r.primary,
                                           "badge_fg": r.white})
    r.stack(els, _region(r, top, cta_top - 56))
    return {"text": fg}


def t_pergunta(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    sub = r.secondary_on_bg(fg)
    if r.p.shapes:
        ov, d = r.shape_layer()
        f = r.font("extrabold", 900)
        d.text((r.W + 60, r.H - 40), "?", font=f, fill=r.accent + (40,), anchor="rd")
        d.rectangle((0, 0, 22, r.H), fill=r.primary + (255,))
        d.ellipse((r.W - 150, 150, r.W - 90, 210), fill=r.primary + (255,))
        r.commit(ov)
    top = r.content_top(r.logo_row(r.primary if fg == r.ink else fg))
    cta_top = r.cta(None, r.accent if fg == r.ink else fg, outline=r.accent if fg == r.ink else fg)
    tag_bg = r.primary
    els = r.standard_elements(fg, sub, sub, tag_bg, r.white)
    r.stack(els, _region(r, top, cta_top - 56))
    return {"text": fg}


def t_educativo(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    sub = r.secondary_on_bg(fg)
    band_h = r.m + 120
    r.rect((0, 0, r.W, band_h), r.primary)
    r.rect((0, band_h, r.W, band_h + 14), r.accent)
    if r.p.shapes:
        ov, d = r.shape_layer()
        for i in range(6):
            for j in range(6):
                cx, cy = r.W - r.m - 20 - i * 34, r.H - r.m - 150 - j * 34
                d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=r.primary + (70,))
        r.commit(ov)
    r.bg_colors = [r.soft] if r.p.background == "light" else r.bg_colors
    logo_bottom = r.logo_row(r.white, dot=r.white)
    top = max(r.content_top(logo_bottom), band_h + 64)
    cta_top = r.cta(r.primary, r.white)
    els = r.standard_elements(fg, sub, sub, r.accent, r.white,
                              item_colors={"badge_bg": r.primary, "badge_fg": r.white, "text": fg})
    r.stack(els, _region(r, top, cta_top - 56))
    return {"text": fg}


def t_autonomos(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    if r.p.shapes:
        ov, d = r.shape_layer()
        cx, cy = r.W - 40, r.H - 260
        for k, rad in enumerate((420, 330, 240, 150)):
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), outline=(255, 255, 255, 70 + 25 * k), width=10)
        # formas decorativas ficam no topo, acima da área de conteúdo, para nunca cobrir texto ou itens
        d.rectangle((r.W - 300, 150, r.W - 258, 192), fill=(255, 255, 255, 120))
        d.rectangle((r.W - 240, 204, r.W - 212, 232), fill=r.accent + (255,))
        r.commit(ov)
    top = r.content_top(r.logo_row(fg))
    cta_top = r.cta(r.white, r.primary)
    els = r.standard_elements(fg, fg, fg, r.white, r.primary,
                              item_colors={"badge_bg": r.white, "badge_fg": r.primary, "text": fg})
    r.stack(els, _region(r, top, cta_top - 64))
    return {"text": fg}


def t_contratantes(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    if r.p.shapes:
        ov, d = r.shape_layer()
        for k in range(-r.H, r.W, 120):
            d.polygon([(k, r.H), (k + 50, r.H), (k + 50 + r.H, 0), (k + r.H, 0)], fill=(255, 255, 255, 16))
        d.rectangle((r.W - r.m - 140, r.H - r.m - 22, r.W - r.m, r.H - r.m), fill=r.accent + (255,))
        r.commit(ov)
    top = r.content_top(r.logo_row(fg, dot=r.accent if fg == r.white else r.primary))
    cta_top = r.cta(r.white, r.primary, bottom=r.H - r.m - 56)
    els = r.standard_elements(fg, fg, fg, r.white, r.primary,
                              item_colors={"badge_bg": r.white, "badge_fg": r.primary, "text": fg})
    r.stack(els, _region(r, top, cta_top - 56))
    return {"text": fg}


def t_funcionalidades(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    right = r.W - r.m
    if r.p.shapes:
        # ilustração geométrica de um celular (sem representar telas reais do app)
        px0, py0, px1, py1 = r.W - 360, 330, r.W - 70, 900
        ov, d = r.shape_layer()
        d.rounded_rectangle((px0 + 18, py0 + 22, px1 + 18, py1 + 22), radius=48, fill=(0, 0, 0, 40))
        d.rounded_rectangle((px0, py0, px1, py1), radius=48, fill=(255, 255, 255, 255))
        d.rounded_rectangle((px0 + 18, py0 + 18, px1 - 18, py1 - 18), radius=34, fill=r.soft + (255,))
        d.rounded_rectangle(((px0 + px1) / 2 - 40, py0 + 30, (px0 + px1) / 2 + 40, py0 + 44), radius=7,
                            fill=r.muted + (90,))
        yy = py0 + 80
        for k, w in enumerate((0.8, 0.55, 0.7)):
            d.rounded_rectangle((px0 + 40, yy, px0 + 40 + (px1 - px0 - 80) * w, yy + 22), radius=11,
                                fill=(r.primary if k == 0 else r.muted) + (200 if k == 0 else 90,))
            yy += 44
        for k in range(3):
            d.rounded_rectangle((px0 + 40, yy + 10, px1 - 40, yy + 94), radius=20, fill=(255, 255, 255, 255))
            d.ellipse((px0 + 56, yy + 26, px0 + 108, yy + 78), fill=(r.primary if k % 2 == 0 else r.accent) + (255,))
            d.rounded_rectangle((px0 + 124, yy + 34, px1 - 64, yy + 50), radius=8, fill=r.muted + (110,))
            d.rounded_rectangle((px0 + 124, yy + 60, px1 - 110, yy + 72), radius=6, fill=r.muted + (60,))
            yy += 110
        r.commit(ov)
        right = r.W - 400
    top = r.content_top(r.logo_row(fg))
    cta_top = r.cta(r.white, r.primary)
    els = r.standard_elements(fg, fg, fg, r.white, r.primary,
                              item_colors={"badge_bg": r.white, "badge_fg": r.primary, "text": fg})
    title_el = next(e for e in els if e.name == "title")
    if r.p.shapes:
        title_el.base_size = min(title_el.base_size, 80)
    r.stack(els, _region(r, top, cta_top - 56, right=right))
    return {"text": fg}


def t_aquisicao(r: Renderer) -> dict:
    split = int(r.H * 0.56)
    if r.p.background == "gradient":
        r.gradient(r.primary, r.accent, (0, 0, r.W, split))
        r.bg_colors = [r.primary, r.accent]
    else:
        r.rect((0, 0, r.W, split), r.primary if r.p.background != "dark" else r.night)
        r.bg_colors = [r.primary if r.p.background != "dark" else r.night]
    fg = r.text_on_bg()
    r.rect((0, split, r.W, r.H), r.white)
    if r.p.shapes:
        ov, d = r.shape_layer()
        d.polygon([(r.W, split - 260), (r.W, split), (r.W - 260, split)], fill=r.accent + (255,))
        d.polygon([(r.W, split - 260), (r.W - 260, split), (r.W - 330, split)], fill=(255, 255, 255, 60))
        d.ellipse((r.W - 260, 120, r.W - 120, 260), outline=(255, 255, 255, 110), width=8)
        r.commit(ov)
    logo_bottom = r.logo_row(fg)
    top = r.content_top(logo_bottom)
    p = r.p
    upper: list[Element] = []
    if p.tag:
        upper.append(Element("tag", p.tag, "bold", p.tag_size, r.primary, gap_after=30, pill_bg=r.white))
    upper.append(Element("title", p.title, "extrabold", p.title_size, fg, gap_after=0, line_spacing=1.05))
    r.stack(upper, _region(r, top, split - 60))
    cta_top = r.cta(r.accent, r.white, full_width=True)
    lower: list[Element] = []
    if p.subtitle:
        lower.append(Element("subtitle", p.subtitle, "semibold", p.subtitle_size, r.ink, gap_after=24, line_spacing=1.2))
    if p.body:
        lower.append(Element("body", p.body, "regular", p.body_size, r.muted, gap_after=24, line_spacing=1.3))
    if p.items:
        lower.append(Element("items", p.items, "semibold", p.body_size, r.ink, gap_after=0, item_style=p.item_style,
                             item_colors={"badge_bg": r.primary, "badge_fg": r.white, "text": r.ink}, min_key="item"))
    if lower:
        lower[-1].gap_after = 0
        r.stack(lower, _region(r, split + 56, cta_top - 48), valign="top")
    return {"text": fg}


def t_institucional(r: Renderer) -> dict:
    r.fill_background()
    fg = r.text_on_bg()
    sub = (255, 190, 240) if fg == r.white and r.p.background == "dark" else r.secondary_on_bg(fg)
    if r.p.shapes:
        ov, d = r.shape_layer()
        cx, cy, rad = r.W - 60, -60, 330
        steps = 48
        for k in range(steps):
            t = k / (steps - 1)
            col = tuple(int(r.primary[i] + (r.accent[i] - r.primary[i]) * t) for i in range(3))
            a0 = 90 + k * (180 / steps)
            d.arc((cx - rad, cy - rad, cx + rad, cy + rad), a0, a0 + 180 / steps + 1, fill=col + (255,), width=46)
        d.ellipse((r.m, r.H - r.m - 170, r.m + 26, r.H - r.m - 144), fill=r.primary + (255,))
        d.ellipse((r.m + 40, r.H - r.m - 170, r.m + 66, r.H - r.m - 144), fill=r.accent + (255,))
        r.commit(ov)
    show_logo_row = r.p.show_location or (r.cfg.brand.get("handle") is not None)
    top = r.m + 40
    if show_logo_row:
        saved = r.p.show_logo
        r.p.show_logo = False  # a marca aparece grande no centro
        top = r.content_top(r.logo_row(fg))
        r.p.show_logo = saved
    cta_top = r.cta(r.primary, r.white)
    els: list[Element] = []
    if r.p.show_logo:
        els.append(Element("wordmark", r.cfg.brand.get("logo_text", "doova"), "extrabold", 170, fg, gap_after=36,
                           line_spacing=1.0, min_key="title"))
    els += r.standard_elements(fg, sub, sub, r.primary, r.white)
    r.stack(els, _region(r, top, cta_top - 56))
    return {"text": fg}


def t_foto_destaque(r: Renderer) -> dict:
    """Foto no topo (52% da altura) e painel com o texto embaixo."""
    ph = int(r.H * 0.52)
    r.paste_photo((0, 0, r.W, ph))
    r.fade(0, 240, 165, 0)  # véu para o logotipo sobre a foto
    r.logo_row(r.white, dot=r.primary)
    panel = (0, ph, r.W, r.H)
    kind = r.p.background
    if kind == "gradient":
        r.gradient(r.primary, r.accent, panel)
        r.bg_colors = [r.primary, r.accent]
    elif kind == "solid":
        r.rect(panel, r.primary)
        r.bg_colors = [r.primary]
    elif kind == "light":
        r.rect(panel, r.soft)
        r.bg_colors = [r.soft]
    else:
        r.rect(panel, r.night)
        r.bg_colors = [r.night]
    if r.p.shapes:
        r.rect((r.m, ph - 9, r.m + 150, ph + 9), r.accent, radius=9)
    fg = r.text_on_bg()
    sub = r.secondary_on_bg(fg)
    r.photo_credit_label(r.W - r.m, ph - 22)
    cta_top = r.cta(r.ink, r.white) if fg == r.ink else r.cta(r.white, r.ink)
    els = r.standard_elements(fg, sub, sub, r.primary, r.ink)
    r.stack(els, _region(r, ph + 60, cta_top - 44))
    return {"text": fg}


def t_foto_fundo(r: Renderer) -> dict:
    """Foto em tela cheia; o véu escuro é reforçado até o texto atingir contraste mínimo."""
    r.paste_photo((0, 0, r.W, r.H))
    r.fade(0, 300, 175, 0)
    text_top = int(r.H * 0.42)
    measure = (r.m, text_top, r.W - r.m, r.H - r.m)
    base = r.img.copy()
    point = r.white
    for alpha in (150, 175, 195, 212, 228, 240, 250):
        r.img = base.copy()
        r.draw = ImageDraw.Draw(r.img)
        r.fade(int(r.H * 0.2), text_top, 0, alpha)
        r.fade(text_top, r.H, alpha, min(255, alpha + 25))
        point = r.bright_point(measure)
        r.overlay_alpha = alpha
        if contrast_ratio(r.white, point) >= 4.8:
            break
    r.bg_colors = [point]
    fg = r.text_on_bg()
    top = r.content_top(r.logo_row(r.white, dot=r.primary))
    cta_top = r.cta(r.white, r.ink)
    els = r.standard_elements(fg, fg, fg, r.primary, r.ink)
    r.stack(els, _region(r, max(top, text_top + 24), cta_top - 48))
    r.photo_credit_label(r.W - r.m, r.H - 18)
    return {"text": fg}


TEMPLATE_RENDERERS: dict[str, Callable[[Renderer], dict]] = {
    "beneficios": t_beneficios,
    "pergunta": t_pergunta,
    "educativo": t_educativo,
    "autonomos": t_autonomos,
    "contratantes": t_contratantes,
    "funcionalidades": t_funcionalidades,
    "aquisicao": t_aquisicao,
    "institucional": t_institucional,
    "foto_destaque": t_foto_destaque,
    "foto_fundo": t_foto_fundo,
}


# ================================================================ API
def render(params: TemplateParams, cfg: TemplateConfig | None = None, fonts: FontResolver | None = None,
           photo: Image.Image | None = None) -> RenderResult:
    cfg = cfg or get_template_config()
    fonts = fonts or get_font_resolver()
    fn = TEMPLATE_RENDERERS.get(params.template)
    if fn is None:
        raise KeyError(f"template sem renderizador: {params.template}")
    if cfg.requires_photo(params.template) and photo is None:
        raise PhotoRequiredError(f"o modelo '{params.template}' exige uma foto (photo_id)")
    r = Renderer(params, cfg, fonts, photo)
    info = fn(r)
    r.check_bounds()
    img = r.img.convert("RGB")
    report = {
        "template": params.template,
        "size": [r.W, r.H],
        "elements": r.placed,
        "scale_used": getattr(r, "scale_used", None),
        "text_amount": params.text_amount(),
        "font_fallback_used": fonts.fallback_used,
        "warnings": r.warnings,
        "photo_overlay_alpha": r.overlay_alpha,
        **r.contrast_report(info["text"]),
    }
    return RenderResult(img, report)


@dataclass
class GeneratedImage:
    png_path: Path
    jpg_path: Path
    jpg_bytes: bytes
    sha256: str
    width: int
    height: int
    report: dict[str, Any]


def params_hash(params: TemplateParams) -> str:
    return hashlib.sha256(json.dumps(params.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export(result: RenderResult, out_dir: Path, params: TemplateParams) -> GeneratedImage:
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path, jpg_path = out_dir / "arte.png", out_dir / "arte.jpg"
    result.image.save(png_path, format="PNG", optimize=True)
    buf = io.BytesIO()
    # A Meta aceita somente JPEG para imagens publicadas pela API.
    result.image.save(buf, format="JPEG", quality=92, subsampling=0, optimize=True)
    jpg = buf.getvalue()
    jpg_path.write_bytes(jpg)
    (out_dir / "parametros.json").write_text(
        json.dumps({"params": params.model_dump(), "layout": result.report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return GeneratedImage(png_path, jpg_path, jpg, hashlib.sha256(png_path.read_bytes()).hexdigest(),
                          result.image.width, result.image.height, result.report)


def generate(params: TemplateParams, out_dir: Path, cfg: TemplateConfig | None = None,
             fonts: FontResolver | None = None, photo: Image.Image | None = None) -> GeneratedImage:
    return export(render(params, cfg, fonts, photo), out_dir, params)
