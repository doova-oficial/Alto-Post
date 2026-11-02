"""Busca vídeos gratuitos e cria reels verticais com gancho e legendas queimadas."""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import VideoAsset

PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"


class VideoSearchError(RuntimeError):
    pass


@dataclass
class VideoCandidate:
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


def _candidate(raw: dict[str, Any], query: str) -> VideoCandidate | None:
    videos = raw.get("videos") or {}
    # Pixabay devolve várias resoluções; prioriza grande para não pixelar ao publicar em 9:16.
    choices = sorted(videos.values(), key=lambda x: int(x.get("width") or 0), reverse=True)
    for item in choices:
        url = item.get("url")
        if url and int(item.get("width") or 0) >= 720:
            return VideoCandidate(str(raw.get("id")), str(raw.get("tags") or query),
                                  str(raw.get("user") or "Pixabay"), str(raw.get("pageURL") or ""), url,
                                  int(item.get("width") or 0), int(item.get("height") or 0),
                                  float(raw.get("duration") or 0), query)
    return None


def search_videos(query: str, settings: Settings | None = None,
                  client: httpx.Client | None = None) -> list[VideoCandidate]:
    s = settings or get_settings()
    if not s.video_enabled:
        raise VideoSearchError("vídeos desativados (VIDEO_ENABLED=false)")
    if not s.pixabay_configured:
        raise VideoSearchError("PIXABAY_API_KEY é necessária para buscar vídeos gratuitos")
    q = " ".join((query or "").split())[:100]
    if len(q) < 2:
        raise VideoSearchError("termo de vídeo vazio")
    c, own = _client(s, client)
    try:
        r = c.get(PIXABAY_VIDEO_URL, params={"key": s.pixabay_api_key.get_secret_value(), "q": q,
                                              "lang": "pt", "safesearch": "true", "per_page": 30},
                  timeout=s.video_timeout_seconds)
        if r.status_code != 200:
            raise VideoSearchError(f"Pixabay vídeos HTTP {r.status_code}")
        return [x for raw in r.json().get("hits", []) if (x := _candidate(raw, q)) is not None]
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as e:
        raise VideoSearchError(f"Pixabay vídeos indisponível: {type(e).__name__}") from e
    finally:
        if own:
            c.close()


def _download(candidate: VideoCandidate, settings: Settings, client: httpx.Client | None) -> bytes:
    c, own = _client(settings, client)
    try:
        r = c.get(candidate.download_url, timeout=settings.video_timeout_seconds)
        if r.status_code != 200:
            raise VideoSearchError(f"download do vídeo falhou (HTTP {r.status_code})")
        if len(r.content) > settings.video_max_download_bytes:
            raise VideoSearchError("vídeo maior que o limite configurado")
        if not r.content.startswith(b"\x00\x00\x00"):
            raise VideoSearchError("o banco não devolveu um MP4 válido")
        return r.content
    finally:
        if own:
            c.close()


def subtitle_plan(theme: str, trend_term: str, duration: int) -> tuple[str, list[dict[str, Any]]]:
    hooks = HOOKS.get(theme, ["Isso pode facilitar sua rotina.", "Você também está passando por isso?", "Encontre ajuda perto de você."])
    hook = hooks[sum(ord(c) for c in trend_term) % len(hooks)]
    texts = [hook, "Tem alguém que sabe resolver.", "Encontre serviços no Doova.", "Peça ajuda. Ofereça seu trabalho."]
    span = max(duration / len(texts), 1)
    rows = [{"start": round(i * span, 2), "end": round((i + 1) * span, 2), "text": t} for i, t in enumerate(texts)]
    return hook, rows


