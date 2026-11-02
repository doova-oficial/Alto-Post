"""Busca vídeos gratuitos e cria reels verticais com gancho, narração e legendas queimadas."""
from __future__ import annotations

import asyncio
import hashlib
import io
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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import VideoAsset

PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"
PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
logger = logging.getLogger("app")


class VideoSearchError(RuntimeError):
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
             if item.get("link") and item.get("file_type", "mp4") == "mp4"]
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
            params={"query": query, "per_page": 30, "orientation": "portrait"},
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
        if s.video_provider == "pexels":
            providers.append("pixabay")
        for provider in providers:
            candidates = (_search_pexels(q, s, c) if provider == "pexels"
                          else _search_pixabay(q, s, c))
            if candidates:
                return candidates
        raise VideoSearchError(f"nenhum vídeo encontrado para '{q}' nos provedores configurados")
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


def _tts_narration(text: str, settings: Settings, out_path: Path, timeout: float) -> None:
    """Gera voz neural quando disponível e usa gTTS como fallback gratuito."""
    if settings.video_tts_provider in ("auto", "elevenlabs"):
        if settings.elevenlabs_configured:
            try:
                _tts_elevenlabs(text, settings, out_path, timeout)
                return
            except VideoSearchError:
                if settings.video_tts_provider == "elevenlabs":
                    raise
                logger.warning("ElevenLabs falhou; usando gTTS como fallback")
        elif settings.video_tts_provider == "elevenlabs":
            raise VideoSearchError("ElevenLabs não configurado: defina ELEVENLABS_API_KEY e ELEVENLABS_VOICE_ID")
    _tts_gtts(text, settings.video_tts_lang, settings.video_tts_tld, settings.video_tts_slow,
              out_path, timeout)


def _script_texts(theme: str, trend_term: str) -> tuple[str, list[str]]:
    """Gancho e as falas do roteiro, sem depender da duração (usado tanto pra TTS quanto legenda)."""
    hooks = HOOKS.get(theme, ["Isso pode facilitar sua rotina.", "Você também está passando por isso?", "Encontre ajuda perto de você."])
    hook = hooks[sum(ord(c) for c in trend_term) % len(hooks)]
    texts = [hook, "Tem alguém que sabe resolver.", "Encontre serviços no Doova.", "Peça ajuda. Ofereça seu trabalho."]
    return hook, texts


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
    subtitle_path = subs.as_posix().replace(":", "\\:")
    subtitle_filter = f"subtitles={subtitle_path}:force_style='FontName=Inter,FontSize={settings.video_font_size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=180'"
    if font.exists():
        subtitle_filter = subtitle_filter.replace("FontName=Inter", f"FontName={font.stem}")
    encode_opts = [
        "-c:v", "libx264",
        # ultrafast + sem b-frames: encoder mais simples, bem menos buffer de lookahead
        # (o preset padrão "medium" pode usar várias vezes mais RAM pra isso num clipe 1080x1920)
        "-preset", "ultrafast", "-bf", "0",
        # trava threads do libx264 pra não disparar várias fatias simultâneas num host com
        # pouca RAM/CPU compartilhada — 2 é um bom equilíbrio pra instâncias pequenas
        "-threads", "2",
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
        raise VideoSearchError(f"ffmpeg falhou: {result.stderr.decode(errors='replace')[-400:]}")


def _upload_to_cloudinary(source: bytes | Path, asset_key: str, settings: Settings) -> str:
    if not settings.cloudinary_configured:
        raise VideoSearchError(
            "Cloudinary não configurado: defina CLOUDINARY_CLOUD_NAME, "
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
        # passando o caminho do arquivo (str), o SDK do Cloudinary faz streaming multipart
        # direto do disco — não carrega o vídeo inteiro em memória do Python antes de subir.
        payload = str(source) if isinstance(source, Path) else io.BytesIO(source)
        result = upload(
            payload, resource_type="video", folder="doova/videos",
            public_id=asset_key, overwrite=False, unique_filename=False,
        )
        url = result.get("secure_url")
        if not url:
            raise VideoSearchError("Cloudinary não retornou a URL do vídeo")
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
    existing = db.scalar(select(VideoAsset).where(VideoAsset.provider == candidate.provider,
                                                   VideoAsset.provider_id == candidate.provider_id))
    if existing and (existing.video_url or not render):
        return existing
    if existing and existing.mp4_bytes:
        existing.video_url = _upload_to_cloudinary(
            existing.mp4_bytes, f"{theme}-{candidate.provider_id}-legacy-{uuid4().hex}", s
        )
        existing.mp4_bytes = None
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
        src = root / "source.mp4"
        _download(candidate, s, client, src)
        digest = _sha256_file(src)
        if render:
            out = root / "output.mp4"
            audio_path: Path | None = None
            # duração alvo parte do valor configurado; se o TTS rodar, passa a ser a
            # duração real da fala, pra legenda e narração ficarem sincronizadas.
            duration_seconds: float = float(s.video_duration_seconds)
            if s.video_tts_enabled:
                audio_path = root / "narration.mp3"
                try:
                    narration = ". ".join(script_texts)
                    _tts_narration(narration, s, audio_path, s.video_timeout_seconds)
                    duration_seconds = _probe_duration(audio_path, s.video_timeout_seconds)
                except VideoSearchError as e:
                    # TTS é bônus, não motivo pra derrubar o vídeo inteiro — segue mudo com a
                    # duração configurada, igual ao comportamento antes do TTS existir.
                    logger.warning("TTS falhou para %s/%s, seguindo sem áudio: %s", theme, trend_term, e)
                    audio_path = None
                    duration_seconds = float(s.video_duration_seconds)
            _, rows = subtitle_plan(theme, trend_term, duration_seconds)
            render_video(src, rows, s, out, duration_seconds, audio=audio_path)
            video_url = _upload_to_cloudinary(out, f"{theme}-{candidate.provider_id}-{uuid4().hex}", s)
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
        existing.metadata_json = {"texts": rows, "sha256": digest}
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
                       theme_key=theme[:80], metadata_json={"texts": rows, "sha256": digest})
    db.add(asset); db.commit(); db.refresh(asset)
    return asset


def public(asset: VideoAsset, base_url: str = "") -> dict[str, Any]:
    return {"id": asset.id, "provider": asset.provider, "query": asset.query, "title": asset.title,
            "creator": asset.creator, "source": asset.source, "license": asset.license,
            "landing_url": asset.landing_url, "trend_term": asset.trend_term, "theme_key": asset.theme_key,
            "hook": asset.hook, "subtitle_text": asset.subtitle_text,
            "rendered": bool(asset.video_url or asset.mp4_bytes),
            "video_url": asset.video_url or f"{base_url}/api/videos/{asset.id}/file"}
