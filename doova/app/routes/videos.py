"""Geração de placeholders/reels a partir de assuntos em alta."""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, utcnow
from app.models import Post, PostStatus, VideoAsset
from app.routes.deps import http_client
from app.security import AdminDep, GenerationLimit
from app.services.trend_content import TREND_THEMES, match_theme
from app.services.video_service import (VIDEO_CREATIVE_SPECS, VIDEO_TRANSITIONS, VideoDeleteError,
                                        VideoPublishError, VideoSearchError, create_video, delete_video,
                                        public, publish_video_to_targets, search_videos,
                                        sync_linked_post_after_reel)
from app.services.video_worker import enqueue_video_render

router = APIRouter(prefix="/api", dependencies=[AdminDep])


class VideoTrendRequest(BaseModel):
    trend_term: str = Field(min_length=2, max_length=200)
    theme_key: str | None = Field(default=None, max_length=80)
    render: bool = True
    force: bool = False
    # Níveis explícitos dos fatores testáveis; None = rodízio automático (ver VIDEO_FACTORS
    # em app/services/video_service.py). É assim que um experimento fixa o nível do bloco.
    variant: str | None = Field(default=None, max_length=40)
    transition: str | None = Field(default=None, max_length=20)
    duration_seconds: float | None = Field(default=None, ge=5, le=60)
    generated_image_count: int | None = Field(default=None, ge=0, le=4)
    publish_to_instagram: bool | None = None  # None respeita VIDEO_AUTO_PUBLISH


class VideoCheckRequest(BaseModel):
    trend_term: str = Field(min_length=2, max_length=200)
    theme_key: str | None = Field(default=None, max_length=80)


class VideoPublishRequest(BaseModel):
    platforms: list[str] | None = Field(default=None, max_length=3)
    force: bool = False

    @field_validator("platforms")
    @classmethod
    def validate_platforms(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(value) != len(set(value))
                                  or set(value) - {"instagram", "facebook", "tiktok"}):
            raise ValueError("use destinos únicos: instagram, facebook, tiktok")
        return value


def _theme(req: VideoTrendRequest) -> str:
    theme = req.theme_key or match_theme(req.trend_term)
    if theme not in TREND_THEMES:
        raise HTTPException(422, "a tendência não corresponde a um tema de serviço seguro")
    return theme


def _theme_from_check(req: VideoCheckRequest) -> str:
    theme = req.theme_key or match_theme(req.trend_term)
    if theme not in TREND_THEMES:
        raise HTTPException(422, "a tendência não corresponde a um tema de serviço seguro")
    return theme


@router.post("/videos/trend/check", tags=["vídeos"],
             summary="Verificar Pexels/Pixabay e ffmpeg sem baixar ou renderizar vídeo",
             dependencies=[GenerationLimit])
def check_video_from_trend(req: VideoCheckRequest):
    """Consulta apenas metadados dos bancos; não grava nada e não baixa MP4."""
    theme = _theme_from_check(req)
    settings = get_settings()
    query = req.trend_term
    try:
        candidates = search_videos(query, settings, client=http_client())
    except VideoSearchError as e:
        raise HTTPException(502, str(e))
    return {
        "trend_term": req.trend_term,
        "theme_key": theme,
        "query": query,
        "video_provider": settings.video_provider,
        "pexels_configured": settings.pexels_configured,
        "pixabay_configured": settings.pixabay_configured,
        "cloudinary_configured": settings.cloudinary_configured,
        "ffmpeg_available": bool(shutil.which("ffmpeg")),
        "downloads_performed": False,
        "results": [
            {"provider_id": x.provider_id, "title": x.title, "creator": x.creator,
             "landing_url": x.landing_url, "width": x.width, "height": x.height,
             "duration_seconds": x.duration}
            for x in candidates[:10]
        ],
    }


@router.post("/videos/trend", tags=["vídeos"], summary="Buscar vídeo e criar reel com gancho e legendas",
             dependencies=[GenerationLimit], status_code=201)
def video_from_trend(req: VideoTrendRequest, db: Session = Depends(get_db)):
    theme = _theme(req)
    settings = get_settings()
    if req.variant and req.variant not in VIDEO_CREATIVE_SPECS:
        raise HTTPException(422, f"variante inválida: {req.variant}; use uma de {sorted(VIDEO_CREATIVE_SPECS)}")
    if req.transition and req.transition not in VIDEO_TRANSITIONS:
        raise HTTPException(422, f"transição inválida: {req.transition}; use uma de {VIDEO_TRANSITIONS}")
    try:
        remote = req.render and settings.video_render_mode == "android"
        script_groq = None
        if settings.trend_use_groq and settings.groq_configured:
            from app.services.groq_service import GroqCaptionService

            script_groq = GroqCaptionService(settings)
        asset = create_video(db, theme, req.trend_term, req.trend_term, settings,
                             client=http_client(), render=req.render and not remote, force=req.force,
                             variant=req.variant, transition=req.transition,
                             duration_seconds=req.duration_seconds, script_groq=script_groq,
                             generated_image_count=req.generated_image_count)
    except VideoSearchError as e:
        raise HTTPException(502, str(e))
    should_publish = settings.video_auto_publish if req.publish_to_instagram is None else req.publish_to_instagram
    targets = None
    if req.publish_to_instagram is True and targets is None:
        targets = ["instagram"]
    result = public(asset)
    if remote and not asset.video_url:
        job = enqueue_video_render(db, asset)
        result["render_job"] = {"id": job.id, "status": job.status}
        result["platform_publication"] = {
            "status": "waiting_for_render",
            "targets": targets if targets is not None else settings.video_publish_targets,
        }
    elif should_publish and req.render:
        result["platform_publication"] = _publish_video(
            asset, db, settings, platforms=targets,
        )
    return result


