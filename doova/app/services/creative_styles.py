"""Biblioteca de composições criativas para as artes do Doova.

Os estilos compartilham o motor de medição/contraste e o relatório de layout.
`auto` escolhe uma direção visual de forma determinística a partir do conteúdo;
um estilo explícito pode ser usado em experimentos A/B.
"""
from __future__ import annotations

import hashlib
from typing import Callable

from app.services.image_generator import Element, Renderer, _region

STYLE_NAMES = (
    "auto", "editorial", "brutalist", "collage", "minimal", "split",
    "type_poster", "photo_card", "photo_full", "swiss_grid",
    "sticker", "gradient_wave", "quote",
)
STYLE_DESCRIPTIONS = {
    "auto": "Escolhe uma composição variada conforme o conteúdo",
    "editorial": "Editorial assimétrico, tipografia forte e hierarquia de revista",
    "brutalist": "Brutalista, blocos sólidos, contraste e títulos grandes",
    "collage": "Colagem com recortes, etiquetas e formas sobrepostas",
    "minimal": "Minimalista, bastante espaço negativo e poucos elementos",
    "split": "Composição dividida entre texto e imagem/bloco gráfico",
    "type_poster": "Pôster tipográfico com palavras em escala e alinhamento expressivo",
    "photo_card": "Fotografia em cartão destacado com painel tipográfico",
    "photo_full": "Fotografia em tela cheia com texto sobre área protegida",
    "swiss_grid": "Grade editorial suíça, alinhamento e linhas geométricas",
    "sticker": "Visual pop com selos, etiquetas e elementos arredondados",
    "gradient_wave": "Gradiente de marca, formas orgânicas e texto em camadas",
    "quote": "Composição de citação com aspas e destaque de frase",
}


def _elements(r: Renderer, title_color, sub_color, body_color, tag_bg, tag_fg):
    return r.standard_elements(title_color, sub_color, body_color, tag_bg, tag_fg,
        item_colors={"card_bg": r.white, "text": r.ink, "badge_bg": r.primary, "badge_fg": r.white})


def _photo_panel(r: Renderer, top: int, bottom: int, x0: int, x1: int):
    top, bottom, x0, x1 = map(int, (top, bottom, x0, x1))
    if r.photo is None:
        r.rect((x0, top, x1, bottom), r.primary, radius=36)
        # Abstract image placeholder: layered forms, not a fake photograph.
        ov, d = r.shape_layer()
        w, h = x1-x0, bottom-top
        d.ellipse((x0+w*.40, top-h*.18, x1+w*.18, top+h*.55), fill=r.accent+(230,))
        d.ellipse((x0-w*.12, top+h*.43, x0+w*.62, bottom+h*.12), fill=r.white+(65,))
        r.commit(ov)
    else:
        r.paste_photo((x0, top, x1, bottom))


