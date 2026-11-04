"""Busca vídeos gratuitos e cria reels verticais com gancho, narração e legendas queimadas."""
from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import logging
import shutil
import subprocess
import tempfile
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Post, PostStatus, VideoAsset

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
logger = logging.getLogger("app")

DOOVA_RED = (255, 0, 0)
DOOVA_MAGENTA = (255, 0, 208)

VIDEO_CREATIVE_SPECS: dict[str, dict[str, Any]] = {
    "clean_center": {
        "name": "texto central sem imagem",
        "text_position": "center",
        "has_context_image": False,
        "background": "brand_gradient",
        "cta_position": "bottom",
    },
    "context_photo_bottom": {
        "name": "foto contextual com texto inferior",
        "text_position": "bottom",
        "has_context_image": True,
        "background": "context_photo",
        "cta_position": "bottom",
    },
    "hook_top": {
        "name": "gancho no topo sem imagem",
        "text_position": "top",
        "has_context_image": False,
        "background": "solid_brand",
        "cta_position": "bottom",
    },
    "photo_scrim_top": {
        "name": "foto contextual com gancho no topo",
        "text_position": "top",
        "has_context_image": True,
        "background": "context_photo",
        "cta_position": "bottom",
    },
    "badge_quote": {
        "name": "selo com citação central",
        "text_position": "center",
        "has_context_image": False,
        "background": "solid_brand",
        "cta_position": "bottom",
    },
    "split_block": {
        "name": "bloco dividido com barra de destaque",
        "text_position": "center",
        "has_context_image": False,
        "background": "brand_gradient",
        "cta_position": "bottom",
    },
    "editorial_split": {
        "name": "editorial assimétrico",
        "text_position": "top",
        "has_context_image": True,
        "background": "context_photo",
        "cta_position": "bottom",
    },
    "brutalist_type": {
        "name": "tipografia brutalista",
        "text_position": "center",
        "has_context_image": False,
        "background": "solid_brand",
        "cta_position": "bottom",
    },
    "collage_badge": {
        "name": "colagem com selo pop",
        "text_position": "center",
        "has_context_image": True,
        "background": "context_photo",
        "cta_position": "bottom",
    },
    "minimal_quote": {
        "name": "minimalista com frase em destaque",
        "text_position": "center",
        "has_context_image": False,
        "background": "brand_gradient",
        "cta_position": "bottom",
    },
    "photo_frame": {
        "name": "fotografia emoldurada",
        "text_position": "bottom",
        "has_context_image": True,
        "background": "context_photo",
        "cta_position": "bottom",
    },
    "kinetic_band": {
        "name": "faixas diagonais de impacto",
        "text_position": "center",
        "has_context_image": False,
        "background": "solid_brand",
        "cta_position": "bottom",
    },
}

# Nomes válidos do filtro xfade do ffmpeg usados como níveis do fator "transição".
VIDEO_TRANSITIONS: tuple[str, ...] = ("fade", "wipeleft", "circleopen", "slideup")

# Fatores de retenção testáveis em vídeo, no mesmo espírito do FACTORS de
# experiment_service.py (app/services/experiment_service.py) para fotos: cada um
# vira um nível explícito passado a create_video(), nunca um palpite da IA.
MAX_VIDEO_COMBINATIONS = 8
VIDEO_FACTORS: dict[str, dict[str, Any]] = {
    "video_variant": {"allowed": list(VIDEO_CREATIVE_SPECS)},
    "video_transition": {"allowed": list(VIDEO_TRANSITIONS)},
    "video_duration_seconds": {"type": int, "min": 5, "max": 60},
    "video_generated_image_count": {"type": int, "min": 0, "max": 4},
}


def validate_video_factors(factors: dict[str, list]) -> None:
    if not factors:
        raise VideoSearchError("informe ao menos um fator de vídeo")
    for name, levels in factors.items():
        spec = VIDEO_FACTORS.get(name)
        if spec is None:
            raise VideoSearchError(f"fator de vídeo desconhecido: {name}; use um de {sorted(VIDEO_FACTORS)}")
        if not isinstance(levels, list) or len(levels) < 2 or len(set(map(str, levels))) != len(levels):
            raise VideoSearchError(f"o fator {name} precisa de pelo menos dois níveis distintos")
        if "allowed" in spec and any(l not in spec["allowed"] for l in levels):
            raise VideoSearchError(f"níveis inválidos para {name}: use {spec['allowed']}")
        if spec.get("type") is int:
            if any(not isinstance(l, int) for l in levels):
                raise VideoSearchError(f"o fator {name} aceita somente números inteiros")
            lo, hi = spec.get("min"), spec.get("max")
            if any(not (lo <= l <= hi) for l in levels):
                raise VideoSearchError(f"{name} deve estar entre {lo} e {hi}")
    combos = 1
    for levels in factors.values():
        combos *= len(levels)
    if combos > MAX_VIDEO_COMBINATIONS:
        raise VideoSearchError(f"{combos} combinações; o máximo é {MAX_VIDEO_COMBINATIONS}. "
                               "Teste menos fatores por vez.")


def video_combinations(factors: dict[str, list]) -> list[dict[str, Any]]:
    """Mesmo produto cartesiano de experiment_service.combinations(), para vídeo."""
    names = sorted(factors)
    return [dict(zip(names, vals)) for vals in itertools.product(*(factors[n] for n in names))]


class VideoSearchError(RuntimeError):
    pass


class VideoPublishError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


class VideoDeleteError(RuntimeError):
    pass


@dataclass
class VideoCandidate:
    provider: str
    provider_id: str
    title: str
    creator: str
    landing_url: str
    download_url: str
    width: int
    height: int
    duration: float
    query: str


# Textos curtos, concretos e sem afirmar que a tendência é uma notícia sobre o Doova.
HOOKS: dict[str, list[str]] = {
    "calor": ["O calor apertou?", "Antes de passar sufoco...", "Seu ar está preparado?"],
    "chuva": ["Choveu e apareceu goteira?", "Não deixe a chuva virar prejuízo.", "Olha esse sinal antes da próxima chuva."],
    "festas": ["Vai ter festa?", "A data está chegando.", "Sua festa merece ajuda."],
    "estudos": ["A prova está chegando?", "Travou naquela matéria?", "Estudar sozinho nem sempre resolve."],
    "energia": ["A energia caiu de novo?", "Problema elétrico não espera.", "Precisa de eletricista?"],
    "agua": ["Vazamento não se resolve sozinho.", "Cano entupido?", "Achou um vazamento?"],
    "pets": ["Seu pet precisa de cuidado?", "Banho e tosa na agenda?", "Cuidar também é rotina."],
    "autonomos": ["Você trabalha por conta?", "Seu serviço precisa ser encontrado.", "Mais gente pode conhecer seu trabalho."],
    "limpeza": ["A casa pediu socorro?", "Faxina acumulou?", "Tempo curto para limpar?"],
    "jardim": ["O quintal cresceu?", "Seu jardim pede atenção.", "Capina ou poda na lista?"],
    "veiculos": ["Seu carro deu sinal?", "Revisão atrasada?", "Não espere o problema aumentar."],
    "reforma": ["A reforma saiu do papel?", "Tem reparo pendente?", "Sua casa pode mudar."],
}


def _client(settings: Settings, client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client:
        return client, False
    return httpx.Client(timeout=settings.video_timeout_seconds, follow_redirects=True), True


# teto de largura do candidato: baixar 4K só pra reescalar pra 1080x1920 estoura RAM
# em planos pequenos (512MB) à toa — o maior lado do vídeo final já cobre o necessário.
VIDEO_CANDIDATE_MAX_WIDTH = 1280
VIDEO_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "chuva": ("rain", "rain drops", "roof repair"),
    "calor": ("hot weather", "air conditioner repair"),
    "festas": ("party food", "party decoration"),
    "estudos": ("student studying", "tutor"),
    "energia": ("electrician working", "electrical repair"),
    "agua": ("plumber", "water leak"),
    "pets": ("dog grooming", "pet care"),
    "limpeza": ("house cleaning", "cleaning service"),
    "jardim": ("gardener", "lawn mowing"),
    "veiculos": ("car mechanic", "auto repair"),
    "reforma": ("house renovation", "house painter"),
}