@router.post("/videos/{video_id}/publish", tags=["vídeos"],
             summary="Publicar um Reel nos destinos configurados")
def publish_video_now(video_id: str, req: VideoPublishRequest = Body(default=VideoPublishRequest()),
                      db: Session = Depends(get_db)):
    asset = db.get(VideoAsset, video_id)
    if asset is None:
        raise HTTPException(404, "vídeo não encontrado")
    post = db.scalar(select(Post).where(Post.video_asset_id == asset.id))
    caption = None
    if post is not None:
        if (post.status != PostStatus.SCHEDULED.value or not post.scheduled_at
                or post.scheduled_at > utcnow()):
            raise HTTPException(409, "a publicação vinculada ainda não está aprovada e agendada para agora")
        if post.expires_at and post.expires_at <= utcnow():
            raise HTTPException(409, "a publicação vinculada expirou")
        if post.caption is None or not post.caption.is_valid:
            raise HTTPException(409, "a publicação vinculada não possui legenda válida")
        caption = post.caption.text
    result = _publish_video(
        asset, db, caption_override=caption, platforms=req.platforms, force=req.force,
    )
    sync_linked_post_after_reel(db, asset, result)
    return result


def _publish_video(asset: VideoAsset, db: Session, settings=None,
                   caption_override: str | None = None, platforms: list[str] | None = None,
                   force: bool = False) -> dict:
    try:
        return publish_video_to_targets(
            db, asset, settings, caption_override=caption_override, platforms=platforms, force=force,
        )
    except VideoPublishError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/videos/factors", tags=["vídeos"],
           summary="Listar variantes, transições e faixas válidas para testar retenção")
def video_factors():
    from app.services.video_service import VIDEO_FACTORS
    return {"factors": {
        "video_variant": {"allowed": sorted(VIDEO_CREATIVE_SPECS),
                          "descriptions": {k: v["name"] for k, v in VIDEO_CREATIVE_SPECS.items()}},
        "video_transition": {"allowed": list(VIDEO_TRANSITIONS)},
        "video_duration_seconds": {"min": VIDEO_FACTORS["video_duration_seconds"]["min"],
                                   "max": VIDEO_FACTORS["video_duration_seconds"]["max"]},
        "video_generated_image_count": {"min": VIDEO_FACTORS["video_generated_image_count"]["min"],
                                        "max": VIDEO_FACTORS["video_generated_image_count"]["max"]},
    }}


@router.get("/videos", tags=["vídeos"], summary="Listar placeholders de vídeo gerados")
def list_videos(db: Session = Depends(get_db)):
    return [public(x) for x in db.scalars(select(VideoAsset).order_by(VideoAsset.created_at.desc())).all()]


@router.get("/videos/{video_id}", tags=["vídeos"], summary="Detalhes do vídeo e do roteiro")
def video_detail(video_id: str, db: Session = Depends(get_db)):
    asset = db.get(VideoAsset, video_id)
    if asset is None:
        raise HTTPException(404, "vídeo não encontrado")
    return public(asset)


@router.delete("/videos/{video_id}", tags=["vídeos"], summary="Apagar um vídeo gerado (e do Cloudinary, se houver)")
def delete_video_endpoint(video_id: str, force: bool = False, db: Session = Depends(get_db)):
    asset = db.get(VideoAsset, video_id)
    if asset is None:
        raise HTTPException(404, "vídeo não encontrado")
    try:
        return delete_video(db, asset, force=force)
    except VideoDeleteError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/videos/{video_id}/file", tags=["vídeos"], summary="Baixar o MP4 renderizado")
def video_file(video_id: str, db: Session = Depends(get_db)):
    asset = db.get(VideoAsset, video_id)
    if asset is None:
        raise HTTPException(404, "MP4 ainda no renderizado")
    if asset.video_url:
        return RedirectResponse(asset.video_url, status_code=307)
    if not asset.mp4_bytes:
        raise HTTPException(404, "MP4 ainda não renderizado")
    return Response(asset.mp4_bytes, media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"})