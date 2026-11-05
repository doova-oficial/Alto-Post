"""Rotas públicas: saúde do serviço e imagem servida para a Meta."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.database import get_db
from app.models import Artwork
from app.services.state import is_paused

router = APIRouter(tags=["público"])


@router.get("/analytics", include_in_schema=False, response_class=HTMLResponse)
def analytics_dashboard():
    page = Path(__file__).resolve().parents[1] / "static" / "analytics.html"
    if not page.is_file():
        raise HTTPException(503, "painel de analytics indisponível")
    return HTMLResponse(
        page.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
        },
    )


@router.get("/health", summary="Verificar a saúde do serviço")
def health(db: Session = Depends(get_db)):
    s = get_settings()
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
        paused = is_paused(db)
    except Exception:
        db_ok, paused = False, None
    body = {
        "status": "ok" if db_ok else "degraded", "version": __version__, "database": "ok" if db_ok else "erro",
        "dry_run": s.dry_run, "paused": paused, "manual_approval": s.require_manual_approval,
        "groq_configured": s.groq_configured, "meta_configured": s.meta_configured,
        "public_base_url_configured": bool(s.public_base_url),
        "cloudinary_configured": s.cloudinary_configured,
        "video_provider": s.video_provider,
        "video_render_mode": s.video_render_mode,
        "pexels_configured": s.pexels_configured,
        "pixabay_fallback_enabled": s.video_pixabay_fallback_enabled,
        "video_tts_provider": s.video_tts_provider,
        "video_tts_enabled": s.video_tts_enabled,
        "elevenlabs_configured": s.elevenlabs_configured,
    }
    return Response(content=__import__("json").dumps(body), media_type="application/json",
                    status_code=200 if db_ok else 503)


@router.get("/media/{token}.jpg", summary="Imagem JPEG pública (usada pela Meta para baixar a arte)",
            responses={200: {"content": {"image/jpeg": {}}}})
def media(token: str, db: Session = Depends(get_db)):
    if len(token) < 20 or len(token) > 64:
        raise HTTPException(404)
    art = db.scalar(select(Artwork).where(Artwork.public_token == token))
    if art is None:
        raise HTTPException(404)
    data = art.jpg_bytes
    if not data and art.jpg_path and Path(art.jpg_path).is_file():
        data = Path(art.jpg_path).read_bytes()
    if not data:
        raise HTTPException(404)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