def _video_query_variants(query: str) -> list[str]:
    """Gera consultas de stock progressivamente mais amplas, sem trocar o provedor."""
    q = " ".join((query or "").split())[:100]
    variants = [q]
    words = q.casefold().split()
    for key, aliases in VIDEO_QUERY_ALIASES.items():
        if any(word == key or word.startswith(key) for word in words):
            if len(words) > 1:
                variants.append(key)
            variants.extend(aliases)
            break
    return list(dict.fromkeys(item for item in variants if len(item) >= 2))


def _wrap_video_image_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
                           max_width: int) -> str:
    words = (text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines) or "Doova"


def _brand_gradient(width: int, height: int, index: int) -> Image.Image:
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    top = DOOVA_RED if index % 2 == 0 else DOOVA_MAGENTA
    bottom = DOOVA_MAGENTA if index % 2 == 0 else (35, 0, 45)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(round(top[channel] * (1 - ratio) + bottom[channel] * ratio) for channel in range(3))
        draw.line((0, y, width, y), fill=color)
    return image


def _brand_chip(draw: ImageDraw.ImageDraw, margin: int, label_font: ImageFont.ImageFont,
                footer_font: ImageFont.ImageFont, theme: str, trend_term: str,
                light: bool = False) -> None:
    """Desenha o selo DOOVA + tema no topo, igual em toda variante, pra manter identidade visual."""
    text_color = (28, 16, 32, 255) if light else (255, 255, 255, 255)
    chip_color = (255, 255, 255, 230) if light else (255, 255, 255, 40)
    draw.rounded_rectangle((margin, 70, margin + 170, 70 + 56), radius=16, fill=chip_color)
    draw.text((margin + 20, 86), "DOOVA", font=label_font, fill=text_color)
    draw.text((margin, 150), f"{theme.upper()}  •  {trend_term[:42]}", font=footer_font,
              fill=(28, 16, 32, 255) if light else (255, 229, 190, 255))


