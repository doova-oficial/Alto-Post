"""Busca e importação de fotos genéricas em bancos gratuitos: Openverse e Pixabay.

Regras de uso, conferidas na documentação oficial em out/2026:

* **Openverse** (https://api.openverse.org/v1/): uso anônimo permitido, com limite de
  requisições (429 quando excedido). É proibido raspar o acervo. Cada resultado traz
  licença, autor e página de origem. Aqui só entram licenças que permitem uso
  comercial e modificação sem "compartilha igual": CC0, Marca de Domínio Público e CC BY.
  A CC BY exige crédito com título, autor, origem, licença e aviso de modificação.
  Esse crédito é escrito automaticamente no fim da legenda.
* **Pixabay** (https://pixabay.com/api/docs/): exige chave (PIXABAY_API_KEY). O limite é
  de 100 requisições por 60 s e as buscas precisam ficar em cache por 24 h. Não é
  permitido usar o link da imagem de forma permanente (hotlink), então a foto é
  baixada. É preciso indicar a origem (Pixabay) ao mostrar resultados.

As fotos são sempre ilustrativas. Elas nunca representam usuários, prestadores ou
trabalhos reais do Doova.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Post, StockPhoto
from app.services.caption_validator import strip_emojis
from app.services.state import get_state, set_state

logger = logging.getLogger(__name__)

OPENVERSE_URL = "https://api.openverse.org/v1/images/"
PIXABAY_URL = "https://pixabay.com/api/"
# Licenças aceitas: uso comercial + modificação, sem cláusula "compartilha igual" (by-sa) nem "não comercial".
OPENVERSE_LICENSES = ("cc0", "pdm", "by")
ALLOWED_LICENSES = set(OPENVERSE_LICENSES) | {"pixabay"}
LICENSE_LABELS = {"cc0": "CC0", "pdm": "Domínio público", "by": "CC BY", "pixabay": "Licença do Pixabay"}
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_SIDE = 2000
SOURCE_LABELS = {"flickr": "Flickr", "wikimedia": "Wikimedia Commons", "rawpixel": "rawpixel",
                 "stocksnap": "StockSnap", "nappy": "nappy", "pixabay": "Pixabay"}


class ImageSearchError(RuntimeError):
    def __init__(self, message: str, upstream: bool = False):
        super().__init__(message)
        self.upstream = upstream  # True = falha do serviço externo (rede, limite, indisponível)


@dataclass
class PhotoCandidate:
    provider: str
    provider_id: str
    title: str
    creator: str
    creator_url: str | None
    source: str
    license: str
    license_version: str | None
    license_url: str | None
    landing_url: str
    download_url: str
    thumbnail: str | None
    width: int
    height: int
    tags: list[str] = field(default_factory=list)
    attribution: str = ""
    query: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ HTTP
def _user_agent(s: Settings) -> str:
    contact = f"; {s.contact_email}" if s.contact_email else ""
    return f"DoovaMarketing/1.0 (+https://github.com/doova{contact})"


def _client(s: Settings, client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=s.image_search_timeout_seconds, follow_redirects=True,
                        headers={"User-Agent": _user_agent(s)}), True


def _get(c: httpx.Client, url: str, s: Settings, params: dict | None = None, attempts: int = 3) -> httpx.Response:
    last = "erro desconhecido"
    for i in range(attempts):
        try:
            r = c.get(url, params=params, timeout=s.image_search_timeout_seconds,
                      headers={"User-Agent": _user_agent(s)})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = f"falha de rede/timeout ({type(e).__name__})"
        else:
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                if i < attempts - 1:
                    try:
                        wait = min(10.0, float(r.headers.get("retry-after", 2 * (i + 1))))
                    except ValueError:
                        wait = 2.0 * (i + 1)
                    time.sleep(wait)
                continue
            return r
        if i < attempts - 1:
            time.sleep(1.0 * (i + 1))
    raise ImageSearchError(f"serviço de imagens indisponível: {last}", upstream=True)


def _clean_query(q: str) -> str:
    q = unicodedata.normalize("NFC", q or "")
    q = re.sub(r"[^\w\s\-]", " ", q)
    return re.sub(r"\s+", " ", q).strip()[:100]


def _words(text: str) -> set[str]:
    t = "".join(ch for ch in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(ch) != "Mn")
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2}


# --------------------------------------------------------------- parsing
def _parse_openverse(r: dict, q: str) -> PhotoCandidate | None:
    lic = (r.get("license") or "").lower()
    if lic not in OPENVERSE_LICENSES or r.get("mature") or r.get("unstable__sensitivity"):
        return None
    url, creator = r.get("url"), (r.get("creator") or "").strip()
    if not url or (lic == "by" and not creator):  # CC BY sem autor não permite crédito correto
        return None
    return PhotoCandidate(
        provider="openverse", provider_id=str(r.get("id")), title=(r.get("title") or "").strip(),
        creator=creator or "autor desconhecido", creator_url=r.get("creator_url"),
        source=(r.get("source") or r.get("provider") or "").strip(), license=lic,
        license_version=r.get("license_version"), license_url=r.get("license_url"),
        landing_url=r.get("foreign_landing_url") or r.get("detail_url") or "", download_url=url,
        thumbnail=r.get("thumbnail"), width=int(r.get("width") or 0), height=int(r.get("height") or 0),
        tags=[t.get("name") for t in (r.get("tags") or []) if isinstance(t, dict) and t.get("name")][:15],
        attribution=r.get("attribution") or "", query=q)


def _parse_pixabay(h: dict, q: str) -> PhotoCandidate | None:
    url = h.get("largeImageURL") or h.get("webformatURL")
    if not url:
        return None
    w, hh = int(h.get("imageWidth") or 0), int(h.get("imageHeight") or 0)
    if url == h.get("largeImageURL") and w > 1280:  # largeImageURL tem no máximo 1280 px de lado
        hh, w = int(hh * 1280 / w), 1280
    tags = [t.strip() for t in (h.get("tags") or "").split(",") if t.strip()]
    return PhotoCandidate(
        provider="pixabay", provider_id=str(h.get("id")), title=", ".join(tags[:3]),
        creator=(h.get("user") or "Pixabay").strip(),
        creator_url=f"https://pixabay.com/users/{h.get('user')}-{h.get('user_id')}/" if h.get("user") else None,
        source="pixabay", license="pixabay", license_version=None,
        license_url="https://pixabay.com/service/license-summary/", landing_url=h.get("pageURL") or "",
        download_url=url, thumbnail=h.get("previewURL"), width=w, height=hh, tags=tags, attribution="", query=q)


def _usable(c: PhotoCandidate, s: Settings) -> bool:
    if c.license not in ALLOWED_LICENSES:
        return False
    if c.width and c.width < s.image_min_width:
        return False
    if c.width and c.height:
        ratio = c.width / c.height
        if not 0.5 <= ratio <= 2.0:
            return False
    return True


# ------------------------------------------------------------------ busca
def search(db: Session, query: str, provider: str = "openverse", settings: Settings | None = None,
           client: httpx.Client | None = None) -> list[PhotoCandidate]:
    """Busca fotos (com cache de pelo menos 24 h por termo e banco). Não baixa nada."""
    s = settings or get_settings()
    if not s.image_search_enabled:
        raise ImageSearchError("busca de imagens desativada (IMAGE_SEARCH_ENABLED=false)")
    if provider not in ("openverse", "pixabay"):
        raise ImageSearchError(f"banco de imagens desconhecido: {provider}")
    if provider == "pixabay" and not s.pixabay_configured:
        raise ImageSearchError("PIXABAY_API_KEY não configurada")
    q = _clean_query(query)
    if len(q) < 2:
        raise ImageSearchError("termo de busca vazio")
    key = f"imgsearch:{provider}:{hashlib.sha1(q.lower().encode()).hexdigest()[:24]}"
    cache = get_state(db, key)
    now = utcnow().timestamp()
    if cache and now - float(cache.get("fetched_at", 0)) < s.image_search_cache_hours * 3600:
        return [PhotoCandidate(**c) for c in cache.get("items", [])]
    c, own = _client(s, client)
    try:
        if provider == "openverse":
            r = _get(c, OPENVERSE_URL, s, params={
                "q": q, "license": ",".join(OPENVERSE_LICENSES), "category": "photograph",
                "mature": "false", "filter_dead": "true", "page_size": 20})
            if r.status_code != 200:
                raise ImageSearchError(f"Openverse HTTP {r.status_code}", upstream=True)
            raw = [_parse_openverse(x, q) for x in r.json().get("results", [])]
        else:
            r = _get(c, PIXABAY_URL, s, params={
                "key": s.pixabay_api_key.get_secret_value(), "q": q[:100], "lang": "pt", "image_type": "photo",
                "safesearch": "true", "per_page": 30, "min_width": s.image_min_width})
            if r.status_code != 200:
                raise ImageSearchError(f"Pixabay HTTP {r.status_code}: {r.text[:120]}", upstream=True)
            raw = [_parse_pixabay(x, q) for x in r.json().get("hits", [])]
    except ValueError as e:  # JSON inválido
        raise ImageSearchError(f"resposta inválida do banco de imagens: {e}", upstream=True) from e
    finally:
        if own:
            c.close()
    items = [x for x in raw if x is not None and _usable(x, s)]
    set_state(db, key, {"fetched_at": now, "query": q, "provider": provider, "items": [x.public() for x in items]})
    db.commit()
    return items


def get_candidate(db: Session, provider: str, provider_id: str, settings: Settings | None = None,
                  client: httpx.Client | None = None) -> PhotoCandidate:
    """Busca os dados atuais de uma foto específica (para importar a partir de um id)."""
    s = settings or get_settings()
    pid = re.sub(r"[^\w\-]", "", provider_id or "")[:120]
    if not pid:
        raise ImageSearchError("identificador da foto inválido")
    c, own = _client(s, client)
    try:
        if provider == "openverse":
            r = _get(c, f"{OPENVERSE_URL}{pid}/", s)
            if r.status_code == 404:
                raise ImageSearchError("foto não encontrada no Openverse")
            if r.status_code != 200:
                raise ImageSearchError(f"Openverse HTTP {r.status_code}", upstream=True)
            cand = _parse_openverse(r.json(), "")
        elif provider == "pixabay":
            if not s.pixabay_configured:
                raise ImageSearchError("PIXABAY_API_KEY não configurada")
            r = _get(c, PIXABAY_URL, s, params={"key": s.pixabay_api_key.get_secret_value(), "id": pid})
            if r.status_code != 200:
                raise ImageSearchError(f"Pixabay HTTP {r.status_code}", upstream=True)
            hits = r.json().get("hits", [])
            cand = _parse_pixabay(hits[0], "") if hits else None
        else:
            raise ImageSearchError(f"banco de imagens desconhecido: {provider}")
    finally:
        if own:
            c.close()
    if cand is None or not _usable(cand, s):
        raise ImageSearchError("foto sem licença comercial aceita, pequena demais ou indisponível")
    return cand


# -------------------------------------------------------------- importação
def import_photo(db: Session, cand: PhotoCandidate, settings: Settings | None = None,
                 client: httpx.Client | None = None) -> StockPhoto:
    """Baixa a foto, valida, converte para JPEG e guarda com origem e licença."""
    s = settings or get_settings()
    existing = db.scalar(select(StockPhoto).where(StockPhoto.provider == cand.provider,
                                                  StockPhoto.provider_id == cand.provider_id))
    if existing is not None:
        return existing
    if cand.license not in ALLOWED_LICENSES:
        raise ImageSearchError(f"licença não aceita: {cand.license}")
    c, own = _client(s, client)
    try:
        r = _get(c, cand.download_url, s, attempts=2)
    finally:
        if own:
            c.close()
    if r.status_code != 200:
        raise ImageSearchError(f"download da foto falhou (HTTP {r.status_code})", upstream=True)
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith("image/"):
        raise ImageSearchError(f"o link não devolveu uma imagem ({ctype})")
    data = r.content
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ImageSearchError("arquivo de imagem grande demais")
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in ("JPEG", "PNG", "WEBP"):
            raise ImageSearchError(f"formato de imagem não suportado: {im.format}")
        im.load()
    except ImageSearchError:
        raise
    except Exception as e:
        raise ImageSearchError(f"imagem inválida: {type(e).__name__}") from e
    im = ImageOps.exif_transpose(im).convert("RGB")
    if im.width < s.image_min_width:
        raise ImageSearchError(f"foto pequena demais ({im.width}px; mínimo {s.image_min_width}px)")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90, optimize=True)
    jpg = buf.getvalue()
    photo = StockPhoto(
        provider=cand.provider, provider_id=cand.provider_id, query=cand.query[:200], title=cand.title[:300],
        creator=cand.creator[:200], creator_url=(cand.creator_url or None) and cand.creator_url[:500],
        source=cand.source[:80], license=cand.license, license_version=cand.license_version,
        license_url=cand.license_url, landing_url=cand.landing_url[:500], download_url=cand.download_url[:800],
        attribution=cand.attribution, tags=cand.tags, width=im.width, height=im.height, jpg_bytes=jpg,
        sha256=hashlib.sha256(jpg).hexdigest())
    db.add(photo)
    db.commit()
    return photo


def recently_used(db: Session, days: int) -> set[tuple[str, str]]:
    if days <= 0:
        return set()
    cutoff = utcnow() - timedelta(days=days)
    rows = db.execute(select(StockPhoto.provider, StockPhoto.provider_id)
                      .join(Post, Post.photo_id == StockPhoto.id).where(Post.created_at >= cutoff)).all()
    return {(a, b) for a, b in rows}


# Palavras (sem acento) que, no título ou nas tags, indicam pessoa identificável ou marca.
FLAG_WORDS = {
    "person", "persons", "people", "man", "men", "woman", "women", "face", "faces", "portrait", "selfie",
    "girl", "girls", "boy", "boys", "child", "children", "kid", "kids", "baby", "family", "couple", "crowd",
    "celebrity", "model", "logo", "brand", "trademark",
    "pessoa", "pessoas", "homem", "homens", "mulher", "mulheres", "rosto", "retrato", "crianca", "criancas",
    "menino", "menina", "familia", "bebe", "casal", "multidao", "logotipo", "marca", "marcas",
}


def photo_flags(title: str | None, tags: list[str] | None) -> list[str]:
    return sorted(_words(" ".join([title or ""] + list(tags or []))) & FLAG_WORDS)


def photo_auto_issues(photo: StockPhoto | None) -> list[str]:
    """Conferência automática da foto (substitui a revisão humana no modo automático)."""
    if photo is None:
        return ["foto ausente"]
    hits = photo_flags(photo.title, photo.tags)
    if hits:
        return [f"foto pode mostrar pessoas ou marcas (título/tags: {', '.join(hits)}); troque a foto"]
    return []


def _relevance(c: PhotoCandidate, q: str) -> int:
    return len(_words(q) & (_words(c.title) | _words(" ".join(c.tags))))


def find_photo(db: Session, query_en: str, query_pt: str | None = None, settings: Settings | None = None,
               client: httpx.Client | None = None) -> tuple[StockPhoto | None, list[str]]:
    """Procura nos bancos configurados e importa a primeira foto adequada. Retorna (foto, registro)."""
    s = settings or get_settings()
    log: list[str] = []
    used = recently_used(db, s.image_reuse_days)
    providers = [p for p in s.image_provider_list() if p != "pixabay" or s.pixabay_configured]
    if not providers:
        return None, ["nenhum banco de imagens disponível"]
    for prov in providers:
        base = (query_pt or query_en) if prov == "pixabay" else query_en
        for q in fallback_queries(base):
            try:
                cands = search(db, q, prov, s, client)
            except ImageSearchError as e:
                log.append(f"{prov}: {e}")
                break  # serviço indisponível: passa para o próximo banco
            if prov == "openverse":  # acervo amplo: exige que título ou tags tenham alguma palavra da busca
                cands = [c for c in cands if _relevance(c, q) > 0]
            cands.sort(key=lambda c: (-_relevance(c, q), -(c.width or 0)))
            fresh = [c for c in cands if (c.provider, c.provider_id) not in used]
            if s.photo_auto_check:  # fotos que indicam pessoas ou marcas ficam de fora
                fresh = [c for c in fresh if not photo_flags(c.title, c.tags)]
            log.append(f"{prov}: {len(cands)} resultado(s) aproveitável(is) para '{q}'")
            for c in fresh[:4]:
                try:
                    return import_photo(db, c, s, client), log
                except ImageSearchError as e:
                    log.append(f"{prov}:{c.provider_id}: {e}")
    return None, log


def fallback_queries(q: str) -> list[str]:
    """Termo completo e versões mais curtas (tira a última palavra), no máximo 3 tentativas."""
    words = _clean_query(q).split()
    out = [" ".join(words[:n]) for n in range(len(words), 0, -1)]
    return [x for x in out if len(x) >= 3][:3]


# ---------------------------------------------------------------- créditos
def _safe(text: str | None, limit: int) -> str:
    t = strip_emojis(unicodedata.normalize("NFC", text or ""))
    t = re.sub(r"[#<>\[\]{}\"“”]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def license_label(photo: StockPhoto) -> str:
    base = LICENSE_LABELS.get(photo.license, photo.license.upper())
    if photo.license in ("by", "cc0") and photo.license_version:
        return f"{base} {photo.license_version}"
    return base


def _source_label(photo: StockPhoto) -> str:
    src = SOURCE_LABELS.get((photo.source or "").lower(), photo.source or "")
    return f"{src} via Openverse" if photo.provider == "openverse" and src else "Openverse"


def short_credit(photo: StockPhoto) -> str:
    """Crédito curto exibido sobre a foto, na arte."""
    if photo.provider == "pixabay":
        return _safe(f"Foto: {photo.creator} / Pixabay", 60)
    return _safe(f"Foto: {_safe(photo.creator, 34)} ({license_label(photo)})", 60)


def caption_credit(photo: StockPhoto) -> str:
    """Crédito completo que vai no fim da legenda (título, autor, origem, licença e alteração)."""
    creator = _safe(photo.creator, 60)
    if photo.provider == "pixabay":
        return f"Imagem ilustrativa: {creator} / Pixabay."
    title = _safe(photo.title, 70)
    head = f"Imagem ilustrativa: '{title}', de {creator}" if title else f"Imagem ilustrativa de {creator}"
    if photo.license == "by":
        url = f" ({photo.license_url})" if photo.license_url else ""
        return f"{head}, licença {license_label(photo)}{url}, {_source_label(photo)}. Recortada e com texto adicionado."
    return f"{head} ({license_label(photo)}), {_source_label(photo)}."


def with_photo_credit(text: str, photo: StockPhoto | None) -> str:
    t = (text or "").rstrip()
    if photo is None:
        return t
    credit = caption_credit(photo)
    if credit in t:
        return t
    return f"{t}\n\n{credit}" if t else credit