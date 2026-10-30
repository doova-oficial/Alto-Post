"""Monta a folha de revisão das artes do plano de lançamento e um ZIP com os JPEGs."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import init_engine, session_scope  # noqa: E402
from app.models import Post  # noqa: E402
from app.services.library import launch_plan  # noqa: E402

OUT = ROOT / "generated" / "revisao_lancamento"


def font(size: int, bold: bool = False):
    fonts = sorted((ROOT / "app" / "assets" / "fonts").glob("*.ttf"))
    pick = [f for f in fonts if ("Bold" in f.name) == bold] or fonts
    return ImageFont.truetype(str(pick[0]), size) if pick else ImageFont.load_default()


def main() -> None:
    init_engine(get_settings().database_url)
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    with session_scope() as db:
        for order, (key, theme) in enumerate(launch_plan(), start=1):
            p = db.scalar(select(Post).where(Post.theme_key == key))
            if not p:
                print("faltando", key)
                continue
            rep = p.artwork.layout_report or {}
            rows.append({
                "ordem": order, "tema": key, "modelo": theme["template"], "post_id": p.id, "status": p.status,
                "png": p.artwork.png_path, "jpg": p.artwork.jpg_path,
                "escala": rep.get("scale_used"), "avisos": rep.get("warnings"),
                "contraste_min": rep.get("min_contrast_text_vs_background"),
                "titulo": theme.get("title"),
            })
    tw, th, label_h, gap, cols = 360, 450, 56, 14, 4
    nrows = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (tw + gap) + gap, nrows * (th + label_h + gap) + gap), "white")
    d = ImageDraw.Draw(sheet)
    fb, fr = font(20, True), font(16)
    for i, r in enumerate(rows):
        x = gap + (i % cols) * (tw + gap)
        y = gap + (i // cols) * (th + label_h + gap)
        sheet.paste(Image.open(r["png"]).convert("RGB").resize((tw, th), Image.LANCZOS), (x, y))
        d.text((x, y + th + 6), f"{r['ordem']:02d} · {r['tema']}", fill="#1C1020", font=fb)
        d.text((x, y + th + 32), f"modelo: {r['modelo']}", fill="#6B5A68", font=fr)
    sheet_path = OUT / "folha_lancamento.png"
    sheet.save(sheet_path, optimize=True)
    zip_path = OUT / "artes_lancamento.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            z.write(r["jpg"], f"{r['ordem']:02d}_{r['tema']}.jpg")
    (OUT / "resumo.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(r["ordem"], r["tema"], r["modelo"], r["status"], "escala", r["escala"], "contraste", r["contraste_min"], "avisos", r["avisos"])
    print("folha", sheet_path, sheet.size, "zip", zip_path, zip_path.stat().st_size)


if __name__ == "__main__":
    main()