def _cta_bar(draw: ImageDraw.ImageDraw, width: int, height: int, margin: int,
            footer_font: ImageFont.ImageFont, cta: str = "Instale o app Doova") -> None:
    cta_box = (margin, height - 205, width - margin, height - 105)
    draw.rounded_rectangle(cta_box, radius=30, fill=(*DOOVA_MAGENTA, 255), outline=(*DOOVA_RED, 255), width=5)
    draw.text((width // 2, (cta_box[1] + cta_box[3]) // 2), cta, font=footer_font,
              fill=(255, 255, 255, 255), anchor="mm", align="center")


def _extract_context_frame(source: Path, out_path: Path, settings: Settings) -> None:
    """Extrai um frame do placeholder para a variante que usa contexto visual."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg nao instalado; impossivel extrair frame contextual")
    scale_filter = (f"scale={settings.video_width}:{settings.video_height}:"
                    f"force_original_aspect_ratio=increase,crop={settings.video_width}:{settings.video_height}")
    cmd = [ffmpeg, "-y", "-ss", "0.8", "-i", str(source), "-frames:v", "1",
           "-vf", scale_filter, "-q:v", "3", str(out_path)]
    _run_ffmpeg(cmd, out_path, settings)


def _generate_video_image(theme: str, trend_term: str, text: str, out_path: Path,
                          settings: Settings, index: int, variant: str = "clean_center",
                          background_path: Path | None = None) -> None:
    """Gera um criativo deterministico com layout/fatores mensuraveis.

    Nao usa IA generativa: cada variante e um esqueleto de campanha repetivel,
    para que a comparacao posterior identifique o que mudou. As 6 variantes de
    VIDEO_CREATIVE_SPECS cobrem 3 eixos: presenca de foto de contexto, posicao
    do texto (topo/centro/base) e composicao do cartao (solido/bloco/selo).
    """
    spec = VIDEO_CREATIVE_SPECS.get(variant)
    if spec is None:
        raise VideoSearchError(f"variante visual de video invalida: {variant}")
    width, height = settings.video_width, settings.video_height
    use_photo = bool(spec["has_context_image"] and background_path and background_path.exists())
    if use_photo:
        with Image.open(background_path) as source_image:
            image = ImageOps.fit(source_image.convert("RGB"), (width, height),
                                 method=Image.Resampling.LANCZOS)
        image = ImageEnhance.Brightness(image).enhance(0.5)
        image = ImageEnhance.Contrast(image).enhance(1.08)
    else:
        image = _brand_gradient(width, height, index)
    draw = ImageDraw.Draw(image, "RGBA")
    font_path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Inter-Bold.otf"
    extrabold_path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Inter-ExtraBold.otf"
    if font_path.exists():
        label_font = ImageFont.truetype(str(font_path), max(26, width // 32))
        body_font = ImageFont.truetype(str((extrabold_path if extrabold_path.exists() else font_path)),
                                       max(48, min(84, width // 13)))
        footer_font = ImageFont.truetype(str(font_path), max(26, width // 30))
    else:
        label_font = body_font = footer_font = ImageFont.load_default()
    margin = max(56, width // 12)
    body = _wrap_video_image_text(draw, text, body_font, width - 2 * margin - 80)

    if variant in ("context_photo_bottom", "photo_scrim_top"):
        # scrim em degrade (nao um retangulo solido) pra a foto continuar visivel
        band_top, band_bottom = (int(height * 0.50), height) if variant == "context_photo_bottom" \
            else (0, int(height * 0.46))
        for y in range(band_top, band_bottom):
            span = band_bottom - band_top
            ratio = (y - band_top) / max(span - 1, 1)
            ratio = ratio if variant == "context_photo_bottom" else 1 - ratio
            alpha = int(235 * min(1.0, ratio * 1.3))
            draw.line((0, y, width, y), fill=(10, 8, 20, alpha))
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        box_center_y = int(height * 0.74) if variant == "context_photo_bottom" else int(height * 0.22)
        draw.multiline_text((width // 2, box_center_y), body, font=body_font,
                            fill=(255, 255, 255, 255), anchor="mm", align="center", spacing=14)
    elif variant == "hook_top":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.rounded_rectangle((margin, 280, width - margin, int(height * 0.63)), radius=38,
                               fill=(255, 255, 255, 244))
        draw.rectangle((margin, 280, margin + 10, int(height * 0.63)), fill=(*DOOVA_RED, 255))
        draw.multiline_text((margin + 56, 390), body, font=body_font,
                            fill=(28, 16, 32, 255), align="left", spacing=14)
    elif variant == "badge_quote":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        quote_mark_font = ImageFont.truetype(str(font_path), max(80, width // 6)) if font_path.exists() else label_font
        draw.text((width // 2, int(height * 0.30)), "“", font=quote_mark_font,
                  fill=(255, 255, 255, 60), anchor="mm")
        draw.multiline_text((width // 2, height // 2), body, font=body_font,
                            fill=(255, 255, 255, 255), anchor="mm", align="center", spacing=16)
        draw.ellipse((width // 2 - 5, int(height * 0.58), width // 2 + 5, int(height * 0.58) + 10),
                     fill=(*DOOVA_RED, 255))
    elif variant == "split_block":
        split_y = int(height * 0.40)
        draw.rectangle((0, 0, width, split_y), fill=(12, 9, 24, 255))
        draw.rectangle((0, split_y, width, split_y + 10), fill=(*DOOVA_RED, 255))
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.multiline_text((width // 2, (split_y + height) // 2 - 60), body, font=body_font,
                            fill=(255, 255, 255, 255), anchor="mm", align="center", spacing=14)
    elif variant == "editorial_split":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        split = int(height * 0.48)
        draw.rectangle((0, split, width, height), fill=(12, 9, 24, 242))
        draw.rectangle((margin, int(height*.18), margin+16, split-32), fill=(*DOOVA_MAGENTA, 255))
        draw.multiline_text((margin+42, int(height*.22)), body, font=body_font,
                            fill=(255,255,255,255), spacing=14)
    elif variant == "brutalist_type":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.rectangle((0, int(height*.68), width, height), fill=(*DOOVA_RED, 255))
        draw.rectangle((margin, int(height*.27), width-margin, int(height*.29)),
                       fill=(*DOOVA_MAGENTA, 255))
        draw.multiline_text((margin, int(height*.36)), body, font=body_font,
                            fill=(255,255,255,255), spacing=14)
    elif variant == "collage_badge":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.polygon([(0,int(height*.28)),(width,int(height*.18)),
                      (width,int(height*.62)),(0,int(height*.72))], fill=(12,9,24,235))
        draw.ellipse((width-margin-145,int(height*.20),width-margin,int(height*.20)+145),
                     fill=(*DOOVA_RED,255))
        draw.multiline_text((margin,int(height*.37)),body,font=body_font,
                            fill=(255,255,255,255),spacing=14)
    elif variant == "minimal_quote":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.rectangle((margin,int(height*.25),margin+8,int(height*.70)),
                       fill=(*DOOVA_MAGENTA,255))
        draw.text((margin+32,int(height*.27)),"“",font=body_font,fill=(*DOOVA_RED,255))
        draw.multiline_text((margin+32,int(height*.40)),body,font=body_font,
                            fill=(255,255,255,255),spacing=16)
    elif variant == "photo_frame":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        pad = margin // 2
        draw.rounded_rectangle((pad,int(height*.12),width-pad,int(height*.82)),
                               radius=34,outline=(*DOOVA_MAGENTA,255),width=max(8,width//90))
        for y in range(int(height*.54),height):
            alpha = int(220*(y-height*.54)/max(1,height-int(height*.54)))
            draw.line((0,y,width,y),fill=(10,8,20,alpha))
        draw.multiline_text((margin,int(height*.61)),body,font=body_font,
                            fill=(255,255,255,255),spacing=14)
    elif variant == "kinetic_band":
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        for offset, alpha in ((0,255),(70,190),(140,120)):
            draw.polygon([(0,int(height*.28)+offset),(width,int(height*.18)+offset),
                          (width,int(height*.42)+offset),(0,int(height*.52)+offset)],
                         fill=(*DOOVA_RED,alpha))
        draw.multiline_text((margin,int(height*.37)),body,font=body_font,
                            fill=(255,255,255,255),spacing=16)
    else:  # clean_center (padrao)
        _brand_chip(draw, margin, label_font, footer_font, theme, trend_term)
        draw.rounded_rectangle((margin, 250, width - margin, height - 270), radius=38,
                               fill=(12, 9, 24, 238), outline=(255, 255, 255, 230), width=3)
        draw.multiline_text((width // 2, height // 2), body, font=body_font,
                            fill=(255, 255, 255, 255), anchor="mm", align="center", spacing=14)

    _cta_bar(draw, width, height, margin, footer_font)
    image.save(out_path, format="PNG", optimize=True)


def _choose_video_variant(db: Session, settings: Settings, existing: VideoAsset | None,
                          force: bool, requested: str | None = None) -> str:
    requested = requested or getattr(settings, "video_creative_variant", "auto")
    if requested != "auto":
        if requested not in VIDEO_CREATIVE_SPECS:
            raise VideoSearchError(f"variante visual de video invalida: {requested}")
        return requested
    keys = tuple(VIDEO_CREATIVE_SPECS)
    if existing and force:
        previous = (existing.metadata_json or {}).get("creative_variant")
        if previous in keys:
            return keys[(keys.index(previous) + 1) % len(keys)]
    total = db.scalar(select(func.count()).select_from(VideoAsset)) or 0
    return keys[int(total) % len(keys)]


def _choose_video_transition(db: Session, settings: Settings, existing: VideoAsset | None,
                             force: bool, requested: str | None = None) -> str:
    """Mesma lógica de rodízio de _choose_video_variant, aplicada ao fator transição."""
    requested = requested or getattr(settings, "video_transition", "auto")
    if requested != "auto":
        if requested not in VIDEO_TRANSITIONS:
            raise VideoSearchError(f"transição de vídeo inválida: {requested}; use uma de {VIDEO_TRANSITIONS}")
        return requested
    keys = VIDEO_TRANSITIONS
    if existing and force:
        previous = (existing.metadata_json or {}).get("transition")
        if previous in keys:
            return keys[(keys.index(previous) + 1) % len(keys)]
    total = db.scalar(select(func.count()).select_from(VideoAsset)) or 0
    return keys[int(total) % len(keys)]


def _candidate(raw: dict[str, Any], query: str, provider: str = "pixabay") -> VideoCandidate | None:
    videos = raw.get("videos") or {}
    # Pixabay devolve várias resoluções; evita tanto pixelado (muito pequeno) quanto
    # 4K desnecessário (decodificar 4K no ffmpeg consome muito mais RAM que o resultado exige).
    usable = [item for item in videos.values()
             if item.get("url") and 720 <= int(item.get("width") or 0) <= VIDEO_CANDIDATE_MAX_WIDTH]
    choices = sorted(usable, key=lambda x: int(x.get("width") or 0), reverse=True) or \
        sorted(videos.values(), key=lambda x: int(x.get("width") or 0))  # sem opção no teto: pega a menor disponível
    for item in choices:
        url = item.get("url")
        if url and int(item.get("width") or 0) >= 720:
            return VideoCandidate(provider, str(raw.get("id")), str(raw.get("tags") or query),
                                  str(raw.get("user") or "Pixabay"), str(raw.get("pageURL") or ""), url,
                                  int(item.get("width") or 0), int(item.get("height") or 0),
                                  float(raw.get("duration") or 0), query)
    return None


def _pexels_candidate(raw: dict[str, Any], query: str) -> VideoCandidate | None:
    """Converte a resposta do Pexels e escolhe um MP4 adequado ao encode."""
    files = [item for item in raw.get("video_files", [])
             if item.get("link") and (
                 str(item.get("file_type") or "mp4").casefold() in {"mp4", "video/mp4"}
             )]
    usable = [item for item in files
              if 720 <= int(item.get("width") or 0) <= VIDEO_CANDIDATE_MAX_WIDTH]
    choices = sorted(usable, key=lambda x: int(x.get("width") or 0), reverse=True) or \
        sorted(files, key=lambda x: int(x.get("width") or 0), reverse=True)
    for item in choices:
        width = int(item.get("width") or 0)
        if width < 720:
            continue
        return VideoCandidate(
            "pexels", str(raw.get("id")), str(raw.get("url") or query),
            str((raw.get("user") or {}).get("name") or "Pexels"),
            str(raw.get("url") or ""), str(item["link"]), width,
            int(item.get("height") or 0), float(raw.get("duration") or 0), query,
        )
    return None


def _search_pexels(query: str, settings: Settings, client: httpx.Client) -> list[VideoCandidate]:
    if not settings.pexels_configured:
        return []
    try:
        response = client.get(
            PEXELS_VIDEO_URL,
            headers={"Authorization": settings.pexels_api_key.get_secret_value()},
            # O render final faz crop para 9:16; exigir portrait aqui elimina
            # muitos resultados relevantes de chuva, reparos e serviços.
            params={"query": query, "per_page": 30},
            timeout=settings.video_timeout_seconds,
        )
        if response.status_code != 200:
            logger.warning("Pexels videos HTTP %s; tentando o fallback", response.status_code)
            return []
        return [candidate for raw in response.json().get("videos", [])
                if (candidate := _pexels_candidate(raw, query)) is not None]
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as e:
        logger.warning("Pexels videos indisponivel (%s); tentando o fallback", type(e).__name__)
        return []


def _search_pixabay(query: str, settings: Settings, client: httpx.Client) -> list[VideoCandidate]:
    if not settings.pixabay_configured:
        return []
    try:
        response = client.get(
            PIXABAY_VIDEO_URL,
            params={"key": settings.pixabay_api_key.get_secret_value(), "q": query,
                    "lang": "pt", "safesearch": "true", "per_page": 30},
            timeout=settings.video_timeout_seconds,
        )
        if response.status_code != 200:
            logger.warning("Pixabay videos HTTP %s", response.status_code)
            return []
        return [candidate for raw in response.json().get("hits", [])
                if (candidate := _candidate(raw, query)) is not None]
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as e:
        logger.warning("Pixabay videos indisponivel: %s", type(e).__name__)
        return []


# Busca Pexels primeiro e cai para Pixabay quando o provedor principal não
# retorna candidatos adequados.
def search_videos(query: str, settings: Settings | None = None,
                  client: httpx.Client | None = None) -> list[VideoCandidate]:
    s = settings or get_settings()
    if not s.video_enabled:
        raise VideoSearchError("videos desativados (VIDEO_ENABLED=false)")
    if not s.video_search_configured:
        raise VideoSearchError("PEXELS_API_KEY ou PIXABAY_API_KEY e necessaria para buscar videos gratuitos")
    q = " ".join((query or "").split())[:100]
    if len(q) < 2:
        raise VideoSearchError("termo de video vazio")
    c, own = _client(s, client)
    try:
        providers = [s.video_provider]
        if s.video_provider == "pexels" and s.video_pixabay_fallback_enabled:
            providers.append("pixabay")
        attempted_queries = _video_query_variants(q)
        for search_query in attempted_queries:
            for provider in providers:
                candidates = (_search_pexels(search_query, s, c) if provider == "pexels"
                              else _search_pixabay(search_query, s, c))
                if candidates:
                    return candidates
        raise VideoSearchError(
            f"nenhum vídeo encontrado para '{q}' nos provedores configurados "
            f"(consultas tentadas: {', '.join(attempted_queries)})"
        )
    finally:
        if own:
            c.close()


def _download(candidate: VideoCandidate, settings: Settings, client: httpx.Client | None,
              dest: Path) -> int:
    """Baixa em streaming direto pro disco (dest), sem acumular o arquivo inteiro em RAM.
    Aborta assim que ultrapassa o limite configurado, sem baixar o resto. Retorna o tamanho em bytes."""
    c, own = _client(settings, client)
    total = 0
    try:
        with c.stream("GET", candidate.download_url, timeout=settings.video_timeout_seconds) as r:
            if r.status_code != 200:
                raise VideoSearchError(f"download do vídeo falhou (HTTP {r.status_code})")
            with dest.open("wb") as f:
                first_chunk = True
                for chunk in r.iter_bytes(chunk_size=256 * 1024):
                    if first_chunk:
                        if not chunk.startswith(b"\x00\x00\x00"):
                            raise VideoSearchError("o banco não devolveu um MP4 válido")
                        first_chunk = False
                    total += len(chunk)
                    if total > settings.video_max_download_bytes:
                        raise VideoSearchError("vídeo maior que o limite configurado")
                    f.write(chunk)
        return total
    finally:
        if own:
            c.close()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_duration(path: Path, timeout: float) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise VideoSearchError("ffprobe não instalado (vem junto do pacote ffmpeg)")
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise VideoSearchError(f"ffprobe falhou: {result.stderr.decode(errors='replace')[-300:]}")
    try:
        return float(json.loads(result.stdout).get("format", {}).get("duration") or 0)
    except (ValueError, json.JSONDecodeError) as e:
        raise VideoSearchError(f"ffprobe devolveu formato inesperado: {e}") from e


def _tts_gtts(text: str, language: str, tld: str, slow: bool,
              out_path: Path, timeout: float) -> None:
    from gtts import gTTS

    async def _run() -> None:
        tts = gTTS(text=text, lang=language, tld=tld, slow=slow)
        await asyncio.to_thread(tts.save, str(out_path))

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=timeout))
    except asyncio.TimeoutError as e:
        raise VideoSearchError("gTTS demorou demais para gerar a narração") from e
    except Exception as e:
        raise VideoSearchError(f"gTTS falhou: {type(e).__name__}: {e}") from e
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise VideoSearchError("gTTS não gerou um arquivo de áudio válido")


def _tts_elevenlabs(text: str, settings: Settings, out_path: Path, timeout: float) -> None:
    if not settings.elevenlabs_configured:
        raise VideoSearchError("ELEVENLABS_API_KEY e ELEVENLABS_VOICE_ID são necessários")
    try:
        response = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}",
            headers={"xi-api-key": settings.elevenlabs_api_key.get_secret_value(),
                     "Accept": "audio/mpeg", "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": settings.elevenlabs_model,
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.75,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            },
            timeout=timeout,
        )
        if response.status_code == 402:
            raise VideoSearchError(
                "ElevenLabs recusou ELEVENLABS_VOICE_ID (HTTP 402): contas gratuitas "
                "nao podem usar vozes da Voice Library via API. Configure uma voz "
                "propria disponivel em My Voices ou use um plano pago."
            )
        if response.status_code != 200:
            detail = response.text[-300:].replace("\n", " ")
            raise VideoSearchError(f"ElevenLabs HTTP {response.status_code}: {detail}")
        out_path.write_bytes(response.content)
    except VideoSearchError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, OSError) as e:
        raise VideoSearchError(f"ElevenLabs falhou: {type(e).__name__}: {e}") from e
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise VideoSearchError("ElevenLabs não gerou um arquivo de áudio válido")


def _tts_narration(text: str, settings: Settings, out_path: Path, timeout: float,
                   elevenlabs_text: str | None = None) -> None:
    """Gera ElevenLabs; gTTS só roda quando escolhido explicitamente."""
    if settings.video_tts_provider in ("auto", "elevenlabs"):
        if not settings.elevenlabs_configured:
            raise VideoSearchError("ElevenLabs não configurado: defina ELEVENLABS_API_KEY e ELEVENLABS_VOICE_ID")
        _tts_elevenlabs(elevenlabs_text or text, settings, out_path, timeout)
        return
    if settings.video_tts_provider == "gtts":
        _tts_gtts(text, settings.video_tts_lang, settings.video_tts_tld, settings.video_tts_slow,
                  out_path, timeout)
        return
    raise VideoSearchError(f"provedor de TTS inválido: {settings.video_tts_provider}")


def _script_texts(theme: str, trend_term: str) -> tuple[str, list[str]]:
    """Gancho e as falas do roteiro, sem depender da duração (usado tanto pra TTS quanto legenda)."""
    hooks = HOOKS.get(theme, ["Isso pode facilitar sua rotina.", "Você também está passando por isso?", "Encontre ajuda perto de você."])
    hook = hooks[sum(ord(c) for c in trend_term) % len(hooks)]
    texts = [hook, "Tem alguém que sabe resolver.", "Encontre serviços no Doova.",
             "Instale o app Doova e encontre ajuda ou ofereça seu serviço."]
    return hook, texts


def _elevenlabs_narration_prompt(script_texts: list[str], settings: Settings) -> str:
    """Adiciona Audio Tags apenas aos modelos ElevenLabs que as suportam."""
    clean = ". ".join(script_texts)
    supported_models = {"eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo"}
    if (settings.video_tts_provider not in ("auto", "elevenlabs")
            or not settings.elevenlabs_configured
            or settings.elevenlabs_model not in supported_models):
        return clean
    tags = ("curious", "thoughtful", "confident", "excited")
    return ". ".join(
        f"[{tags[index] if index < len(tags) else 'confident'}] {text}"
        for index, text in enumerate(script_texts)
    )


def subtitle_plan(theme: str, trend_term: str, duration: float) -> tuple[str, list[dict[str, Any]]]:
    hook, texts = _script_texts(theme, trend_term)
    span = max(duration / len(texts), 1)
    rows = [{"start": round(i * span, 2), "end": round((i + 1) * span, 2), "text": t} for i, t in enumerate(texts)]
    return hook, rows


def _srt(rows: list[dict[str, Any]]) -> str:
    def stamp(value: float) -> str:
        ms = int(round(value * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
        return f"{h:02}:{m:02}:{sec:02},{ms:03}"
    return "\n\n".join(f"{i}\n{stamp(x['start'])} --> {stamp(x['end'])}\n{x['text']}" for i, x in enumerate(rows, 1)) + "\n"


def _ass_stamp(value: float) -> str:
    centiseconds = max(0, int(round(value * 100)))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _karaoke_text(text: str, duration: float) -> str:
    words = text.split()
    if not words:
        return ""
    total_centiseconds = max(len(words), int(round(duration * 100)))
    weights = [max(len(word), 1) for word in words]
    total_weight = sum(weights)
    cursor = 0
    parts: list[str] = []
    for index, word in enumerate(words):
        if index == len(words) - 1:
            end = total_centiseconds
        else:
            end = max(cursor + 1, round(total_centiseconds * sum(weights[:index + 1]) / total_weight))
        parts.append(f"{{\\k{end - cursor}}}{_ass_escape(word)}")
        cursor = end
    return " ".join(parts)


def _ass(rows: list[dict[str, Any]], settings: Settings, font_name: str) -> str:
    style = (
        f"Style: Default,{font_name},{settings.video_font_size},"
        f"{settings.video_karaoke_active_color},{settings.video_karaoke_inactive_color},"
        "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,2,20,20,180,1"
    )
    events = [
        f"Dialogue: 0,{_ass_stamp(row['start'])},{_ass_stamp(row['end'])},Default,,0,0,0,,"
        f"{_karaoke_text(row['text'], row['end'] - row['start'])}"
        for row in rows
    ]
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        style, "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        *events,
        "",
    ])


def render_video(src: Path, rows: list[dict[str, Any]], settings: Settings, out: Path,
                 duration_seconds: float, audio: Path | None = None) -> None:
    """Lê src (e, se houver, audio) e escreve em out, sem passar o vídeo pela memória do
    Python em nenhum momento (o ffmpeg processa stream a stream por fora do processo)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg não instalado; use a imagem Docker do projeto ou instale ffmpeg")
    subs = src.parent / "captions.srt"
    subs.write_text(_srt(rows), encoding="utf-8")
    font = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Inter-Bold.otf"
    font_name = font.stem if font.exists() else "Arial"
    if settings.video_karaoke_enabled:
        ass = src.parent / "captions.ass"
        ass.write_text(_ass(rows, settings, font_name), encoding="utf-8")
        subtitle_path = ass.as_posix().replace(":", "\\:")
        subtitle_filter = f"subtitles={subtitle_path}"
    else:
        subtitle_path = subs.as_posix().replace(":", "\\:")
        subtitle_filter = f"subtitles={subtitle_path}:force_style='FontName={font_name},FontSize={settings.video_font_size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=180'"
    encode_opts = [
        "-c:v", "libx264",
        # ultrafast + sem b-frames: encoder mais simples, bem menos buffer de lookahead
        # (o preset padrão "medium" pode usar várias vezes mais RAM pra isso num clipe 1080x1920)
        "-preset", "ultrafast", "-bf", "0",
        # trava threads do libx264 pra não disparar várias fatias simultâneas num host com
        # pouca RAM/CPU compartilhada — 2 é um bom equilíbrio pra instâncias pequenas
        "-threads", "1",
        "-filter_threads", "1",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    scale_filter = (f"scale={settings.video_width}:{settings.video_height}:"
                    f"force_original_aspect_ratio=increase,crop={settings.video_width}:{settings.video_height},"
                    f"{subtitle_filter}")
    if audio is not None:
        # -stream_loop -1 no vídeo: se a narração (TTS) for mais longa que o clipe de origem,
        # o vídeo repete em loop até cobrir a fala inteira em vez de cortar o áudio no meio.
        # -t no final (depois dos dois -i) corta vídeo e áudio exatamente na duração da narração.
        cmd = [ffmpeg, "-y",
               "-stream_loop", "-1", "-i", str(src),
               "-i", str(audio),
               "-t", str(duration_seconds),
               "-vf", scale_filter,
               "-map", "0:v:0", "-map", "1:a:0",
               *encode_opts,
               "-c:a", "aac", "-b:a", "128k",
               str(out)]
    else:
        cmd = [ffmpeg, "-y", "-i", str(src), "-t", str(duration_seconds), "-vf", scale_filter,
               "-an", *encode_opts, str(out)]
    # margem maior que antes (+30s não bastava em CPU compartilhada/limitada)
    result = subprocess.run(cmd, capture_output=True, timeout=settings.video_timeout_seconds + 60)
    if result.returncode != 0 or not out.exists():
        raise VideoSearchError(f"ffmpeg falhou: {result.stderr.decode(errors='replace')[-2000:]}")


def _ffmpeg_loop_input(path: Path) -> list[str]:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return ["-loop", "1", "-framerate", "30", "-i", str(path)]
    return ["-stream_loop", "-1", "-i", str(path)]


def _run_ffmpeg(cmd: list[str], out: Path, settings: Settings) -> None:
    result = subprocess.run(cmd, capture_output=True, timeout=settings.video_timeout_seconds + 60)
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise VideoSearchError(f"ffmpeg falhou: {result.stderr.decode(errors='replace')[-2000:]}")


def _normalize_video_source(source: Path, out: Path, settings: Settings,
                            duration_seconds: float) -> None:
    """Normaliza uma fonte por vez sem reduzir a resolucao final."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg nao instalado; use a imagem Docker do projeto ou instale ffmpeg")
    scale_filter = (f"scale={settings.video_width}:{settings.video_height}:"
                    f"force_original_aspect_ratio=increase,crop={settings.video_width}:{settings.video_height},"
                    "fps=30,format=yuv420p,settb=AVTB,setsar=1")
    cmd = [ffmpeg, "-y", "-filter_threads", "1", *_ffmpeg_loop_input(source),
           "-t", f"{duration_seconds:.3f}", "-vf", scale_filter, "-an",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0",
           "-threads", "1", "-pix_fmt", "yuv420p", str(out)]
    _run_ffmpeg(cmd, out, settings)


def _render_xfade_pair(first: Path, second: Path, out: Path, settings: Settings,
                       transition_duration: float, offset: float,
                       final: bool, rows: list[dict[str, Any]] | None,
                       duration_seconds: float, audio: Path | None,
                       transition: str = "fade") -> None:
    """Aplica uma transicao entre apenas dois videos."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg nao instalado; use a imagem Docker do projeto ou instale ffmpeg")
    if transition not in VIDEO_TRANSITIONS:
        transition = "fade"
    filters = [
        "[0:v:0]setpts=PTS-STARTPTS,fps=30,format=yuv420p,settb=AVTB,setsar=1[first]",
        "[1:v:0]setpts=PTS-STARTPTS,fps=30,format=yuv420p,settb=AVTB,setsar=1[second]",
        (f"[first][second]xfade=transition={transition}:duration={transition_duration:.3f}:"
         f"offset={offset:.3f}[mixed]"),
    ]
    output_label = "[mixed]"
    if final:
        if rows is None:
            raise VideoSearchError("legendas ausentes no render final")
        work_dir = out.parent
        subs = work_dir / "captions.srt"
        subs.write_text(_srt(rows), encoding="utf-8")
        font = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Inter-Bold.otf"
        font_name = font.stem if font.exists() else "Arial"
        if settings.video_karaoke_enabled:
            ass = work_dir / "captions.ass"
            ass.write_text(_ass(rows, settings, font_name), encoding="utf-8")
            subtitle_path = ass.as_posix().replace(":", "\\:")
            subtitle_filter = f"subtitles={subtitle_path}"
        else:
            subtitle_path = subs.as_posix().replace(":", "\\:")
            subtitle_filter = (f"subtitles={subtitle_path}:force_style='FontName={font_name},"
                               f"FontSize={settings.video_font_size},Bold=1,"
                               "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                               "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=180'")
        filters.append(f"[mixed]{subtitle_filter}[vout]")
        output_label = "[vout]"

    cmd = [ffmpeg, "-y", "-filter_complex_threads", "1", "-i", str(first), "-i", str(second)]
    if audio is not None:
        cmd.extend(["-i", str(audio)])
    cmd.extend(["-t", f"{duration_seconds:.3f}", "-filter_complex", ";".join(filters),
                "-map", output_label])
    if audio is not None:
        cmd.extend(["-map", "2:a:0", "-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0",
                "-threads", "1", "-filter_threads", "1", "-pix_fmt", "yuv420p"])
    if final:
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(str(out))
    _run_ffmpeg(cmd, out, settings)


def _render_video_sources_progressive(sources: list[Path], rows: list[dict[str, Any]],
                                      settings: Settings, out: Path, duration_seconds: float,
                                      audio: Path | None = None, transition: str = "fade") -> None:
    transition_duration = min(0.35, duration_seconds / (len(sources) * 3))
    # Compensa o overlap do xfade para preservar a duracao total do reel.
    segment_duration = (duration_seconds + (len(sources) - 1) * transition_duration) / len(sources)
    work_dir = sources[0].parent
    current = work_dir / "__doova-normalized-0.mp4"
    _normalize_video_source(sources[0], current, settings, segment_duration)
    current_duration = segment_duration
    for index, source in enumerate(sources[1:], start=1):
        next_source = work_dir / f"__doova-normalized-{index}.mp4"
        _normalize_video_source(source, next_source, settings, segment_duration)
        final = index == len(sources) - 1
        next_output = out if final else work_dir / f"__doova-xfade-{index}.mp4"
        _render_xfade_pair(
            current, next_source, next_output, settings, transition_duration,
            current_duration - transition_duration, final, rows,
            duration_seconds if final else current_duration + segment_duration - transition_duration,
            audio if final else None, transition=transition,
        )
        if current != out:
            current.unlink(missing_ok=True)
        next_source.unlink(missing_ok=True)
        current = next_output
        current_duration += segment_duration - transition_duration


def _render_video_sources(sources: list[Path], rows: list[dict[str, Any]], settings: Settings,
                          out: Path, duration_seconds: float, audio: Path | None = None,
                          transition: str = "fade") -> None:
    """Renderiza vários placeholders com transições e pico menor de memória."""
    if len(sources) <= 1:
        render_video(sources[0], rows, settings, out, duration_seconds, audio=audio)
        return
    _render_video_sources_progressive(sources, rows, settings, out, duration_seconds, audio=audio,
                                      transition=transition)
    return
def _upload_to_cloudinary(source: bytes | Path, asset_key: str, settings: Settings,
                          overwrite: bool = False) -> tuple[str, str]:
    """Envia o vídeo renderizado e devolve ``(url, public_id)``.

    O ``public_id`` (com a pasta) é guardado no vídeo para permitir apagar o
    arquivo do Cloudinary depois, sem precisar reconstruir o nome a partir da URL.
    """
    if not settings.cloudinary_configured:
        raise VideoSearchError(
            "Cloudinary não configurado: defina CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY e CLOUDINARY_API_SECRET"
        )
    folder = "doova/videos"
    public_id = f"{folder}/{asset_key}"
    try:
        import cloudinary
        from cloudinary.uploader import upload

        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret.get_secret_value(),
            secure=True,
        )
        # passando o caminho do arquivo (str), o SDK do Cloudinary faz streaming multipart
        # direto do disco — não carrega o vídeo inteiro em memória do Python antes de subir.
        payload = str(source) if isinstance(source, Path) else io.BytesIO(source)
        result = upload(
            payload, resource_type="video", folder=folder, overwrite=overwrite,
            public_id=asset_key, unique_filename=False,
        )
        url = result.get("secure_url")
        if not url:
            raise VideoSearchError("Cloudinary não retornou a URL do vídeo")
        return str(url), str(result.get("public_id") or public_id)
    except VideoSearchError:
        raise
    except Exception as e:
        raise VideoSearchError(f"upload para o Cloudinary falhou: {type(e).__name__}: {e}") from e


def _destroy_cloudinary(public_id: str, settings: Settings) -> None:
    if not settings.cloudinary_configured:
        raise VideoSearchError("Cloudinary não configurado")
    try:
        import cloudinary
        from cloudinary.uploader import destroy

        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret.get_secret_value(),
            secure=True,
        )
        result = destroy(public_id, resource_type="video", invalidate=True)
        if result.get("result") not in ("ok", "not found"):
            raise VideoSearchError(f"Cloudinary recusou apagar {public_id}: {result}")
    except VideoSearchError:
        raise
    except Exception as e:
        raise VideoSearchError(f"remoção no Cloudinary falhou: {type(e).__name__}: {e}") from e


