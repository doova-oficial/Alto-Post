"""Esquemas de entrada e saída da API administrativa."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Post
from app.timeutil import iso_utc, utc_to_local

Audience = Literal["prestadores", "contratantes", "ambos"]
Kind = Literal["institutional", "educational", "engagement", "feature", "acquisition", "opportunity", "trend"]
MessageType = Literal["beneficio", "pergunta", "informacao", "chamada"]


class ArtParams(BaseModel):
    """Parâmetros opcionais da arte; o que não for informado vem de templates.yaml."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=160)
    subtitle: str | None = Field(default=None, max_length=240)
    body: str | None = Field(default=None, max_length=600)
    items: list[str] | None = Field(default=None, max_length=4)
    cta: str | None = Field(default=None, max_length=60)
    tag: str | None = Field(default=None, max_length=48)
    primary_color: str | None = None
    accent_color: str | None = None
    text_color: str | None = None
    background: Literal["gradient", "solid", "light", "dark"] | None = None
    title_position: Literal["top", "center", "bottom"] | None = None
    subtitle_position: Literal["above_title", "below_title"] | None = None
    text_align: Literal["left", "center"] | None = None
    logo_position: Literal["top_left", "top_right"] | None = None
    cta_align: Literal["left", "center"] | None = None
    title_size: int | None = Field(default=None, ge=40, le=180)
    subtitle_size: int | None = Field(default=None, ge=24, le=90)
    body_size: int | None = Field(default=None, ge=22, le=70)
    cta_size: int | None = Field(default=None, ge=22, le=60)
    margin: int | None = Field(default=None, ge=48, le=160)
    shapes: bool | None = None
    show_logo: bool | None = None
    show_location: bool | None = None
    app_ui: bool | None = None   # elementos gráficos do app (chips, stories, pins, navegação)
    item_style: Literal["cards", "numbered", "checks"] | None = None
    photo_id: str | None = Field(default=None, max_length=40, description="foto importada (GET /api/photos)")
    photo_focus_y: float | None = Field(default=None, ge=0, le=1)

    def overrides(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ArtRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=2, max_length=50)
    params: ArtParams = Field(default_factory=ArtParams)


class CaptionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=3, max_length=300)
    audience: Audience = "ambos"
    objective: str = Field(default="apresentar o Doova", max_length=300)
    art_text: str = Field(min_length=3, max_length=800)
    template: str = Field(default="institucional", max_length=50)
    cta: str | None = Field(default=None, max_length=60)
    message_type: MessageType | None = None
    extra_context: str | None = Field(default=None, max_length=1000)


class PostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str | None = Field(default=None, max_length=50, description="obrigatório se theme_key não for usado")
    theme_key: str | None = Field(default=None, max_length=80, description="tema da biblioteca (content/themes.yaml)")
    params: ArtParams = Field(default_factory=ArtParams)
    topic: str | None = Field(default=None, max_length=300)
    audience: Audience | None = None
    objective: str | None = Field(default=None, max_length=300)
    campaign: str | None = Field(default=None, max_length=80)
    content_kind: Kind | None = None
    message_type: MessageType | None = None
    caption: str | None = Field(default=None, max_length=2200, description="legenda manual (dispensa a Groq)")
    generate_caption: bool = True
    extra_context: str | None = Field(default=None, max_length=1000)
    scheduled_at: datetime | None = Field(default=None, description="horário local (TIMEZONE) se sem fuso")
    verified_opportunity_id: int | None = None
    alt_text: str | None = Field(default=None, max_length=1000)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_by: str = Field(default="admin", min_length=1, max_length=120)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)
    rejected_by: str = Field(default="admin", max_length=120)


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_at: datetime = Field(description="horário local (TIMEZONE) se sem fuso")


class CaptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str = Field(min_length=1, max_length=2200)


class RegenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_context: str | None = Field(default=None, max_length=1000)


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=300)


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_\-]+$")
    factors: dict[str, list[Any]] = Field(description='ex.: {"title_position": ["top", "bottom"]}')
    theme_keys: list[str] = Field(min_length=1, max_length=30, description="temas usados como blocos")
    template: str | None = None
    base_params: ArtParams = Field(default_factory=ArtParams)
    hypothesis: str = Field(default="", max_length=1000)
    primary_metric: str = "engagement_rate"
    caption_strategy: Literal["shared", "per_post"] = "shared"
    min_days_between_block_posts: int = Field(default=1, ge=1, le=14)


class ExperimentSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    seed: int | None = None


class OpportunityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=1500)
    source: str = Field(min_length=5, max_length=300, description="como foi verificada")
    verified_by: str = Field(min_length=2, max_length=120)
    expires_at: datetime | None = None


def post_out(p: Post, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": p.id, "status": p.status, "topic": p.topic, "theme_key": p.theme_key, "template": p.variant.template_key,
        "content_kind": p.content_kind, "message_type": p.message_type, "audience": p.audience,
        "campaign": p.campaign.slug if p.campaign else None, "experiment_id": p.experiment_id,
        "variant_id": p.variant_id, "artwork_id": p.artwork_id,
        "factor_values": p.variant.factor_values, "caption": p.caption.text if p.caption else None,
        "caption_issues": p.caption.issues if p.caption else [], "review_notes": p.review_notes,
        "scheduled_at_local": utc_to_local(p.scheduled_at).isoformat() if p.scheduled_at else None,
        "published_at": iso_utc(p.published_at), "dry_run": p.dry_run, "ig_media_id": p.ig_media_id,
        "permalink": p.permalink, "attempts": p.attempts, "last_error": p.last_error,
        "approved_by": p.approved_by, "image_path": p.artwork.png_path, "created_at": iso_utc(p.created_at),
        "photo_id": p.photo_id, "trend_term": p.trend_term, "expires_at": iso_utc(p.expires_at),
    }
    if detail:
        out["params"] = p.variant.params
        out["layout_report"] = p.artwork.layout_report
        out["alt_text"] = p.alt_text
    return out