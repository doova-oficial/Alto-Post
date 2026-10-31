"""Elementos gráficos da interface do app Doova para uso nas artes de marketing.

Reproduz a linguagem visual do app (vista nas telas Início, Para Você, Mapa e Perfil):

* ícones de linha com ponta arredondada (pin, coração, comentário, enviar, casa, lupa,
  mais, bússola, perfil, estrela, chama, camera, check);
* chips de categoria (ativo = preto, inativo = cinza claro);
* anel rosa de story com avatar cinza e botão "+";
* barra cinza de preço com "CONTRATAR";
* pins do mapa (amarelo/rosa) e toast "Localização encontrada!";
* barra de navegação inferior e contador "1/3" de carrossel.

Tudo é desenhado com supersampling (4x) para ficar suave. Nenhum texto de preço ou nome
real é inventado: onde o app mostra dados, as artes usam apenas rótulos genéricos ou barras.
"""
from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter

from app.services.text_layout import text_width

SS = 4
RGB = tuple[int, int, int]

# Cores medidas nas capturas do app. Podem ser sobrescritas em templates.yaml (brand.colors).
APP_COLORS: dict[str, RGB] = {
    "app_pink": (240, 20, 120),      # chama, anel de story, pin do perfil
    "app_yellow": (232, 176, 0),     # pin amarelo do mapa e estrela "Novo"
    "app_black": (10, 10, 10),       # chip ativo, botão "+" do story
    "app_gray": (230, 230, 230),     # chip inativo, avatar vazio
    "app_bar": (142, 142, 147),      # barra "R$ / CONTRATAR"
    "app_like": (255, 32, 48),       # coração curtido
    "app_silhouette": (168, 168, 168),
}


def col(r, key: str) -> RGB:
    from app.services.template_config import hex_to_rgb

    v = r.cfg.brand.get("colors", {}).get(key)
    return hex_to_rgb(v) if v else APP_COLORS[key]


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


# ------------------------------------------------------------------ base AA
def blit(r, x: float, y: float, w: float, h: float, painter: Callable[[ImageDraw.ImageDraw], None]) -> None:
    """Pinta em uma camada 4x maior e reduz — bordas suaves. ``painter`` usa coordenadas já em 4x."""
    w, h = max(1, int(math.ceil(w))), max(1, int(math.ceil(h)))
    tile = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    painter(ImageDraw.Draw(tile))
    tile = tile.resize((w, h), Image.LANCZOS)
    r.img.alpha_composite(tile, (int(round(x)), int(round(y))))


def round_rect(r, box, radius: float, fill: RGB | tuple | None = None, outline: RGB | None = None, width: float = 0,
               alpha: int = 255) -> None:
    x0, y0, x1, y1 = box
    pad = 2
    w, h = x1 - x0, y1 - y0

    def paint(d):
        f = None if fill is None else tuple(fill[:3]) + (alpha,)
        d.rounded_rectangle((pad * SS, pad * SS, (w + pad) * SS - 1, (h + pad) * SS - 1), radius=radius * SS, fill=f,
                            outline=outline, width=int(width * SS))

    blit(r, x0 - pad, y0 - pad, w + 2 * pad, h + 2 * pad, paint)


def circle(r, cx: float, cy: float, rad: float, fill: RGB | None = None, outline: RGB | None = None,
           width: float = 0, alpha: int = 255) -> None:
    pad = 2
    s = rad * 2 + 2 * pad

    def paint(d):
        f = None if fill is None else tuple(fill[:3]) + (alpha,)
        d.ellipse((pad * SS, pad * SS, (pad + 2 * rad) * SS - 1, (pad + 2 * rad) * SS - 1), fill=f, outline=outline,
                  width=int(width * SS))

    blit(r, cx - rad - pad, cy - rad - pad, s, s, paint)


def shadow(r, box, radius: float, blur: int = 18, alpha: int = 55, dy: int = 10) -> None:
    x0, y0, x1, y1 = box
    pad = blur * 3
    w, h = int(x1 - x0 + 2 * pad), int(y1 - y0 + 2 * pad)
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), radius=radius, fill=(0, 0, 0, alpha))
    tile = tile.filter(ImageFilter.GaussianBlur(blur))
    r.img.alpha_composite(tile, (int(x0 - pad), int(y0 - pad + dy)))