def create_video(db: Session, theme: str, trend_term: str, query: str, settings: Settings | None = None,
                 client: httpx.Client | None = None, render: bool = True, force: bool = False,
                 variant: str | None = None, transition: str | None = None,
                 duration_seconds: float | None = None, generated_image_count: int | None = None) -> VideoAsset:
    """Gera (e opcionalmente renderiza) um reel.

    variant/transition/duration_seconds/generated_image_count são os níveis dos
    fatores testáveis de retenção: quando None, cada um roda em rodízio automático
    (igual ao sistema de experimentos de foto); quando informado, fixa o nível —
    é o que um experimento passa pra garantir que cada publicação do bloco use o
    nível exato que está sendo comparado.
    """
    s = settings or get_settings()
    candidates = search_videos(query, s, client)
    if not candidates:
        raise VideoSearchError(f"nenhum vídeo adequado encontrado para '{query}'")
    selected = candidates[:s.video_segment_count]
    candidate = selected[0]
    existing = db.scalar(select(VideoAsset).where(VideoAsset.provider == candidate.provider,
                                                   VideoAsset.provider_id == candidate.provider_id))
    chosen_variant = _choose_video_variant(db, s, existing, force, requested=variant)
    chosen_transition = _choose_video_transition(db, s, existing, force, requested=transition)
    # O fluxo automático continua idempotente; o teste manual pode re-renderizar
    # o mesmo vídeo de stock explicitamente com force=True.
    if existing and not force and (existing.video_url or not render):
        return existing
    if existing and not force and existing.mp4_bytes:
        existing.video_url, legacy_public_id = _upload_to_cloudinary(
            existing.mp4_bytes, f"{theme}-{candidate.provider_id}-legacy-{uuid4().hex}", s
        )
        existing.mp4_bytes = None
        meta_legacy = dict(existing.metadata_json or {})
        meta_legacy["cloudinary_public_id"] = legacy_public_id
        existing.metadata_json = meta_legacy
        db.commit()
        db.refresh(existing)
        return existing
    hook, script_texts = _script_texts(theme, trend_term)
    video_url: str | None = None
    digest = ""
    # tudo em disco, dentro de um diretório temporário que é apagado ao sair do with —
    # o vídeo não passa pela memória do Python em nenhuma etapa deste bloco.
    with tempfile.TemporaryDirectory(prefix="doova-video-") as folder:
        root = Path(folder)
        source_items: list[dict[str, Any]] = []
        digests: list[str] = []
        for index, source_candidate in enumerate(selected):
            source = root / f"source-{index}.mp4"
            try:
                _download(source_candidate, s, client, source)
            except VideoSearchError:
                if not source_items:
                    raise
                logger.warning("ignorando placeholder %s/%s; os demais serao usados", source_candidate.provider,
                               source_candidate.provider_id)
                continue
            source_items.append({
                "path": source,
                "kind": "stock_video",
                "provider": source_candidate.provider,
                "provider_id": source_candidate.provider_id,
                "title": source_candidate.title,
            })
            digests.append(_sha256_file(source))
        if not source_items:
            raise VideoSearchError("nenhum vídeo placeholder foi baixado")
        digest = hashlib.sha256("".join(digests).encode()).hexdigest()
        generated_texts = script_texts[1:-1]
        requested_count = generated_image_count if generated_image_count is not None \
            else s.video_generated_image_count
        generated_count = min(max(0, requested_count), len(generated_texts))
        spec = VIDEO_CREATIVE_SPECS[chosen_variant]
        context_frame: Path | None = None
        context_photo_info: dict[str, Any] | None = None
        if spec["has_context_image"] and generated_count:
            # A imagem de apoio vem de um banco de fotos, nunca de um frame do vídeo.
            # Se não houver foto semanticamente relacionada, o criativo usa o gradiente Doova.
            try:
                from app.services.image_search import find_photo
                image_query_en = (VIDEO_QUERY_ALIASES.get(theme) or (trend_term,))[0]
                photo, photo_log = find_photo(db, image_query_en, trend_term, s, client)
                if photo is not None:
                    context_frame = root / "context-photo.jpg"
                    context_frame.write_bytes(photo.jpg_bytes)
                    context_photo_info = {
                        "provider": photo.provider, "provider_id": photo.provider_id,
                        "title": photo.title, "landing_url": photo.landing_url,
                        "license": photo.license, "creator": photo.creator,
                    }
                else:
                    logger.info("sem foto contextual para %s; usando placeholder Doova: %s",
                                trend_term, "; ".join(photo_log[-3:]))
            except Exception as exc:
                logger.warning("busca de foto contextual falhou (%s); usando placeholder Doova",
                               type(exc).__name__)
                context_frame = None
        for index in range(generated_count):
            generated = root / f"generated-{index}.png"
            _generate_video_image(theme, trend_term, generated_texts[index], generated, s, index,
                                  variant=chosen_variant, background_path=context_frame)
            insertion = min(len(source_items), 1 + index * 2)
            source_items.insert(insertion, {
                "path": generated,
                "kind": "generated_image",
                "provider": "doova",
                "provider_id": f"generated-{index}",
                "role": "script_support",
                "text": generated_texts[index],
            })
            digests.append(_sha256_file(generated))
        sources = [item["path"] for item in source_items]
        requested_duration = float(duration_seconds) if duration_seconds is not None \
            else float(s.video_duration_seconds)
        if render:
            out = root / "output.mp4"
            audio_path: Path | None = None
            # duração alvo parte do nível pedido (ou do padrão configurado); se o TTS
            # rodar, a narração passa a comandar a duração — exceto quando um nível
            # explícito foi pedido por um experimento, caso em que o render corta
            # vídeo e áudio em requested_duration (o -t do ffmpeg já faz esse corte),
            # mesmo que a fala natural fosse mais longa.
            final_duration: float = requested_duration
            if s.video_tts_enabled:
                audio_path = root / "narration.mp3"
                try:
                    narration = ". ".join(script_texts)
                    elevenlabs_narration = _elevenlabs_narration_prompt(script_texts, s)
                    _tts_narration(narration, s, audio_path, s.video_timeout_seconds,
                                   elevenlabs_text=elevenlabs_narration)
                    measured = _probe_duration(audio_path, s.video_timeout_seconds)
                    final_duration = requested_duration if duration_seconds is not None else measured
                except VideoSearchError as e:
                    raise VideoSearchError(f"TTS falhou para {theme}/{trend_term}: {e}") from e
            duration_seconds = final_duration
            _, rows = subtitle_plan(theme, trend_term, duration_seconds)
            _render_video_sources(sources, rows, s, out, duration_seconds, audio=audio_path,
                                  transition=chosen_transition)
            video_url, cloudinary_public_id = _upload_to_cloudinary(
                out, f"{theme}-{candidate.provider_id}-{uuid4().hex}", s
            )
        else:
            duration_seconds = requested_duration
            cloudinary_public_id = None
            _, rows = subtitle_plan(theme, trend_term, duration_seconds)
        segment_span = duration_seconds / len(source_items)
        segments = [
            {
                "index": index,
                "kind": item["kind"],
                "provider": item.get("provider"),
                "provider_id": item.get("provider_id"),
                "role": item.get("role", "stock_placeholder"),
                "text": item.get("text"),
                "start_seconds": round(index * segment_span, 2),
                "end_seconds": round((index + 1) * segment_span, 2),
                "transition_in": chosen_transition if index else "start",
            }
            for index, item in enumerate(source_items)
        ]
        digest = hashlib.sha256("".join(digests).encode()).hexdigest()
        video_metadata = {
            "texts": rows,
            "sha256": digest,
            "segments": segments,
            "creative_variant": chosen_variant,
            "duration_seconds": round(duration_seconds, 2),
            "duration_requested": round(requested_duration, 2),
            "transition": chosen_transition,
            "generated_image_count": generated_count,
            "context_image": context_photo_info or {"provider": "doova", "kind": "brand_placeholder"},
            "cloudinary_public_id": cloudinary_public_id,
        }
    if existing:
        existing.query = query[:200]
        existing.title = candidate.title[:300]
        existing.creator = candidate.creator[:200]
        existing.landing_url = candidate.landing_url[:500]
        existing.download_url = candidate.download_url[:800]
        existing.width = candidate.width
        existing.height = candidate.height
        existing.duration_seconds = candidate.duration
        existing.video_url = video_url
        existing.mp4_bytes = None
        existing.subtitle_text = _srt(rows)
        existing.hook = hook[:300]
        existing.trend_term = trend_term[:200]
        existing.theme_key = theme[:80]
        existing.metadata_json = video_metadata
        db.commit()
        db.refresh(existing)
        return existing
    source = "Pexels" if candidate.provider == "pexels" else "Pixabay"
    license_name = "Pexels License" if candidate.provider == "pexels" else "Pixabay Content License"
    asset = VideoAsset(provider=candidate.provider, provider_id=candidate.provider_id, query=query[:200], title=candidate.title[:300],
                       creator=candidate.creator[:200], source=source, license=license_name,
                       landing_url=candidate.landing_url[:500], download_url=candidate.download_url[:800],
                       width=candidate.width, height=candidate.height, duration_seconds=candidate.duration,
                       video_url=video_url, mp4_bytes=None, subtitle_text=_srt(rows), hook=hook[:300], trend_term=trend_term[:200],
                       theme_key=theme[:80], metadata_json=video_metadata)
    db.add(asset); db.commit(); db.refresh(asset)
    return asset