def _draw(r: Renderer, style: str):
    p = r.p
    # Cada direção visual recebe famílias tipográficas reais, não apenas pesos diferentes.
    # O usuário pode sobrescrever qualquer papel via *_font_family na configuração.
    style_fonts = {
        "editorial": {"title": "serif_display", "subtitle": "inter", "body": "inter", "cta": "inter"},
        "brutalist": {"title": "lato", "subtitle": "condensed", "body": "inter", "cta": "lato"},
        "collage": {"title": "condensed", "subtitle": "slab", "body": "inter", "cta": "lato"},
        "minimal": {"title": "inter", "subtitle": "inter", "body": "inter", "cta": "inter"},
        "split": {"title": "slab", "subtitle": "inter", "body": "inter", "cta": "lato"},
        "type_poster": {"title": "condensed", "subtitle": "inter", "body": "inter", "cta": "lato"},
        "photo_card": {"title": "serif_display", "subtitle": "inter", "body": "inter", "cta": "inter"},
        "photo_full": {"title": "lato", "subtitle": "inter", "body": "inter", "cta": "lato"},
        "swiss_grid": {"title": "slab", "subtitle": "inter", "body": "inter", "cta": "inter"},
        "sticker": {"title": "lato", "subtitle": "condensed", "body": "inter", "cta": "lato"},
        "gradient_wave": {"title": "inter", "subtitle": "slab", "body": "inter", "cta": "inter"},
        "quote": {"title": "serif_display", "subtitle": "inter", "body": "inter", "cta": "inter"},
    }
    # Resolve a repeatable style so retries of the same post keep the same artwork.
    if style == "auto":
        choices = STYLE_NAMES[1:]
        seed = f"{p.title}|{p.template}|{p.tag or ''}".encode("utf-8")
        style = choices[int(hashlib.sha256(seed).hexdigest()[:8], 16) % len(choices)]

    r.role_font_families = style_fonts.get(style, style_fonts["minimal"]).copy()
    for role, key in (("title", "title_font_family"), ("subtitle", "subtitle_font_family"),
                      ("body", "body_font_family"), ("cta", "cta_font_family")):
        requested = getattr(p, key, "auto")
        if requested != "auto":
            r.role_font_families[role] = requested

    if style == "photo_full":
        if r.photo is not None:
            r.paste_photo((0, 0, r.W, r.H))
            r.fade(0, int(r.H*.34), 170, 0)
            r.fade(int(r.H*.38), r.H, 20, 220)
        else:
            r.fill_background("dark")
            ov, d = r.shape_layer()
            d.ellipse((r.W*.35, r.H*.05, r.W*1.1, r.H*.68), fill=r.primary+(180,))
            d.ellipse((-r.W*.25, r.H*.45, r.W*.65, r.H*1.15), fill=r.accent+(125,))
            r.commit(ov)
        r.bg_colors = [r.night]
        fg, sub = r.white, (255, 235, 249)
        top = r.content_top(r.logo_row(r.white))
        r.stack(_elements(r, fg, sub, sub, r.night, r.white),
                _region(r, max(top, int(r.H*.38)), r.H-r.m-30), valign="bottom")
        r.photo_credit_label(r.W-r.m, r.H-18)
        if p.app_ui:
            r.draw.ellipse((r.W-r.m-24, 26, r.W-r.m-8, 42), fill=r.accent)
        return {"text": fg, "style": style}

    if style == "photo_card":
        r.fill_background("light")
        x0, x1 = r.m, r.W-r.m
        y0, y1 = int(r.H*.08), int(r.H*.34)
        _photo_panel(r, y0, y1, x0, x1)
        fg = r.ink
        top = r.logo_row(r.ink)
        r.stack(_elements(r, fg, r.muted, r.muted, r.primary, r.white),
                _region(r, y1+45, r.H-r.m-20), valign="top")
        r.photo_credit_label(x1, y1-12)
        if p.app_ui:
            r.draw.ellipse((r.W-r.m-24, 26, r.W-r.m-8, 42), fill=r.accent)
        return {"text": fg, "style": style}

    r.fill_background("light" if style in ("minimal", "editorial", "swiss_grid", "quote") else p.background)
    fg = r.text_on_bg()
    sub = r.secondary_on_bg(fg)
    logo_bottom = r.logo_row(fg)
    top = max(r.content_top(logo_bottom), 155)
    bottom = r.H-r.m
    cta_top = r.cta(r.primary if fg == r.ink else r.white,
                    r.white if fg == r.ink else r.primary)
    bottom = min(bottom, cta_top-38)

    if style == "editorial":
        r.rect((r.m, top, r.m+12, bottom), r.accent)
        r.rect((r.m+34, top+12, r.m+180, top+22), r.primary)
        right = r.W-r.m
        if r.photo is not None:
            py = int(r.H*.61)
            _photo_panel(r, py, min(bottom, r.H-r.m), r.W*.47, right)
            bottom = min(bottom, py-28)
        els = _elements(r, fg, sub, sub, r.primary, r.white)
        if els and els[0].name == "tag":
            els[0].gap_after = 48
        r.stack(els, _region(r, top+45, bottom, left=r.m+42), valign="top", align="left")

    elif style == "brutalist":
        r.rect((0, int(r.H*.68), r.W, r.H), r.primary)
        r.rect((r.W-210, top-15, r.W-math_floor(60), top+135), r.accent)
        r.rect((r.m, top, r.m+26, bottom), r.ink if fg == r.white else r.primary)
        els = _elements(r, fg, sub, sub, r.accent, r.white)
        title = next((e for e in els if e.name == "title"), None)
        if title: title.base_size = min(154, int(title.base_size*1.35))
        r.stack(els, _region(r, top+55, bottom-20, left=r.m+58), valign="top")

    elif style == "collage":
        ov, d = r.shape_layer()
        d.polygon([(r.W-360,top-30),(r.W-40,top+90),(r.W-170,top+340)], fill=r.accent+(230,))
        d.ellipse((r.W-330,bottom-300,r.W+80,bottom+120), fill=r.primary+(180,))
        d.rectangle((r.m+15,top+150,r.m+95,top+230), fill=r.accent+(255,))
        r.commit(ov)
        if r.photo is not None:
            _photo_panel(r, top+20, min(bottom,top+410), r.W*.48, r.W-r.m)
            bottom = min(bottom,top+445)
        els = _elements(r, fg, sub, sub, r.ink, r.white)
        r.stack(els, _region(r, top+35, bottom, right=int(r.W*.58)), valign="top")

    elif style == "minimal":
        r.rect((r.m, top, r.m+5, bottom), r.primary)
        r.rect((r.m, bottom-90, r.m+190, bottom-82), r.accent)
        els = _elements(r, fg, sub, sub, None, r.primary)
        r.stack(els, _region(r, top+95, bottom-125, left=r.m+42), valign="center")

    elif style == "split":
        split = int(r.W*.48)
        r.rect((split, 0, r.W, r.H), r.primary)
        _photo_panel(r, int(r.H*.17), int(r.H*.78), split+25, r.W-r.m)
        r.bg_colors = [r.soft]
        fg = r.ink
        r.stack(_elements(r, fg, r.muted, r.muted, r.accent, r.white),
                _region(r, top+25, bottom, right=split-30), valign="center")

    elif style == "type_poster":
        r.rect((0, int(r.H*.73), r.W, r.H), r.primary)
        if p.tag:
            r.pill(r.m, top, p.tag, p.tag_size, r.accent, r.white, name="tag")
        # Deliberately use a narrower title column and oversized title weight.
        els = _elements(r, fg, sub, sub, r.primary, r.white)
        title = next((e for e in els if e.name == "title"), None)
        if title: title.base_size = min(168, int(title.base_size*1.5))
        r.stack([e for e in els if e.name != "tag"], _region(r, top+90, bottom, right=r.W-r.m-40),
                valign="center", align="left")

    elif style == "swiss_grid":
        for x in (r.m, int(r.W*.34), int(r.W*.67), r.W-r.m):
            r.rect((x, top-15, x+2, bottom), r.muted)
        r.rect((r.m, top-15, r.W-r.m, top-11), r.primary)
        els = _elements(r, fg, sub, sub, r.primary, r.white)
        r.stack(els, _region(r, top+60, bottom, left=r.m+30, right=int(r.W*.84)), valign="top")

    elif style == "sticker":
        ov, d = r.shape_layer()
        d.ellipse((r.W-330,top-70,r.W-70,top+190), fill=r.accent+(255,))
        d.ellipse((r.W-280,bottom-240,r.W-40,bottom), fill=r.primary+(220,))
        r.commit(ov)
        els = _elements(r, fg, sub, sub, r.accent, r.white)
        r.stack(els, _region(r, top+45, bottom-35, right=r.W-r.m-120), valign="center")

    elif style == "gradient_wave":
        r.fill_background("gradient")
        ov, d = r.shape_layer()
        d.ellipse((-r.W*.25,r.H*.42,r.W*.65,r.H*1.12), fill=r.white+(28,))
        d.ellipse((r.W*.55,-r.H*.22,r.W*1.2,r.H*.38), fill=r.accent+(85,))
        r.commit(ov)
        fg = r.text_on_bg()
        els = _elements(r, fg, fg, fg, r.white, r.primary)
        r.stack(els, _region(r, top+20, bottom), valign="center")

    elif style == "quote":
        r.rect((r.m, top+25, r.m+12, bottom-25), r.accent)
        f = r.font("extrabold", 240)
        r.draw.text((r.m+35, top+15), "“", font=f, fill=r.primary, anchor="lt")
        els = _elements(r, fg, sub, sub, r.primary, r.white)
        r.stack(els, _region(r, top+180, bottom-40, left=r.m+45), valign="center")

    else:
        raise ValueError(f"estilo criativo desconhecido: {style}")

    if p.app_ui:
        # Pequenos indicadores de interface: distinguem a direção "app" sem
        # competir com o conteúdo principal.
        cy = max(34, logo_bottom - 2)
        for idx, color in enumerate((r.primary, r.accent, r.muted)):
            cx = r.W - r.m - 18 - idx * 30
            r.draw.ellipse((cx-7, cy-7, cx+7, cy+7), fill=color)

    return {"text": fg, "style": style}


def math_floor(value: float) -> int:
    return int(value)


def render_creative(renderer: Renderer, style: str):
    return _draw(renderer, style)
