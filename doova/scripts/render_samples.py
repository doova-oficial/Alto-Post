"""Renderiza uma amostra de cada template em generated/amostras/ (não cria publicações)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app.services.image_generator import generate  # noqa: E402
from app.services.template_config import get_template_config  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "generated" / "amostras"

SAMPLE_OVERRIDES = {
    "beneficios": {"items": ["Encontre prestadores da sua região", "Divulgue o serviço que você oferece",
                             "Converse e combine direto com a outra pessoa"]},
}


def placeholder_photo() -> Image.Image:
    """Foto sintética só para a amostra (as publicações usam fotos reais do Openverse/Pixabay)."""
    from PIL import ImageDraw

    im = Image.new("RGB", (1600, 1200), (96, 110, 124))
    d = ImageDraw.Draw(im)
    for i in range(0, 1600, 80):
        d.rectangle((i, 0, i + 40, 1200), fill=(110, 126, 140))
    d.ellipse((500, 250, 1100, 850), fill=(222, 214, 196))
    return im


def main() -> None:
    cfg = get_template_config()
    thumbs = []
    for key in cfg.keys():
        needs = cfg.requires_photo(key)
        ov = dict(SAMPLE_OVERRIDES.get(key, {}), **({"photo_credit": "Foto ilustrativa de exemplo"} if needs else {}))
        params = cfg.resolve(key, ov)
        img = generate(params, OUT / key, photo=placeholder_photo() if needs else None)
        print(key, img.png_path, img.report["scale_used"], img.report["warnings"],
              img.report["min_contrast_text_vs_background"])
        thumbs.append(Image.open(img.png_path))
    # folha de contato 4x2
    tw, th = 360, 450
    rows = (len(thumbs) + 3) // 4
    sheet = Image.new("RGB", (tw * 4 + 50, th * rows + 10 * (rows + 1)), "white")
    for i, im in enumerate(thumbs):
        sheet.paste(im.resize((tw, th)), (10 + (i % 4) * (tw + 10), 10 + (i // 4) * (th + 10)))
    sheet.save(OUT / "folha_de_contato.png")
    print("folha", OUT / "folha_de_contato.png")


if __name__ == "__main__":
    main()