def publish_reel(db: Session, asset: VideoAsset, settings: Settings | None = None, meta=None,
                 caption_override: str | None = None) -> dict[str, Any]:
    """Publica um Reel já renderizado no Instagram.

    Lógica compartilhada pela rota manual (``POST /api/videos/{id}/publish``) e pelo
    job automático do agendador — por isso levanta ``VideoPublishError`` em vez de
    ``HTTPException`` (a rota converte; o job apenas registra o erro). ``meta`` é
    injetável (mesmo padrão de ``Publisher``), para testar sem chamar a Graph API de verdade.
    """
    from app.services.meta_service import MetaError, MetaService

    s = settings or get_settings()
    metadata = dict(asset.metadata_json or {})
    if metadata.get("instagram_media_id"):
        return {"status": "already_published", "media_id": metadata["instagram_media_id"],
                "permalink": metadata.get("instagram_permalink")}
    if not asset.video_url:
        raise VideoPublishError("o Reel ainda não foi renderizado ou enviado ao Cloudinary", status=409)
    if not s.meta_configured:
        raise VideoPublishError("configure META_IG_USER_ID e META_ACCESS_TOKEN para publicar Reels", status=503)
    caption = caption_override or (
        f"{asset.hook or asset.trend_term}\n\n{asset.trend_term}\n\nEncontre profissionais e serviços no Doova."
    )
    context = metadata.get("context_image") or {}
    if context.get("license") == "by":
        caption += (f"\n\nImagem de apoio: {context.get('title') or 'imagem ilustrativa'}, "
                    f"por {context.get('creator') or 'autor não informado'}. "
                    f"Origem: {context.get('landing_url') or 'Openverse'}. Licença CC BY; imagem adaptada.")
    if s.dry_run:
        return {"status": "simulated", "message": "DRY_RUN=true: nenhum Reel foi enviado ao Instagram"}
    meta = meta or MetaService(s)
    try:
        container_id = metadata.get("instagram_container_id")
        if not container_id:
            container_id = meta.create_reel_container(asset.video_url, caption, share_to_feed=True)
            metadata["instagram_container_id"] = container_id
            asset.metadata_json = metadata
            db.commit()
        status = meta.wait_container(container_id, interval=s.container_check_interval_seconds)
        if status.status_code != "FINISHED":
            raise VideoPublishError(f"Meta ainda não concluiu o processamento do Reel (status: {status.status_code})")
        media_id = meta.publish_container(container_id)
        media = meta.get_media(media_id)
        metadata = {**metadata, "instagram_media_id": media_id, "instagram_permalink": media.get("permalink"),
                    "instagram_published_at": media.get("timestamp")}
        asset.metadata_json = metadata
        db.commit()
        db.refresh(asset)
        return {"status": "published", "media_id": media_id, "permalink": media.get("permalink")}
    except MetaError as exc:
        raise VideoPublishError(f"falha na publicação do Reel: {exc}") from exc


