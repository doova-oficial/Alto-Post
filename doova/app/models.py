"""Modelos de dados."""
from __future__ import annotations

import enum
import secrets
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class PostStatus(str, enum.Enum):
    DRAFT = "draft"
    CAPTION_FAILED = "caption_failed"      # Groq indisponível; nova tentativa controlada
    NEEDS_REVIEW = "needs_review"          # validação encontrou algo que exige revisão humana
    PENDING_APPROVAL = "pending_approval"  # aprovação manual obrigatória
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"              # reservado por um worker
    PROCESSING = "processing"              # contêiner criado na Meta, aguardando FINISHED
    VERIFYING = "verifying"                # resultado ambíguo; verificar antes de repetir
    PUBLISHED = "published"
    SIMULATED = "simulated"                # modo de simulação: nada foi publicado
    FAILED = "failed"
    REJECTED = "rejected"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    CANCELLED = "cancelled"


ACTIVE_PUBLISH_STATES = {PostStatus.PUBLISHING.value, PostStatus.PROCESSING.value, PostStatus.VERIFYING.value}
DONE_STATES = {PostStatus.PUBLISHED.value, PostStatus.SIMULATED.value}


class ContentKind(str, enum.Enum):
    INSTITUTIONAL = "institutional"
    EDUCATIONAL = "educational"
    ENGAGEMENT = "engagement"
    FEATURE = "feature"
    ACQUISITION = "acquisition"
    OPPORTUNITY = "opportunity"  # somente com oportunidade verificada
    TREND = "trend"              # assunto do momento (Google Trends); sempre passa por revisão


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    design: Mapped[str] = mapped_column(String(20))  # "ab" | "factorial"
    factors: Mapped[dict] = mapped_column(JSON)  # {"title_position": ["top","bottom"]}
    template_key: Mapped[str] = mapped_column(String(50))
    base_params: Mapped[dict] = mapped_column(JSON, default=dict)
    theme_keys: Mapped[list] = mapped_column(JSON, default=list)  # blocos
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    primary_metric: Mapped[str] = mapped_column(String(50), default="engagement_rate")
    caption_strategy: Mapped[str] = mapped_column(String(20), default="shared")
    min_days_between_block_posts: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Variant(Base):
    __tablename__ = "variants"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("var"))
    experiment_id: Mapped[int | None] = mapped_column(ForeignKey("experiments.id"), nullable=True, index=True)
    block_key: Mapped[str | None] = mapped_column(String(80), nullable=True)  # tema/bloco experimental
    template_key: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict] = mapped_column(JSON)  # parâmetros completos resolvidos
    factor_values: Mapped[dict] = mapped_column(JSON, default=dict)
    params_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artwork(Base):
    __tablename__ = "artworks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("art"))
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.id"), index=True)
    png_path: Mapped[str] = mapped_column(String(500))
    jpg_path: Mapped[str] = mapped_column(String(500))
    jpg_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # cópia para servir sem disco persistente
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(32))
    layout_report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    variant: Mapped[Variant] = relationship()


class Caption(Base):
    __tablename__ = "captions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="groq")  # groq | manual
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VerifiedOpportunity(Base):
    __tablename__ = "verified_opportunities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(300))  # como foi verificada (ex.: contato direto com o contratante)
    verified_by: Mapped[str] = mapped_column(String(120))
    verified_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class StockPhoto(Base):
    """Foto de banco gratuito (Openverse/Pixabay), baixada e guardada com origem e licença."""

    __tablename__ = "stock_photos"
    __table_args__ = (UniqueConstraint("provider", "provider_id", name="uq_stock_photo_provider"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("pho"))
    provider: Mapped[str] = mapped_column(String(20))           # openverse | pixabay
    provider_id: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    creator: Mapped[str] = mapped_column(String(200), default="")
    creator_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="")  # acervo de origem (flickr, wikimedia...)
    license: Mapped[str] = mapped_column(String(30))
    license_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    license_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    landing_url: Mapped[str] = mapped_column(String(500))
    download_url: Mapped[str] = mapped_column(String(800))
    attribution: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    jpg_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VideoAsset(Base):
    """Vídeo ilustrativo baixado de um banco gratuito e renderizado com texto."""

    __tablename__ = "video_assets"
    __table_args__ = (UniqueConstraint("provider", "provider_id", name="uq_video_asset_provider"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("vid"))
    provider: Mapped[str] = mapped_column(String(20))
    provider_id: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    creator: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(80), default="")
    license: Mapped[str] = mapped_column(String(40), default="pixabay")
    landing_url: Mapped[str] = mapped_column(String(500), default="")
    download_url: Mapped[str] = mapped_column(String(800), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    video_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mp4_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    subtitle_text: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(String(300), default="")
    trend_term: Mapped[str] = mapped_column(String(200), default="")
    theme_key: Mapped[str] = mapped_column(String(80), default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("post"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    variant_id: Mapped[str] = mapped_column(ForeignKey("variants.id"), index=True)
    artwork_id: Mapped[str] = mapped_column(ForeignKey("artworks.id"), index=True)
    caption_id: Mapped[int | None] = mapped_column(ForeignKey("captions.id"), nullable=True)
    experiment_id: Mapped[int | None] = mapped_column(ForeignKey("experiments.id"), nullable=True, index=True)
    theme_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    topic: Mapped[str] = mapped_column(String(300), default="")
    content_kind: Mapped[str] = mapped_column(String(30), default=ContentKind.INSTITUTIONAL.value)
    message_type: Mapped[str] = mapped_column(String(30), default="informacao")
    audience: Mapped[str] = mapped_column(String(50), default="ambos")
    objective: Mapped[str] = mapped_column(String(300), default="")
    alt_text: Mapped[str] = mapped_column(String(1000), default="")
    verified_opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("verified_opportunities.id"), nullable=True)
    photo_id: Mapped[str | None] = mapped_column(ForeignKey("stock_photos.id"), nullable=True, index=True)
    trend_term: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # assunto do momento

    status: Mapped[str] = mapped_column(String(30), default=PostStatus.DRAFT.value, index=True)
    review_notes: Mapped[list] = mapped_column(JSON, default=list)
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)

    ig_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ig_media_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    permalink: Mapped[str | None] = mapped_column(String(500), nullable=True)
    container_checks: Mapped[int] = mapped_column(Integer, default=0)
    publish_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    caption_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    variant: Mapped[Variant] = relationship()
    artwork: Mapped[Artwork] = relationship()
    caption: Mapped[Caption | None] = relationship()
    campaign: Mapped[Campaign | None] = relationship()
    photo: Mapped[StockPhoto | None] = relationship()


class PostEvent(Base):
    __tablename__ = "post_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text, default="")


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    age_hours: Mapped[float] = mapped_column(Float)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saved: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_interactions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_visits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_activity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    follows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unavailable: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class JobLock(Base):
    __tablename__ = "job_locks"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class StoredToken(Base):
    __tablename__ = "stored_tokens"
    __table_args__ = (UniqueConstraint("name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
