"""API autenticada e restrita para workers Android de renderização."""
from __future__ import annotations

import base64
import hashlib
import hmac
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import VideoAsset, VideoRenderJob
from app.security import require_video_worker
from app.services.video_service import (
    VideoSearchError,
    _elevenlabs_audio_with_timestamps,
    _upload_to_cloudinary,
    get_pexels_video,
)
from app.services.video_worker import (
    LEASE_MINUTES,
    claim_video_render,
    fail_video_render,
    renew_video_render_lease,
)

router = APIRouter(prefix="/api/video-worker", tags=["worker de vídeo"])
WorkerAuth = Depends(require_video_worker)
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
REACTION_PEXELS_VIDEO_ID = "15909403"


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")


class FailureRequest(WorkerRequest):
    error: str = Field(min_length=1, max_length=1000)


def _job_or_404(db: Session, job_id: str) -> VideoRenderJob:
    job = db.get(VideoRenderJob, job_id)
    if job is None:
        raise HTTPException(404, "tarefa de renderização não encontrada")
    return job


@router.post("/jobs/claim", dependencies=[WorkerAuth])
def claim(req: ClaimRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    if settings.video_render_mode != "android":
        raise HTTPException(409, "VIDEO_RENDER_MODE não está configurado para android")
    claimed = claim_video_render(db, req.worker_id)
    if claimed is None:
        return {"job": None, "poll_after_seconds": 60}
    job, asset = claimed
    subtitles = (asset.metadata_json or {}).get("texts", [])
    narration_text = ". ".join(
        row.get("text", "").strip() for row in subtitles
        if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"].strip()
    )
    try:
        audio, words = _elevenlabs_audio_with_timestamps(
            narration_text, settings, settings.video_timeout_seconds,
        )
    except VideoSearchError as e:
        fail_video_render(db, job, req.worker_id, f"Narração ElevenLabs falhou: {e}")
        raise HTTPException(502, str(e)) from e
    try:
        reaction = get_pexels_video(REACTION_PEXELS_VIDEO_ID, settings)
    except VideoSearchError as e:
        fail_video_render(db, job, req.worker_id, f"Vídeo de reação não encontrado: {e}")
        raise HTTPException(502, str(e)) from e
    reaction_source = {
        "url": reaction.download_url,
        "provider": reaction.provider,
        "title": reaction.title,
        "creator": reaction.creator,
        "landing_url": reaction.landing_url,
        "license": "Pexels License" if reaction.provider == "pexels" else "Pixabay Content License",
    }
    metadata = dict(asset.metadata_json or {})
    metadata["reaction_source"] = {key: value for key, value in reaction_source.items() if key != "url"}
    asset.metadata_json = metadata
    db.commit()
    return {
        "job": {
            "id": job.id,
            "video_id": asset.id,
            "attempt": job.attempts,
            "lease_seconds": LEASE_MINUTES * 60,
            "source": {
                "url": asset.download_url,
                "provider": asset.provider,
                "title": asset.title,
                "creator": asset.creator,
                "landing_url": asset.landing_url,
                "license": asset.license,
            },
            "reaction_source": reaction_source,
            "render": {
                "width": settings.video_width,
                "height": settings.video_height,
                "max_source_bytes": settings.video_max_download_bytes,
                "duration_seconds": float((asset.metadata_json or {}).get(
                    "duration_requested", settings.video_duration_seconds)),
                "theme": asset.theme_key,
                "trend_term": asset.trend_term,
                "subtitles": subtitles,
                "narration_audio_base64": base64.b64encode(audio).decode("ascii"),
                "narration_words": words,
            },
        }
    }


@router.post("/jobs/{job_id}/heartbeat", dependencies=[WorkerAuth])
def heartbeat(job_id: str, req: WorkerRequest, db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    try:
        renew_video_render_lease(db, job, req.worker_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"status": job.status, "lease_seconds": LEASE_MINUTES * 60}


@router.post("/jobs/{job_id}/complete", dependencies=[WorkerAuth])
async def complete(
    job_id: str,
    request: Request,
    worker_id: str = Header(alias="X-Worker-Id", min_length=1, max_length=80,
                             pattern=r"^[A-Za-z0-9._-]+$"),
    content_sha256: str = Header(alias="X-Content-SHA256", min_length=64, max_length=64),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, job_id)
    if job.status == "ready":
        asset = db.get(VideoAsset, job.video_id)
        return {"status": "ready", "video_id": job.video_id, "video_url": asset.video_url if asset else None}
    if job.status != "processing" or job.lease_owner != worker_id:
        raise HTTPException(409, "tarefa não pertence a este worker ou não está em processamento")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "video/mp4":
        raise HTTPException(415, "envie o arquivo como video/mp4")
    if len(content_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in content_sha256):
        raise HTTPException(422, "X-Content-SHA256 inválido")

    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    with tempfile.TemporaryDirectory(prefix="doova-android-render-") as directory:
        path = Path(directory) / "output.mp4"
        with path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "MP4 excede o limite de 60 MB")
                if len(prefix) < 12:
                    prefix.extend(chunk[:12 - len(prefix)])
                digest.update(chunk)
                output.write(chunk)
        if size < 12 or bytes(prefix[4:8]) != b"ftyp":
            raise HTTPException(422, "o arquivo enviado não parece ser um MP4")
        if not hmac.compare_digest(digest.hexdigest().lower(), content_sha256.lower()):
            raise HTTPException(422, "hash SHA-256 do MP4 não corresponde")
        asset = db.get(VideoAsset, job.video_id)
        if asset is None:
            raise HTTPException(404, "vídeo associado à tarefa não encontrado")
        settings = get_settings()
        try:
            url, public_id = _upload_to_cloudinary(path, f"android-{job.id}", settings, overwrite=True)
        except VideoSearchError as e:
            raise HTTPException(502, str(e)) from e

        db.refresh(job)
        if job.status != "processing" or job.lease_owner != worker_id:
            raise HTTPException(409, "a reserva da tarefa expirou durante o upload")
        metadata = dict(asset.metadata_json or {})
        metadata["cloudinary_public_id"] = public_id
        metadata["android_renderer"] = "media3"
        metadata["render_sha256"] = digest.hexdigest()
        asset.video_url = url
        asset.mp4_bytes = None
        asset.metadata_json = metadata
        job.status = "ready"
        job.error = ""
        job.lease_owner = None
        job.lease_until = None
        db.commit()
    return {"status": "ready", "video_id": asset.id, "video_url": url}


@router.post("/jobs/{job_id}/fail", dependencies=[WorkerAuth])
def fail(job_id: str, req: FailureRequest, db: Session = Depends(get_db)):
    job = _job_or_404(db, job_id)
    try:
        fail_video_render(db, job, req.worker_id, req.error)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"status": job.status, "attempts": job.attempts, "error": job.error}