def sync_linked_post_after_reel(db: Session, asset: VideoAsset, result: dict[str, Any]) -> None:
    """Sincroniza o estado do post associado depois de publicar ou simular seu Reel."""
    result_status = result.get("status")
    if result_status not in ("published", "already_published", "simulated"):
        return
    post = db.scalar(select(Post).where(Post.video_asset_id == asset.id))
    if post is None or post.status != PostStatus.SCHEDULED.value:
        return

    from app.services.content_service import set_status

    post.published_at = utcnow()
    post.ig_media_id = result.get("media_id") or post.ig_media_id
    post.permalink = result.get("permalink") or post.permalink
    final_status = PostStatus.SIMULATED if result_status == "simulated" else PostStatus.PUBLISHED
    message = ("Reel publicado pelo agendador" if final_status == PostStatus.PUBLISHED
               else "Reel simulado (DRY_RUN=true)")
    set_status(db, post, final_status, message)
    db.commit()


def unpublished_rendered_videos(db: Session, limit: int = 20) -> list[VideoAsset]:
    """Reels renderizados (têm ``video_url``) que ainda não foram publicados no Instagram.

    Usado pelo job automático de publicação — ``instagram_media_id`` só existe em
    ``metadata_json`` depois de publicado, então filtramos em Python (JSON não é
    indexável de forma portátil entre SQLite e Postgres aqui).
    """
    assets = db.scalars(
        select(VideoAsset).where(VideoAsset.video_url.is_not(None)).order_by(VideoAsset.created_at)
    ).all()
    out = [a for a in assets if not (a.metadata_json or {}).get("instagram_media_id")]
    return out[:limit]