# ------------------------------------------------------------------- ícones
def _arc(cx: float, cy: float, rad: float, a0: float, a1: float, n: int = 48) -> list[tuple[float, float]]:
    return [(cx + rad * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + rad * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def teardrop(cx: float, cy: float, rad: float, tip_y: float, n: int = 60) -> list[tuple[float, float]]:
    """Gota com a ponta em tip_y (abaixo = pin; acima = chama). Contorno fechado."""
    up = tip_y < cy
    dist = abs(tip_y - cy)
    beta = math.degrees(math.acos(min(1.0, rad / dist)))
    base = -90 if up else 90
    return [(cx, tip_y)] + _arc(cx, cy, rad, base + beta, base + 360 - beta, n)


def _fit(pts: list[tuple[float, float]], margin: float = 8.0) -> list[tuple[float, float]]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    k = (100 - 2 * margin) / max(w, h)
    ox = 50 - (min(xs) + w / 2) * k
    oy = 50 - (min(ys) + h / 2) * k
    return [(x * k + ox, y * k + oy) for x, y in pts]


class _Pen:
    def __init__(self, d: ImageDraw.ImageDraw, u: float, color, stroke: float):
        self.d, self.u, self.c, self.w = d, u, tuple(color[:3]) + (255,), stroke * u

    def P(self, pts):
        return [(x * self.u, y * self.u) for x, y in pts]

    def line(self, pts, closed: bool = False) -> None:
        p = self.P(pts)
        if closed:
            p = p + [p[0], p[1]]
        self.d.line(p, fill=self.c, width=int(self.w), joint="curve")
        rad = self.w / 2
        for x, y in p:
            self.d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=self.c)

    def poly(self, pts) -> None:
        self.d.polygon(self.P(pts), fill=self.c)

    def ring(self, cx, cy, rad) -> None:
        u = self.u
        self.d.ellipse(((cx - rad) * u, (cy - rad) * u, (cx + rad) * u, (cy + rad) * u), outline=self.c,
                       width=int(self.w))

    def disc(self, cx, cy, rad, erase: bool = False) -> None:
        u = self.u
        self.d.ellipse(((cx - rad) * u, (cy - rad) * u, (cx + rad) * u, (cy + rad) * u),
                       fill=(0, 0, 0, 0) if erase else self.c)


def _heart_pts() -> list[tuple[float, float]]:
    pts = []
    for i in range(121):
        t = 2 * math.pi * i / 120
        pts.append((16 * math.sin(t) ** 3,
                    -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))))
    return _fit(pts, 9)


def _silhouette(p: _Pen, cx: float, top: float, s: float, erase: bool = False) -> None:
    """Cabeça + ombros, dentro de um quadrado de lado s (como o avatar vazio do app)."""
    p.disc(cx, top + s * 0.34, s * 0.19, erase)
    body = _arc(cx, top + s * 1.02, s * 0.36, 180, 360, 30)
    pts = p.P(body)
    p.d.polygon(pts, fill=(0, 0, 0, 0) if erase else p.c)


def _i_pin(p, fill):
    p.line(teardrop(50, 38, 30, 94), closed=True)
    p.ring(50, 38, 11)


def _i_heart(p, fill):
    pts = _heart_pts()
    if fill:
        p.poly(pts)
    else:
        p.line(pts, closed=True)


def _i_comment(p, fill):
    pts = _arc(50, 46, 38, 130, 470, 70)
    p.line(pts + [(14, 92)], closed=True)


def _i_send(p, fill):
    p.line([(10, 46), (92, 10), (62, 90), (47, 58)], closed=True)
    p.line([(47, 58), (92, 10)])


def _i_home(p, fill):
    p.line([(12, 48), (50, 14), (88, 48)])
    p.line([(22, 42), (22, 88), (78, 88), (78, 42)])
    p.line([(40, 88), (40, 62), (60, 62), (60, 88)])


def _i_search(p, fill):
    p.ring(43, 43, 28)
    p.line([(64, 64), (90, 90)])


def _i_plus(p, fill):
    p.line([(50, 14), (50, 86)])
    p.line([(14, 50), (86, 50)])


def _i_compass(p, fill):
    p.ring(50, 50, 40)
    p.line([(68, 32), (56, 56), (32, 68), (44, 44)], closed=True)


def _i_user(p, fill):
    _silhouette(p, 50, 6, 88)


def _i_user_badge(p, fill):
    p.disc(50, 50, 46)
    _silhouette(p, 50, 18, 64, erase=True)


