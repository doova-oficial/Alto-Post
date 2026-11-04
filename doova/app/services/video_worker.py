"""Fila de renderização de vídeos no worker Android."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import utcnow
from app.models import VideoAsset, VideoRenderJob

LEASE_MINUTES = 20
MAX_ATTEMPTS = 5


def enqueue_video_render(db: Session, asset: VideoAsset) -> VideoRenderJob:
    job = db.scalar(select(VideoRenderJob).where(VideoRenderJob.video_id == asset.id))
    if job is None:
        job = VideoRenderJob(video_id=asset.id)
        db.add(job)
        db.commit()
        db.refresh(job)
    elif job.status == "failed":
        job.status = "pending"
        job.error = ""
        job.lease_owner = None
        job.lease_until = None
        db.commit()
        db.refresh(job)
    return job


def claim_video_render(db: Session, worker_id: str) -> tuple[VideoRenderJob, VideoAsset] | None:
    now = utcnow()
    job = db.scalar(
        select(VideoRenderJob)
        .where(
            VideoRenderJob.attempts < MAX_ATTEMPTS,
            or_(
                VideoRenderJob.status == "pending",
                (VideoRenderJob.status == "processing") & (VideoRenderJob.lease_until <= now),
            ),
        )
        .order_by(VideoRenderJob.created_at, VideoRenderJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    asset = db.get(VideoAsset, job.video_id)
    if asset is None:
        job.status = "failed"
        job.error = "registro do vídeo não encontrado"
        job.lease_until = None
        db.commit()
        return None
    job.status = "processing"
    job.lease_owner = worker_id
    job.lease_until = now + timedelta(minutes=LEASE_MINUTES)
    job.attempts += 1
    job.error = ""
    db.commit()
    db.refresh(job)
    return job, asset


def renew_video_render_lease(db: Session, job: VideoRenderJob, worker_id: str) -> None:
    if job.status != "processing" or job.lease_owner != worker_id:
        raise ValueError("tarefa não pertence a este worker")
    job.lease_until = utcnow() + timedelta(minutes=LEASE_MINUTES)
    db.commit()


def fail_video_render(db: Session, job: VideoRenderJob, worker_id: str, error: str) -> None:
    if job.status != "processing" or job.lease_owner != worker_id:
        raise ValueError("tarefa não pertence a este worker")
    job.status = "failed" if job.attempts >= MAX_ATTEMPTS else "pending"
    job.error = error[:1000]
    job.lease_owner = None
    job.lease_until = None
    db.commit()
