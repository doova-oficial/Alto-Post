"""Geração de placeholders/reels a partir de assuntos em alta."""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import VideoAsset
from app.routes.deps import http_client
from app.security import AdminDep, GenerationLimit
from app.services.trend_content import TREND_THEMES, match_theme
from app.services.video_service import (VIDEO_CREATIVE_SPECS, VIDEO_TRANSITIONS, VideoSearchError,
                                        create_video, public, search_videos)
from app.services.meta_service import MetaError, MetaService

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
        asset = create_video(db, theme, req.trend_term, req.trend_term, settings,
                             client=http_client(), render=req.render, force=req.force,
                             variant=req.variant, transition=req.transition,
                             duration_seconds=req.duration_seconds,
                             generated_image_count=req.generated_image_count)
    except VideoSearchError as e:
        raise HTTPException(502, str(e))
    should_publish = settings.video_auto_publish if req.publish_to_instagram is None else req.publish_to_instagram
    result = public(asset)
    if should_publish and req.render:
        result["instagram_publication"] = _publish_reel(asset, db, settings)
    return result


def _publish_reel(asset: VideoAsset, db: Session, settings=None) -> dict:
    settings = settings or get_settings()
    metadata = dict(asset.metadata_json or {})
    if metadata.get("instagram_media_id"):
        return {"status": "already_published", "media_id": metadata["instagram_media_id"],
                "permalink": metadata.get("instagram_permalink")}
    if not asset.video_url:
        raise HTTPException(409, "o Reel ainda não foi renderizado ou enviado ao Cloudinary")
    if not settings.meta_configured:
        raise HTTPException(503, "configure META_IG_USER_ID e META_ACCESS_TOKEN para publicar Reels")
    caption = f"{asset.hook or asset.trend_term}\n\n{asset.trend_term}\n\nEncontre profissionais e serviços no Doova."
    context = metadata.get("context_image") or {}
    if context.get("license") == "by":
        caption += (f"\n\nImagem de apoio: {context.get('title') or 'imagem ilustrativa'}, "
                    f"por {context.get('creator') or 'autor não informado'}. "
                    f"Origem: {context.get('landing_url') or 'Openverse'}. Licença CC BY; imagem adaptada.")
    if settings.dry_run:
        return {"status": "simulated", "message": "DRY_RUN=true: nenhum Reel foi enviado ao Instagram"}
    meta = MetaService(settings)
    try:
        container_id = metadata.get("instagram_container_id")
        if not container_id:
            container_id = meta.create_reel_container(asset.video_url, caption, share_to_feed=True)
            metadata["instagram_container_id"] = container_id
            asset.metadata_json = metadata
            db.commit()
        status = meta.wait_container(container_id, interval=settings.container_check_interval_seconds)
        if status.status_code != "FINISHED":
            raise HTTPException(502, f"Meta ainda não concluiu o processamento do Reel (status: {status.status_code})")
        media_id = meta.publish_container(container_id)
        media = meta.get_media(media_id)
        metadata.update({"instagram_media_id": media_id,
                         "instagram_permalink": media.get("permalink"),
                         "instagram_published_at": media.get("timestamp")})
        asset.metadata_json = metadata
        db.commit()
        db.refresh(asset)
        return {"status": "published", "media_id": media_id, "permalink": media.get("permalink")}
    except MetaError as exc:
        raise HTTPException(502, f"falha na publicação do Reel: {exc}") from exc


@router.post("/videos/{video_id}/publish", tags=["vídeos"],
             summary="Publicar um Reel já renderizado no Instagram")
def publish_video_now(video_id: str, db: Session = Depends(get_db)):
    asset = db.get(VideoAsset, video_id)
    if asset is None:
        raise HTTPException(404, "vídeo não encontrado")
    return _publish_reel(asset, db)


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