def _i_star(p, fill):
    pts = []
    for i in range(10):
        rad = 46 if i % 2 == 0 else 19
        a = math.radians(-90 + i * 36)
        pts.append((50 + rad * math.cos(a), 54 + rad * math.sin(a)))
    if fill:
        p.poly(pts)
    else:
        p.line(pts, closed=True)


def _i_flame(p, fill):
    p.poly(teardrop(50, 64, 32, 4))
    p.disc(50, 70, 11, erase=True)


def _i_check(p, fill):
    p.line([(18, 54), (40, 76), (82, 28)])


def _i_camera(p, fill):
    p.line([(10, 32), (30, 32), (36, 20), (64, 20), (70, 32), (90, 32), (90, 84), (10, 84)], closed=True)
    p.ring(50, 57, 17)


def _i_gear(p, fill):
    p.ring(50, 50, 16)
    p.ring(50, 50, 34)


ICONS: dict[str, Callable] = {
    "pin": _i_pin, "heart": _i_heart, "comment": _i_comment, "send": _i_send, "home": _i_home,
    "search": _i_search, "plus": _i_plus, "compass": _i_compass, "user": _i_user, "user_badge": _i_user_badge,
    "star": _i_star, "flame": _i_flame, "check": _i_check, "camera": _i_camera, "gear": _i_gear,
}


def icon(r, name: str, x: float, y: float, size: float, color: RGB, stroke: float = 7.5, fill: bool = False) -> None:
    """Desenha o ícone ``name`` com o canto superior esquerdo em (x, y)."""
    size = int(size)
    u = size * SS / 100

    def paint(d):
        ICONS[name](_Pen(d, u, color, stroke), fill)

    blit(r, x, y, size, size, paint)


def icon_c(r, name: str, cx: float, cy: float, size: float, color: RGB, **kw) -> None:
    icon(r, name, cx - size / 2, cy - size / 2, size, color, **kw)


# ----------------------------------------------------------------- componentes
def chip(r, x: float, y: float, text: str, size: int = 30, active: bool = False, icon_name: str | None = None,
         icon_color: RGB | None = None, dark_bg: bool = False) -> tuple[int, int]:
    """Chip de categoria. Retorna (largura, altura)."""
    f = r.font("medium", size)
    h = int(size * 2.15)
    icw = int(size * 1.0) if icon_name else 0
    gap = int(size * 0.45) if icon_name else 0
    w = int(text_width(f, text)) + int(size * 1.9) + icw + gap
    bg = col(r, "app_black") if active else col(r, "app_gray")
    fg = (255, 255, 255) if active else (28, 28, 30)
    round_rect(r, (x, y, x + w, y + h), h / 2, fill=bg)
    tx = x + (w - (icw + gap)) / 2 + (icw + gap)
    if icon_name:
        icon_c(r, icon_name, x + size * 0.95 + icw / 2, y + h / 2, icw, icon_color or col(r, "app_pink"), fill=True)
    r.draw.text((x + size * 0.95 + icw + gap + text_width(f, text) / 2, y + h / 2), text, font=f, fill=fg, anchor="mm")
    r._place("chip", (x, y, x + w, y + h), size, 1)
    return w, h


def chip_row(r, x: float, y: float, labels: list[str], size: int = 30, active: int = 0, max_right: float | None = None,
             gap: int = 16) -> tuple[int, int]:
    """Fileira de chips; para de desenhar quando o próximo não cabe. Retorna (largura usada, altura)."""
    max_right = max_right or r.W - r.m
    cx, h = x, int(size * 2.15)
    for i, t in enumerate(labels):
        f = r.font("medium", size)
        w = int(text_width(f, t)) + int(size * 1.9)
        if cx + w > max_right:
            break
        chip(r, cx, y, t, size, active=(i == active))
        cx += w + gap
    return int(cx - gap - x), h


