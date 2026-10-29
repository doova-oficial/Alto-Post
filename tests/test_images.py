from itertools import combinations

import pytest
from PIL import Image

from app.services.fonts import FontResolver
from app.services.image_generator import generate, render
from app.services.template_config import contrast_ratio, get_template_config, hex_to_rgb
from app.services.text_layout import TextOverflowError, wrap_text

TEXT_ELEMENTS_EXCLUDED = {"logo"}


def _photo(kind: str = "busy") -> Image.Image:
    from PIL import ImageDraw

    if kind == "white":
        return Image.new("RGB", (1400, 1000), (255, 255, 255))
    if kind == "black":
        return Image.new("RGB", (1400, 1000), (0, 0, 0))
    im = Image.new("RGB", (1400, 1000), (240, 230, 200))
    d = ImageDraw.Draw(im)
    for i in range(0, 1400, 70):
        d.rectangle((i, 0, i + 35, 1000), fill=(255, 255, 255) if (i // 70) % 2 else (40, 80, 160))
    return im


def _ph(cfg, key):
    return _photo() if cfg.requires_photo(key) else None


def _ov(cfg, key):
    return {"photo_credit": "Foto: Teste / Openverse (CC BY 2.0)"} if cfg.requires_photo(key) else {}


def _intersect(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


@pytest.mark.parametrize("key", get_template_config().keys())
def test_all_templates_render_1080x1350(tmp_path, key):
    cfg = get_template_config()
    img = generate(cfg.resolve(key, _ov(cfg, key)), tmp_path / key, photo=_ph(cfg, key))
    assert (img.width, img.height) == (1080, 1350)
    assert img.png_path.exists() and img.jpg_path.exists()
    assert Image.open(img.png_path).format == "PNG"
    assert img.jpg_bytes[:3] == b"\xff\xd8\xff"  # JPEG (único formato aceito pela Meta)
    rep = img.report
    assert rep["warnings"] == []
    assert rep["min_contrast_text_vs_background"] >= 4.5  # WCAG AA
    boxes = [e for e in rep["elements"] if e["name"] not in TEXT_ELEMENTS_EXCLUDED]
    for e in rep["elements"]:
        x0, y0, x1, y1 = e["box"]
        assert 0 <= x0 < x1 <= 1080 and 0 <= y0 < y1 <= 1350, e
    for a, b in combinations(boxes, 2):
        assert not _intersect(a["box"], b["box"]), (key, a["name"], b["name"])


def test_templates_are_visually_distinct(tmp_path):
    cfg = get_template_config()
    small = [render(cfg.resolve(k), photo=_ph(cfg, k)).image.resize((54, 68)).tobytes() for k in cfg.keys()]
    assert len(set(small)) == len(small) == len(cfg.keys())


def test_doova_colors(tmp_path):
    cfg = get_template_config()
    assert cfg.palette["pink"] == "#FF00D0" and cfg.palette["red"] == "#FF0000"
    pink = render(cfg.resolve("autonomos", {"shapes": False})).image
    assert pink.getpixel((20, 700)) == hex_to_rgb("#FF00D0")
    red = render(cfg.resolve("contratantes", {"shapes": False})).image
    assert red.getpixel((20, 700)) == hex_to_rgb("#FF0000")


def test_colors_are_configurable():
    cfg = get_template_config()
    img = render(cfg.resolve("autonomos", {"shapes": False, "primary_color": "#123456"})).image
    assert img.getpixel((20, 700)) == (0x12, 0x34, 0x56)


def test_long_title_shrinks_and_stays_inside(tmp_path):
    cfg = get_template_config()
    title = "Profissionais de Iporá que fazem reformas, consertos, limpeza e aulas agora podem ser encontrados por quem precisa"
    res = render(cfg.resolve("beneficios", {"title": title, "items": ["Item um", "Item dois", "Item três"]}))
    assert res.report["scale_used"] < 1.0
    t = next(e for e in res.report["elements"] if e["name"] == "title")
    assert t["box"][2] <= 1080 - 84 + 1 and t["box"][0] >= 84 - 1
    assert t["lines"] <= 5


def test_text_that_cannot_fit_is_refused_not_clipped():
    cfg = get_template_config()
    params = cfg.resolve("funcionalidades", {
        "title": "Título extremamente longo " * 6,
        "subtitle": "Subtítulo também bastante longo para ocupar muito espaço na arte " * 3,
        "items": ["Item com bastante texto para ocupar várias linhas dentro da arte de teste"] * 4,
    })
    with pytest.raises(TextOverflowError):
        render(params)


def test_wrap_hyphenates_words_longer_than_line():
    font = FontResolver().get("bold", 60)
    lines = wrap_text("Paralelepipedoextraordinariamenteenorme ok", font, 300)
    assert len(lines) > 1
    assert all(font.getlength(ln) <= 300 for ln in lines)


def test_font_fallback_without_any_font_files(tmp_path):
    resolver = FontResolver(use_bundled=False, use_system=False)
    res = render(get_template_config().resolve("institucional"), fonts=resolver)
    assert resolver.fallback_used is True
    assert res.report["font_fallback_used"] is True
    assert res.image.size == (1080, 1350)


def test_bundled_fonts_found_without_system_fonts():
    r = FontResolver(use_system=False)
    assert all(v != "pillow-default" and "assets" in v for v in r.describe().values())


def test_invalid_color_rejected():
    with pytest.raises(ValueError):
        get_template_config().resolve("autonomos", {"primary_color": "rosa"})


def test_auto_text_color_has_contrast():
    cfg = get_template_config()
    for key in cfg.keys():
        rep = render(cfg.resolve(key), photo=_ph(cfg, key)).report
        assert rep["min_contrast_text_vs_background"] >= 4.5, key
    assert contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21, rel=1e-3)