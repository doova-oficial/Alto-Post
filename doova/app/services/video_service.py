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

PEXELS_VIDEO_URL = "https://api.pexels.com/v1/videos/search"
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
    """Monta o texto emocional somente para a chamada do ElevenLabs.

    As legendas continuam usando ``script_texts`` sem instrucoes de atuacao. O
    gTTS tambem recebe o texto limpo, inclusive quando o ElevenLabs falha e o
    modo ``auto`` faz fallback.
    """
    clean_text = ". ".join(script_texts)
    if not (settings.elevenlabs_configured and settings.video_tts_provider in ("auto", "elevenlabs")):
        return clean_text

    emotion_cues = (
        "[tom acolhedor, natural e conversacional]",
        "[tom tranquilizador e prestativo]",
        "[tom claro, positivo e encorajador]",
        "[tom amigavel, convidativo e com energia]",
    )
    return ". ".join(
        f"{emotion_cues[min(index, len(emotion_cues) - 1)]} {text}"
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


def _render_video_sources(sources: list[Path], rows: list[dict[str, Any]], settings: Settings,
                          out: Path, duration_seconds: float, audio: Path | None = None) -> None:
    """Renderiza varios placeholders em sequência, com cortes secos entre eles."""
    if len(sources) <= 1:
        render_video(sources[0], rows, settings, out, duration_seconds, audio=audio)
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoSearchError("ffmpeg nao instalado; use a imagem Docker do projeto ou instale ffmpeg")
    work_dir = sources[0].parent
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
        subtitle_filter = f"subtitles={subtitle_path}:force_style='FontName={font_name},FontSize={settings.video_font_size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=180'"
    base_scale = (f"scale={settings.video_width}:{settings.video_height}:"
                  f"force_original_aspect_ratio=increase,crop={settings.video_width}:{settings.video_height},"
                  "fps=30,format=yuv420p")
    segment_duration = duration_seconds / len(sources)
    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(sources)):
        label = f"v{index}"
        labels.append(f"[{label}]")
        filters.append(f"[{index}:v:0]{base_scale},trim=duration={segment_duration:.3f},"
                       f"setpts=PTS-STARTPTS[{label}]")
    transition_duration = min(0.35, segment_duration / 3)
    current_label = labels[0]
    current_duration = segment_duration
    for index in range(1, len(sources)):
        next_label = labels[index]
        output_label = f"[x{index}]"
        offset = current_duration - transition_duration
        filters.append(
            f"{current_label}{next_label}xfade=transition=fade:duration={transition_duration:.3f}:"
            f"offset={offset:.3f}{output_label}"
        )
        current_label = output_label
        current_duration += segment_duration - transition_duration
    filters.append(f"{current_label}{subtitle_filter}[vout]")
    cmd = [ffmpeg, "-y"]
    for source in sources:
        cmd.extend(["-stream_loop", "-1", "-i", str(source)])
    if audio is not None:
        cmd.extend(["-i", str(audio)])
    cmd.extend(["-t", str(duration_seconds), "-filter_complex", ";".join(filters),
                "-map", "[vout]"])
    if audio is not None:
        cmd.extend(["-map", f"{len(sources)}:a:0", "-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-threads", "2",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
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
                 client: httpx.Client | None = None, render: bool = True, force: bool = False) -> VideoAsset:
    s = settings or get_settings()
    candidates = search_videos(query, s, client)
    if not candidates:
        raise VideoSearchError(f"nenhum vídeo adequado encontrado para '{query}'")
    selected = candidates[:s.video_segment_count]
    candidate = selected[0]
    existing = db.scalar(select(VideoAsset).where(VideoAsset.provider == candidate.provider,
                                                   VideoAsset.provider_id == candidate.provider_id))
    # O fluxo automático continua idempotente; o teste manual pode re-renderizar
    # o mesmo vídeo de stock explicitamente com force=True.
    if existing and not force and (existing.video_url or not render):
        return existing
    if existing and not force and existing.mp4_bytes:
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
        sources: list[Path] = []
        digests: list[str] = []
        for index, source_candidate in enumerate(selected):
            source = root / f"source-{index}.mp4"
            try:
                _download(source_candidate, s, client, source)
            except VideoSearchError:
                if not sources:
                    raise
                logger.warning("ignorando placeholder %s/%s; os demais serao usados", source_candidate.provider,
                               source_candidate.provider_id)
                continue
            sources.append(source)
            digests.append(_sha256_file(source))
        if not sources:
            raise VideoSearchError("nenhum vídeo placeholder foi baixado")
        digest = hashlib.sha256("".join(digests).encode()).hexdigest()
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
                    elevenlabs_narration = _elevenlabs_narration_prompt(script_texts, s)
                    _tts_narration(narration, s, audio_path, s.video_timeout_seconds,
                                   elevenlabs_text=elevenlabs_narration)
                    duration_seconds = _probe_duration(audio_path, s.video_timeout_seconds)
                except VideoSearchError as e:
                    raise VideoSearchError(f"TTS falhou para {theme}/{trend_term}: {e}") from e
            _, rows = subtitle_plan(theme, trend_term, duration_seconds)
            _render_video_sources(sources, rows, s, out, duration_seconds, audio=audio_path)
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