def avatar(r, cx: float, cy: float, d: float, bg: RGB | None = None, fg: RGB | None = None) -> None:
    """Avatar vazio do app: círculo cinza claro com silhueta."""
    bg = bg or col(r, "app_gray")
    fg = fg or col(r, "app_silhouette")
    circle(r, cx, cy, d / 2, fill=bg)
    s = d * 0.62
    u = s * SS / 100

    def paint(dr):
        p = _Pen(dr, u, fg, 0)
        _silhouette(p, 50, 0, 100)

    # recorta a silhueta dentro do círculo
    tile = Image.new("RGBA", (int(s) * SS, int(s) * SS), (0, 0, 0, 0))
    paint(ImageDraw.Draw(tile))
    tile = tile.resize((int(s), int(s)), Image.LANCZOS)
    mask = Image.new("L", (int(d), int(d)), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, int(d) - 1, int(d) - 1), fill=255)
    layer = Image.new("RGBA", (int(d), int(d)), (0, 0, 0, 0))
    layer.alpha_composite(tile, (int((d - s) / 2), int(d * 0.2)))
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", layer.size, 0), mask))
    r.img.alpha_composite(layer, (int(cx - d / 2), int(cy - d / 2)))


def story(r, cx: float, cy: float, d: float, ring: bool = True, plus: bool = False, gap_color: RGB = (255, 255, 255)) -> None:
    """Story do app: anel rosa + respiro branco + avatar; ``plus`` = botão preto "+" (Seu story)."""
    ring_w = max(3.0, d * 0.045)
    if ring:
        circle(r, cx, cy, d / 2, fill=col(r, "app_pink"))
        circle(r, cx, cy, d / 2 - ring_w, fill=gap_color)
        avatar(r, cx, cy, d - 2 * (ring_w + d * 0.045))
    else:
        avatar(r, cx, cy, d)
    if plus:
        br = d * 0.14
        bx, by = cx + d * 0.34, cy + d * 0.34
        circle(r, bx, by, br + 3, fill=gap_color)
        circle(r, bx, by, br, fill=col(r, "app_black"))
        icon_c(r, "plus", bx, by, br * 1.25, (255, 255, 255), stroke=14)


def price_bar(r, box, left: str, right: str = "CONTRATAR", size: int = 30) -> None:
    """Barra cinza do post: valor/unidade à esquerda e CONTRATAR à direita."""
    x0, y0, x1, y1 = [int(v) for v in box]
    r.draw.rectangle((x0, y0, x1, y1), fill=col(r, "app_bar"))
    f = r.font("bold", size)
    pad = int(size * 1.0)
    cy = (y0 + y1) / 2
    r.draw.text((x0 + pad, cy), left, font=f, fill=(255, 255, 255), anchor="lm")
    r.draw.text((x1 - pad, cy), right, font=f, fill=(255, 255, 255), anchor="rm")
    r._place("price_bar", (x0, y0, x1, y1), size, 1)


def actions(r, x: float, cy: float, size: int = 56, color: RGB = (10, 10, 10), liked: bool = True) -> int:
    """Coração, comentário e enviar. Retorna a coordenada x do fim."""
    step = int(size * 1.55)
    icon_c(r, "heart", x + size / 2, cy, size, col(r, "app_like") if liked else color, fill=liked, stroke=8)
    icon_c(r, "comment", x + step + size / 2, cy, size * 0.95, color, stroke=7)
    icon_c(r, "send", x + 2 * step + size / 2, cy, size * 0.95, color, stroke=7)
    return int(x + 2 * step + size)


def app_tile(r, x: float, y: float, s: float) -> None:
    """Ícone do app: quadrado arredondado rosa com pin branco (como no toast do mapa)."""
    round_rect(r, (x, y, x + s, y + s), s * 0.26, fill=col(r, "app_pink"))
    icon_c(r, "pin", x + s / 2, y + s / 2 - s * 0.02, s * 0.62, (255, 255, 255), stroke=9, fill=False)


def toast(r, x: float, y: float, w: float, text: str, size: int = 28, name: str = "toast") -> tuple[int, int]:
    """Aviso flutuante branco com ícone do app ("Localização encontrada!")."""
    h = int(size * 2.6)
    shadow(r, (x, y, x + w, y + h), h * 0.3, blur=14, alpha=60, dy=8)
    round_rect(r, (x, y, x + w, y + h), h * 0.3, fill=(250, 250, 250))
    ts = h * 0.56
    app_tile(r, x + h * 0.22, y + (h - ts) / 2, ts)
    f = r.font("medium", size)
    r.draw.text((x + h * 0.22 + ts + size * 0.7, y + h / 2), text, font=f, fill=(78, 78, 82), anchor="lm")
    r._place(name, (x, y, x + w, y + h), size, 1)
    return int(w), h