def _srt(rows: list[dict[str, Any]]) -> str:
    def stamp(value: float) -> str:
        ms = int(round(value * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
        return f"{h:02}:{m:02}:{sec:02},{ms:03}"
    return "\n\n".join(f"{i}\n{stamp(x['start'])} --> {stamp(x['end'])}\n{x['text']}" for i, x in enumerate(rows, 1)) + "\n"


def render_video(source: bytes, rows: list[dict[str, Any]], settings: Settings) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg não instalado; use a imagem Docker do projeto ou instale ffmpeg")
    with tempfile.TemporaryDirectory(prefix="doova-video-") as folder:
        root = Path(folder); src, subs, out = root / "source.mp4", root / "captions.srt", root / "output.mp4"
        src.write_bytes(source); subs.write_text(_srt(rows), encoding="utf-8")
        font = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Inter-Bold.otf"
        subtitle_path = subs.as_posix().replace(":", "\\:")
        subtitle_filter = f"subtitles={subtitle_path}:force_style='FontName=Inter,FontSize={settings.video_font_size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=180'"
        if font.exists():
            subtitle_filter = subtitle_filter.replace("FontName=Inter", f"FontName={font.stem}")
        cmd = [ffmpeg, "-y", "-i", str(src), "-t", str(settings.video_duration_seconds), "-vf",
               f"scale={settings.video_width}:{settings.video_height}:force_original_aspect_ratio=increase,crop={settings.video_width}:{settings.video_height},{subtitle_filter}",
               "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
        result = subprocess.run(cmd, capture_output=True, timeout=settings.video_timeout_seconds + 30)
        if result.returncode != 0 or not out.exists():
            raise VideoSearchError(f"ffmpeg falhou: {result.stderr.decode(errors='replace')[-400:]}")
        return out.read_bytes()


def _upload_to_cloudinary(data: bytes, asset_key: str, settings: Settings) -> str:
    if not settings.cloudinary_configured:
        raise VideoSearchError(
            "Cloudinary nÃ£o configurado: defina CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY e CLOUDINARY_API_SECRET"
        )
    try:
        import cloudinary
        from cloudinary.uploader import upload

        cloudinary.config(
            cloud_name=settings.cloudinary_cloud_name,
            api_key=settings.cloudinary_api_key,
            api_secret=settings.cloudinary_api_secret.get_secret_value(),
            secure=True,
        )
        result = upload(
            io.BytesIO(data), resource_type="video", folder="doova/videos",
            public_id=asset_key, overwrite=False, unique_filename=False,
        )
        url = result.get("secure_url")
        if not url:
            raise VideoSearchError("Cloudinary nÃ£o retornou a URL do vÃ­deo")
        return str(url)
    except VideoSearchError:
        raise
    except Exception as e:
        raise VideoSearchError(f"upload para o Cloudinary falhou: {type(e).__name__}: {e}") from e


def create_video(db: Session, theme: str, trend_term: str, query: str, settings: Settings | None = None,
                 client: httpx.Client | None = None, render: bool = True) -> VideoAsset:
    s = settings or get_settings()
    candidate = next(iter(search_videos(query, s, client)), None)
    if candidate is None:
        raise VideoSearchError(f"nenhum vídeo adequado encontrado para '{query}'")
    existing = db.scalar(select(VideoAsset).where(VideoAsset.provider == "pixabay",
                                                   VideoAsset.provider_id == candidate.provider_id))
    if existing:
        return existing
    hook, rows = subtitle_plan(theme, trend_term, s.video_duration_seconds)
    raw = _download(candidate, s, client)
    rendered = render_video(raw, rows, s) if render else None
    video_url = _upload_to_cloudinary(rendered, f"{theme}-{candidate.provider_id}-{uuid4().hex}", s) if rendered else None
    asset = VideoAsset(provider="pixabay", provider_id=candidate.provider_id, query=query[:200], title=candidate.title[:300],
                       creator=candidate.creator[:200], source="Pixabay", license="Pixabay Content License",
                       landing_url=candidate.landing_url[:500], download_url=candidate.download_url[:800],
                       width=candidate.width, height=candidate.height, duration_seconds=candidate.duration,
                       video_url=video_url, mp4_bytes=None, subtitle_text=_srt(rows), hook=hook[:300], trend_term=trend_term[:200],
                       theme_key=theme[:80], metadata_json={"texts": rows, "sha256": hashlib.sha256(raw).hexdigest()})
    db.add(asset); db.commit(); db.refresh(asset)
    return asset


def public(asset: VideoAsset, base_url: str = "") -> dict[str, Any]:
    return {"id": asset.id, "provider": asset.provider, "query": asset.query, "title": asset.title,
            "creator": asset.creator, "source": asset.source, "license": asset.license,
            "landing_url": asset.landing_url, "trend_term": asset.trend_term, "theme_key": asset.theme_key,
            "hook": asset.hook, "subtitle_text": asset.subtitle_text,
            "rendered": bool(asset.video_url or asset.mp4_bytes),
            "video_url": asset.video_url or f"{base_url}/api/videos/{asset.id}/file"}
