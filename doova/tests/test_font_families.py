from app.services.fonts import FontResolver
from app.services.image_generator import render
from app.services.template_config import get_template_config


def test_distinct_font_families_are_bundled_and_resolved():
    resolver = FontResolver(use_system=False)
    for family in ("inter", "condensed", "serif_display", "slab", "lato"):
        path = resolver.path_for("bold", family)
        assert path is not None
        assert "assets" in str(path)


def test_creative_styles_select_distinct_title_families():
    cfg = get_template_config()
    expected = {
        "editorial": "serif_display",
        "brutalist": "lato",
        "collage": "condensed",
    }
    for style, family in expected.items():
        params = cfg.resolve("beneficios", {
            "creative_style": style,
            "title": "Seu trabalho merece ser encontrado",
            "subtitle": "Conecte-se a pessoas que precisam do seu serviço.",
        })
        result = render(params, cfg)
        assert result.report["font_families"]["title"] == family


def test_font_family_can_be_overridden_per_role():
    cfg = get_template_config()
    params = cfg.resolve("beneficios", {
        "creative_style": "editorial",
        "title": "Título personalizado",
        "title_font_family": "condensed",
        "body_font_family": "slab",
    })
    result = render(params, cfg)
    assert result.report["font_families"]["title"] == "condensed"
    assert result.report["font_families"]["body"] == "slab"