def toast_width(r, text: str, size: int = 28) -> int:
    h = int(size * 2.6)
    return int(h * 0.22 + h * 0.56 + size * 0.7 + text_width(r.font("medium", size), text) + size * 1.2)


def map_pin(r, cx: float, tip_y: float, w: float, color: RGB, label: str | None = None, face: bool = False) -> None:
    """Pin do mapa (gota com borda branca). ``label`` = inicial do prestador; ``face`` = avatar."""
    rad = w / 2
    cy = tip_y - w * 1.0
    # sombra suave
    sh = Image.new("RGBA", (int(w * 2), int(w * 2)), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((w * 0.78, w * 1.69, w * 1.22, w * 1.81), fill=(0, 0, 0, 90))
    sh = sh.filter(ImageFilter.GaussianBlur(w * 0.06))
    r.img.alpha_composite(sh, (int(cx - w), int(tip_y - w * 1.75)))
    border = max(3.0, w * 0.07)
    # contorno branco e corpo colorido (coordenadas locais -> em 4x)
    ox, oy = cx - rad - border, cy - rad - border
    tw, th = w + 2 * border, w * 1.5 + 2 * border

    def paint(d):
        out = [(px * SS, py * SS) for px, py in teardrop(rad + border, rad + border, rad + border, w * 1.3 + border + 2)]
        d.polygon(out, fill=(255, 255, 255, 255))
        inn = [(px * SS, py * SS) for px, py in teardrop(rad + border, rad + border, rad, w * 1.3 + border - 2)]
        d.polygon(inn, fill=tuple(color) + (255,))
        d.ellipse(((rad + border - rad * 0.7) * SS, (rad + border - rad * 0.7) * SS,
                   (rad + border + rad * 0.7) * SS, (rad + border + rad * 0.7) * SS),
                  fill=(255, 255, 255, 255) if face else None, outline=(255, 255, 255, 255) if not face else None,
                  width=int(max(2.0, w * 0.05) * SS))

    blit(r, ox, oy, tw, th, paint)
    if face:
        avatar(r, cx, cy, rad * 1.34)
    elif label:
        r.draw.text((cx, cy), label, font=r.font("bold", int(w * 0.36)), fill=(255, 255, 255), anchor="mm")


def bottom_nav(r, y: float, h: float, color: RGB, inactive: RGB | None = None, bg: RGB | None = None,
               active: int = 0, x_range: tuple[float, float] | None = None) -> None:
    """Barra de navegação do app: início, busca, mais, explorar e perfil."""
    xa, xb = x_range if x_range else (r.m, r.W - r.m)
    names = ["home", "search", "plus", "compass", "user_badge"]
    inactive = inactive or mix(color, bg or (255, 255, 255), 0.55)
    if bg is not None:
        r.draw.rectangle((int(xa if x_range else 0), int(y), int(xb if x_range else r.W), int(y + h)), fill=bg)
    span = xb - xa
    s = h * 0.46
    for i, n in enumerate(names):
        cx = xa + span * (i + 0.5) / len(names)
        c = color if i == active else inactive
        icon_c(r, n, cx, y + h / 2, s * (1.05 if n == "user_badge" else 1.0), c, stroke=8 if i == active else 7)
    r._place("bottom_nav", (xa, y, xb, y + h), 0, 1)


def counter(r, right: float, top: float, text: str = "1/3", size: int = 26) -> None:
    """Contador de carrossel do post (canto superior direito da imagem)."""
    f = r.font("bold", size)
    w = int(text_width(f, text)) + int(size * 1.5)
    h = int(size * 1.9)
    round_rect(r, (right - w, top, right, top + h), h / 2, fill=(30, 0, 8), alpha=170)
    r.draw.text((right - w / 2, top + h / 2), text, font=f, fill=(255, 255, 255), anchor="mm")
    r._place("counter", (right - w, top, right, top + h), size, 1)


def location_row(r, x: float, cy: float, text: str, size: int = 28, color: RGB = (255, 255, 255)) -> int:
    """Ícone de pin + texto (como a linha "Cidade" do perfil). Retorna o x final."""
    icon_c(r, "pin", x + size * 0.55, cy, size * 1.2, color, stroke=8)
    f = r.font("semibold", size)
    r.draw.text((x + size * 1.35, cy), text, font=f, fill=color, anchor="lm")
    return int(x + size * 1.35 + text_width(f, text))


# ------------------------------------------------------------------ mock de tela
def phone_mock(r, box, accent: RGB, photo_a: RGB, photo_b: RGB) -> None:
    """Celular com a tela inicial do app (Doova, stories, post com barra de contratação e navegação).

    Textos de pessoas e preços são representados por barras neutras — nada de dados inventados.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    W, H = x1 - x0, y1 - y0
    n_placed = len(r.placed)
    shadow(r, (x0, y0, x1, y1), 56, blur=22, alpha=70, dy=16)
    round_rect(r, (x0, y0, x1, y1), 56, fill=(16, 16, 18))
    bz = 11
    sx0, sy0, sx1, sy1 = x0 + bz, y0 + bz, x1 - bz, y1 - bz
    round_rect(r, (sx0, sy0, sx1, sy1), 46, fill=(255, 255, 255))
    sw = sx1 - sx0
    # recorta o conteúdo no formato da tela
    base = r.img
    r.img = Image.new("RGBA", base.size, (0, 0, 0, 0))
    r.draw = ImageDraw.Draw(r.img)
    y = sy0 + 34
    # cabeçalho
    icon_c(r, "pin", sx0 + 34, y + 22, 26, (10, 10, 10), stroke=8)
    r.draw.text(((sx0 + sx1) / 2, y + 22), "Doova", font=r.font("bold", 26), fill=(10, 10, 10), anchor="mm")
    icon_c(r, "heart", sx1 - 34, y + 22, 26, (10, 10, 10), stroke=8)
    y += 58
    # stories
    story(r, sx0 + 52, y + 36, 62, ring=False, plus=True)
    story(r, sx0 + 128, y + 36, 62, ring=True)
    story(r, sx0 + 204, y + 36, 62, ring=True)
    y += 86
    r.draw.rectangle((sx0, y, sx1, y), fill=(236, 236, 236))
    y += 12
    # cabeçalho do post
    avatar(r, sx0 + 34, y + 22, 36)
    r.draw.rounded_rectangle((sx0 + 62, y + 10, sx0 + 62 + 104, y + 20), radius=5, fill=(40, 40, 44))
    r.draw.rounded_rectangle((sx0 + 62, y + 28, sx0 + 62 + 66, y + 36), radius=4, fill=(190, 190, 194))
    for k in range(3):
        circle(r, sx1 - 26, y + 14 + k * 8, 2.2, fill=(10, 10, 10))
    y += 50
    # imagem do post (gradiente neutro)
    ph = 190
    grad = Image.linear_gradient("L").resize((sw, ph)).rotate(0)
    a = Image.new("RGBA", (sw, ph), tuple(photo_a) + (255,))
    b = Image.new("RGBA", (sw, ph), tuple(photo_b) + (255,))
    r.img.alpha_composite(Image.composite(b, a, grad), (sx0, y))
    r.draw = ImageDraw.Draw(r.img)
    counter(r, sx1 - 12, y + 12, "1/3", size=15)
    y += ph
    price_bar(r, (sx0, y, sx1, y + 34), "DIÁRIA", "CONTRATAR", size=15)
    y += 34
    actions(r, sx0 + 16, y + 28, size=26)
    r.draw.rounded_rectangle((sx0 + 18, y + 52, sx0 + 18 + 88, y + 62), radius=5, fill=(40, 40, 44))
    r.draw.rounded_rectangle((sx0 + 18, y + 70, sx0 + 18 + 150, y + 78), radius=4, fill=(190, 190, 194))
    # navegação
    ny = sy1 - 62
    bottom_nav(r, ny, 62, (10, 10, 10), inactive=(150, 150, 154), bg=(255, 255, 255), x_range=(sx0 + 6, sx1 - 6))
    # máscara arredondada da tela
    mask = Image.new("L", r.img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((sx0, sy0, sx1, sy1), radius=46, fill=255)
    layer = r.img
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", layer.size, 0), mask))
    r.img = base
    r.draw = ImageDraw.Draw(r.img)
    r.img.alpha_composite(layer)
    # ilha da câmera
    round_rect(r, ((sx0 + sx1) / 2 - 34, sy0 + 10, (sx0 + sx1) / 2 + 34, sy0 + 26), 8, fill=(16, 16, 18))
    del r.placed[n_placed:]  # elementos internos do mock não entram no relatório de layout
    r._place("phone_mock", (x0, y0, x1, y1), 0, 1)