def delete_video(db: Session, asset: VideoAsset, settings: Settings | None = None, force: bool = False) -> dict[str, Any]:
    """Apaga um vídeo gerado: registro no banco, métricas associadas e arquivo no Cloudinary.

    A Graph API do Instagram não permite apagar uma mídia já publicada (só contêineres
    que ainda não viraram publicação). Por isso, se o Reel já foi publicado, é preciso
    ``force=True`` para confirmar: a remoção apaga o registro local e o arquivo no
    Cloudinary, mas o Reel continua no Instagram até ser apagado por lá manualmente.
    """
    from app.models import VideoMetricSnapshot

    s = settings or get_settings()
    metadata = dict(asset.metadata_json or {})
    linked_posts = db.scalars(select(Post).where(Post.video_asset_id == asset.id)).all()
    if any(post.status != PostStatus.CANCELLED.value for post in linked_posts):
        raise VideoDeleteError("vídeo vinculado a uma publicação ativa; cancele a publicação antes de removê-lo")
    published = bool(metadata.get("instagram_media_id"))
    if published and not force:
        raise VideoDeleteError(
            "este Reel já foi publicado no Instagram e a API não permite apagá-lo de lá; "
            "use force=true para apagar o registro local e o arquivo no Cloudinary mesmo assim "
            "(o vídeo continua no Instagram até ser removido por lá manualmente)"
        )
    cloudinary_public_id = metadata.get("cloudinary_public_id")
    cloudinary_deleted = False
    if cloudinary_public_id and s.cloudinary_configured:
        try:
            _destroy_cloudinary(cloudinary_public_id, s)
            cloudinary_deleted = True
        except VideoSearchError as e:
            logger.warning("falha ao apagar vídeo %s do Cloudinary (%s): %s", asset.id, cloudinary_public_id, e)
    video_id = asset.id
    for post in linked_posts:
        post.video_asset_id = None
    db.execute(VideoMetricSnapshot.__table__.delete().where(VideoMetricSnapshot.video_id == video_id))
    db.delete(asset)
    db.commit()
    return {"id": video_id, "deleted": True, "was_published_on_instagram": published,
            "cloudinary_deleted": cloudinary_deleted}


def public(asset: VideoAsset, base_url: str = "") -> dict[str, Any]:
    return {"id": asset.id, "provider": asset.provider, "query": asset.query, "title": asset.title,
            "creator": asset.creator, "source": asset.source, "license": asset.license,
            "landing_url": asset.landing_url, "trend_term": asset.trend_term, "theme_key": asset.theme_key,
            "hook": asset.hook, "subtitle_text": asset.subtitle_text,
            "segments": (asset.metadata_json or {}).get("segments", []),
            "creative_variant": (asset.metadata_json or {}).get("creative_variant"),
            "transition": (asset.metadata_json or {}).get("transition"),
            "duration_seconds": (asset.metadata_json or {}).get("duration_seconds"),
            "generated_image_count": (asset.metadata_json or {}).get("generated_image_count"),
            "context_image": (asset.metadata_json or {}).get("context_image"),
            "instagram_published": bool((asset.metadata_json or {}).get("instagram_media_id")),
            "instagram_permalink": (asset.metadata_json or {}).get("instagram_permalink"),
            "rendered": bool(asset.video_url or asset.mp4_bytes),
            "video_url": asset.video_url or f"{base_url}/api/videos/{asset.id}/file"}