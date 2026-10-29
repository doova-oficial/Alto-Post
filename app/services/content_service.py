"""Preparação e gestão do conteúdo: arte, legenda, validação, aprovação e agenda.

Fluxo de estados de uma publicação::

    draft ──► (legenda) ──► pending_approval ──aprovar──► approved ──agendar──► scheduled ──► publishing
      │            │                │                                            ▲              │
      │            ├─ caption_failed (Groq indisponível; nova tentativa com backoff)            ▼
      │            └─ needs_review (validação encontrou problemas)               processing ─► published
      └─ rejected / cancelled / duplicate_blocked                                (ou simulated no modo de simulação)

Nada chega a ``scheduled`` sem arte gerada, legenda válida e (se configurado) aprovação manual.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import (
    Artwork,
    Campaign,
    Caption,
    ContentKind,
    Post,
    PostEvent,
    PostStatus,
    Variant,
    VerifiedOpportunity,
    new_id,
)
from app.services.caption_validator import BANNED_PATTERNS, validate_caption
from app.services.groq_service import CaptionGenerationError, CaptionRequest, GroqCaptionService
from app.services.image_generator import generate, params_hash
from app.services.template_config import TemplateConfig, TemplateParams, get_template_config
from app.services.text_layout import TextOverflowError
from app.timeutil import local_to_utc, tz, utc_to_local

logger = logging.getLogger(__name__)

import io as _io  # noqa: E402

from PIL import Image as _PILImage  # noqa: E402

from app.models import StockPhoto  # noqa: E402
from app.services.image_search import photo_auto_issues, short_credit, with_photo_credit  # noqa: E402

PHOTO_CAPTION_NOTE = (
    "A arte usa uma foto ilustrativa de banco de imagens. Não diga nem sugira que a foto mostra usuários, "
    "prestadores, clientes ou trabalhos feitos pelo Doova. Não escreva o crédito da foto: ele é adicionado "
    "automaticamente."
)

AUDIENCES = {
    "prestadores": "pessoas que oferecem serviços: trabalhadores autônomos e prestadores de serviços",
    "contratantes": "pessoas que procuram contratar serviços",
    "ambos": "pessoas que oferecem serviços e pessoas que procuram contratá-los",
}
KINDS = {k.value for k in ContentKind}
MESSAGE_TYPES = {"beneficio", "pergunta", "informacao", "chamada"}

EDITABLE_CAPTION_STATES = {PostStatus.DRAFT.value, PostStatus.CAPTION_FAILED.value, PostStatus.NEEDS_REVIEW.value,
                           PostStatus.PENDING_APPROVAL.value, PostStatus.APPROVED.value, PostStatus.SCHEDULED.value}
APPROVABLE_STATES = {PostStatus.PENDING_APPROVAL.value, PostStatus.NEEDS_REVIEW.value}
REJECTABLE_STATES = EDITABLE_CAPTION_STATES | {PostStatus.FAILED.value, PostStatus.DUPLICATE_BLOCKED.value}
DUPLICATE_SCOPE = {PostStatus.APPROVED.value, PostStatus.SCHEDULED.value, PostStatus.PUBLISHING.value,
                   PostStatus.PROCESSING.value, PostStatus.VERIFYING.value, PostStatus.PUBLISHED.value}


class ContentError(ValueError):
    """Entrada inválida ou operação não permitida."""


class StateError(ContentError):
    """Transição de estado não permitida."""


@dataclass
class PostInput:
    template_key: str
    overrides: dict[str, Any] = field(default_factory=dict)
    topic: str = ""
    audience: str = "ambos"
    objective: str = "apresentar o Doova e atrair os primeiros usuários reais"
    theme_key: str | None = None
    campaign_slug: str | None = None
    content_kind: str = ContentKind.INSTITUTIONAL.value
    message_type: str | None = None
    caption: str | None = None
    generate_caption: bool = True
    extra_context: str | None = None
    scheduled_at: datetime | None = None
    verified_opportunity_id: int | None = None
    alt_text: str | None = None
    experiment_id: int | None = None
    block_key: str | None = None
    factor_values: dict[str, Any] = field(default_factory=dict)
    shared_caption_id: int | None = None
    photo_id: str | None = None
    trend_term: str | None = None
    expires_in_hours: int | None = None
    extra_review_notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ util
def set_status(db: Session, post: Post, status: PostStatus | str, message: str = "") -> None:
    new = status.value if isinstance(status, PostStatus) else status
    old = post.status
    post.status = new
    post.updated_at = utcnow()
    db.add(PostEvent(post_id=post.id, from_status=old, to_status=new, message=message[:2000]))
    logger.info("post %s: %s -> %s %s", post.id, old, new, message[:200])


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    return re.sub(r"\s+", " ", s).strip()


def compute_content_hash(variant_hash: str, caption_text: str) -> str:
    return hashlib.sha256(f"{variant_hash}|{normalize_text(caption_text)}".encode()).hexdigest()


def art_text(params: TemplateParams | dict) -> str:
    p = params if isinstance(params, dict) else params.model_dump()
    parts = [p.get("tag"), p.get("title"), p.get("subtitle"), p.get("body")] + list(p.get("items") or [])
    return " | ".join(x for x in parts if x)


def check_art_text(text: str, verified: bool) -> list[str]:
    notes = []
    for pat, label in BANNED_PATTERNS:
        if label.startswith("vaga") and verified:
            continue
        if pat.search(text or ""):
            notes.append(f"texto da arte: {label}")
    return notes


def get_or_create_campaign(db: Session, slug: str, name: str | None = None, objective: str = "",
                           audience: str = "") -> Campaign:
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug.lower()).strip("-")[:80] or "geral"
    c = db.scalar(select(Campaign).where(Campaign.slug == slug))
    if c is None:
        c = Campaign(slug=slug, name=name or slug, objective=objective, audience=audience)
        db.add(c)
        db.flush()
    return c


def find_duplicate(db: Session, post: Post) -> Post | None:
    if not post.content_hash:
        return None
    return db.scalar(select(Post).where(Post.content_hash == post.content_hash, Post.id != post.id,
                                        Post.status.in_(DUPLICATE_SCOPE)))


class ContentService:
    def __init__(self, settings: Settings | None = None, caption_service: GroqCaptionService | None = None,
                 template_config: TemplateConfig | None = None):
        self.settings = settings or get_settings()
        self.captions = caption_service or GroqCaptionService(self.settings)
        self.cfg = template_config or get_template_config()

    # ------------------------------------------------------------- artes
    def render_variant(self, db: Session, template_key: str, overrides: dict[str, Any], out_dir,
                       experiment_id: int | None = None, block_key: str | None = None,
                       factor_values: dict[str, Any] | None = None,
                       artwork_id: str | None = None) -> tuple[Variant, Artwork, TemplateParams]:
        try:
            params = self.cfg.resolve(template_key, overrides)
        except KeyError as e:
            raise ContentError(str(e)) from e
        except ValueError as e:
            raise ContentError(f"parâmetros inválidos: {e}") from e
        photo_img = None
        if params.photo_id:
            photo = db.get(StockPhoto, params.photo_id)
            if photo is None:
                raise ContentError(f"foto inexistente: {params.photo_id}")
            params = params.model_copy(update={"photo_credit": short_credit(photo)})
            photo_img = _PILImage.open(_io.BytesIO(photo.jpg_bytes))
        elif self.cfg.requires_photo(template_key):
            raise ContentError(f"o modelo '{template_key}' exige uma foto (photo_id)")
        try:
            img = generate(params, out_dir, self.cfg, photo=photo_img)
        except TextOverflowError as e:
            raise ContentError(f"a arte não foi gerada: {e}") from e
        variant = Variant(experiment_id=experiment_id, block_key=block_key, template_key=template_key,
                          params=params.model_dump(), factor_values=factor_values or {},
                          params_hash=params_hash(params))
        db.add(variant)
        db.flush()
        art = Artwork(id=artwork_id or new_id("art"), variant_id=variant.id, png_path=str(img.png_path), jpg_path=str(img.jpg_path),
                      jpg_bytes=img.jpg_bytes, width=img.width, height=img.height, sha256=img.sha256,
                      layout_report=img.report)
        db.add(art)
        db.flush()
        return variant, art, params

    def create_art(self, db: Session, template_key: str, overrides: dict[str, Any]) -> Artwork:
        day = utcnow().replace(tzinfo=None)
        art_id = new_id("art")
        out = self.settings.generated_dir / "avulsas" / utc_to_local(day).strftime("%Y-%m-%d") / art_id
        _, art, _ = self.render_variant(db, template_key, overrides, out, artwork_id=art_id)
        db.commit()
        return art

    # ------------------------------------------------------------ criação
    def _check_opportunity(self, db: Session, kind: str, opp_id: int | None) -> VerifiedOpportunity | None:
        if kind == ContentKind.OPPORTUNITY.value:
            if not opp_id:
                raise ContentError("conteúdo do tipo 'opportunity' exige uma oportunidade verificada")
            opp = db.get(VerifiedOpportunity, opp_id)
            if opp is None or not opp.active:
                raise ContentError("oportunidade inexistente ou desativada")
            if opp.expires_at and opp.expires_at <= utcnow():
                raise ContentError("oportunidade verificada já expirou")
            return opp
        if opp_id:
            raise ContentError("somente conteúdo do tipo 'opportunity' pode ser vinculado a uma oportunidade")
        return None

    def create_post(self, db: Session, data: PostInput) -> Post:
        s = self.settings
        if data.audience not in AUDIENCES:
            raise ContentError(f"público inválido; use um de {sorted(AUDIENCES)}")
        if data.content_kind not in KINDS:
            raise ContentError(f"tipo de conteúdo inválido; use um de {sorted(KINDS)}")
        if data.message_type and data.message_type not in MESSAGE_TYPES:
            raise ContentError(f"tipo de mensagem inválido; use um de {sorted(MESSAGE_TYPES)}")
        if data.template_key not in self.cfg.templates:
            raise ContentError(f"template desconhecido: {data.template_key}")
        opp = self._check_opportunity(db, data.content_kind, data.verified_opportunity_id)
        scheduled = None
        if data.scheduled_at is not None:
            scheduled = local_to_utc(data.scheduled_at)
            if scheduled <= utcnow():
                raise ContentError("o horário de agendamento precisa estar no futuro")
        campaign = get_or_create_campaign(db, data.campaign_slug) if data.campaign_slug else None

        post_id = new_id("post")
        local_day = utc_to_local(utcnow()).strftime("%Y-%m-%d")
        out_dir = s.generated_dir / (campaign.slug if campaign else "sem-campanha") / local_day / post_id
        overrides = dict(data.overrides)
        if data.photo_id:
            overrides["photo_id"] = data.photo_id
        variant, art, params = self.render_variant(db, data.template_key, overrides, out_dir,
                                                   data.experiment_id, data.block_key, data.factor_values)
        tpl = self.cfg.templates[data.template_key]
        alt = data.alt_text or f"Arte do Doova com o texto: {art_text(params)}"
        post = Post(id=post_id, campaign_id=campaign.id if campaign else None, variant_id=variant.id,
                    artwork_id=art.id, experiment_id=data.experiment_id, theme_key=data.theme_key,
                    topic=data.topic or params.title, content_kind=data.content_kind,
                    message_type=data.message_type or tpl.get("message_type", "informacao"),
                    audience=data.audience, objective=data.objective, alt_text=alt[:1000],
                    verified_opportunity_id=opp.id if opp else None, scheduled_at=scheduled,
                    dry_run=s.dry_run, status=PostStatus.DRAFT.value, review_notes=[],
                    photo_id=params.photo_id, trend_term=data.trend_term,
                    expires_at=(utcnow() + timedelta(hours=data.expires_in_hours)) if data.expires_in_hours else None)
        db.add(post)
        db.flush()
        db.add(PostEvent(post_id=post.id, from_status=None, to_status=PostStatus.DRAFT.value,
                         message=f"arte {art.id} gerada (variante {variant.id})"))
        notes = check_art_text(art_text(params), verified=opp is not None)
        if art.layout_report.get("font_fallback_used"):
            notes.append("fonte de fallback utilizada na arte; confira a aparência")
        notes += list(data.extra_review_notes or [])
        if params.photo_id:
            ph = db.get(StockPhoto, params.photo_id)
            if s.require_manual_approval:
                notes.append(f"foto de banco externo ({ph.provider}): confira se mostra pessoas identificáveis ou marcas "
                             f"e confirme a licença em {ph.landing_url}")
            else:
                notes += photo_auto_issues(ph)  # vazio = foto conferida automaticamente
        post.review_notes = notes
        db.commit()

        if data.caption:
            self.set_manual_caption(db, post, data.caption, by="criação")
        elif data.shared_caption_id:
            cap = db.get(Caption, data.shared_caption_id)
            if cap is None:
                raise ContentError("legenda compartilhada inexistente")
            self._attach_caption(db, post, cap)
        elif data.generate_caption:
            self.attempt_caption(db, post, extra_context=data.extra_context)
        db.commit()
        db.refresh(post)
        return post

    # ----------------------------------------------------------- legendas
    def caption_request(self, db: Session, post: Post, extra_context: str | None = None) -> CaptionRequest:
        params = post.variant.params
        if post.photo_id:
            extra_context = f"{extra_context}\n{PHOTO_CAPTION_NOTE}" if extra_context else PHOTO_CAPTION_NOTE
        opp = db.get(VerifiedOpportunity, post.verified_opportunity_id) if post.verified_opportunity_id else None
        return CaptionRequest(
            topic=post.topic, audience=AUDIENCES.get(post.audience, post.audience), objective=post.objective,
            art_text=art_text(params), template=self.cfg.templates.get(post.variant.template_key, {}).get(
                "name", post.variant.template_key),
            cta=params.get("cta"), message_type=post.message_type, extra_context=extra_context,
            verified_opportunity=f"{opp.title}: {opp.description}" if opp else None,
        )

    def attempt_caption(self, db: Session, post: Post, extra_context: str | None = None) -> bool:
        s = self.settings
        post.caption_attempts = (post.caption_attempts or 0) + 1
        try:
            res = self.captions.generate(self.caption_request(db, post, extra_context))
        except CaptionGenerationError as e:
            post.last_error = f"legenda: {e}"
            if e.retryable and post.caption_attempts < s.caption_max_attempts:
                delay = timedelta(minutes=5 * 2 ** (post.caption_attempts - 1))
                post.next_attempt_at = utcnow() + delay
                set_status(db, post, PostStatus.CAPTION_FAILED,
                           f"Groq indisponível ({e}); nova tentativa em {int(delay.total_seconds() // 60)} min")
            else:
                post.next_attempt_at = None
                post.review_notes = list(post.review_notes or []) + [f"legenda não gerada: {e}"]
                set_status(db, post, PostStatus.NEEDS_REVIEW, f"legenda não gerada: {e}")
            db.commit()
            return False
        text, valid, review, issues = res.text, res.check.is_valid, res.check.needs_review, list(res.check.issues)
        if post.photo_id:  # crédito completo da foto (exigência da licença) no fim da legenda
            chk = validate_caption(with_photo_credit(text, db.get(StockPhoto, post.photo_id)),
                                   allow_emojis=s.caption_allow_emojis, max_hashtags=s.caption_max_hashtags,
                                   min_chars=s.caption_min_chars, max_chars=s.caption_max_chars,
                                   verified_opportunity=bool(post.verified_opportunity_id))
            text, valid, review = chk.text, valid and chk.is_valid, review or chk.needs_review
            issues = list(dict.fromkeys(issues + chk.issues))
        if (not valid or review) and s.autonomous and post.caption_attempts < s.caption_max_attempts:
            # modo automático: em vez de esperar revisão, pede outra legenda explicando o problema
            db.add(PostEvent(post_id=post.id, from_status=post.status, to_status=post.status,
                             message="legenda recusada pela validação (" + "; ".join(issues) + "); gerando outra"))
            hint = ("A legenda anterior foi recusada pela validação automática por: " + "; ".join(issues)
                    + ". Escreva outra que evite esses problemas.")
            db.commit()
            return self.attempt_caption(db, post, extra_context=f"{extra_context}\n{hint}" if extra_context else hint)
        cap = Caption(text=text, raw_text=res.raw_text, source="groq", model=res.model,
                      prompt_version=res.prompt_version, is_valid=valid, needs_review=review, issues=issues)
        db.add(cap)
        db.flush()
        post.next_attempt_at = None
        post.last_error = None
        self._attach_caption(db, post, cap)
        db.commit()
        return True

    def _attach_caption(self, db: Session, post: Post, cap: Caption) -> None:
        post.caption_id = cap.id
        post.caption = cap
        post.content_hash = compute_content_hash(post.variant.params_hash, cap.text)
        self.finalize_status(db, post)

    def set_manual_caption(self, db: Session, post: Post, text: str, by: str = "admin") -> Caption:
        if post.status not in EDITABLE_CAPTION_STATES:
            raise StateError(f"a legenda não pode ser alterada no estado {post.status}")
        s = self.settings
        if post.photo_id:
            text = with_photo_credit(text, db.get(StockPhoto, post.photo_id))
        chk = validate_caption(text, allow_emojis=s.caption_allow_emojis, max_hashtags=s.caption_max_hashtags,
                               min_chars=s.caption_min_chars, max_chars=s.caption_max_chars,
                               verified_opportunity=bool(post.verified_opportunity_id))
        if not chk.text:
            raise ContentError("legenda vazia")
        cap = Caption(text=chk.text, raw_text=text, source="manual", is_valid=chk.is_valid,
                      needs_review=chk.needs_review, issues=chk.issues)
        db.add(cap)
        db.flush()
        # alterar a legenda de algo já aprovado exige nova aprovação
        post.approved_by = None
        post.approved_at = None
        db.add(PostEvent(post_id=post.id, from_status=post.status, to_status=post.status,
                         message=f"legenda definida manualmente por {by}"))
        self._attach_caption(db, post, cap)
        db.commit()
        return cap

    def regenerate_caption(self, db: Session, post: Post, extra_context: str | None = None) -> bool:
        if post.status not in EDITABLE_CAPTION_STATES:
            raise StateError(f"a legenda não pode ser regerada no estado {post.status}")
        post.caption_attempts = 0
        post.approved_by = None
        post.approved_at = None
        return self.attempt_caption(db, post, extra_context)

    def retry_failed_captions(self, db: Session, now: datetime | None = None, limit: int = 5) -> int:
        now = now or utcnow()
        posts = db.scalars(select(Post).where(Post.status == PostStatus.CAPTION_FAILED.value,
                                              Post.next_attempt_at <= now).limit(limit)).all()
        ok = 0
        for p in posts:
            ok += int(self.attempt_caption(db, p))
        return ok

    def finalize_status(self, db: Session, post: Post) -> None:
        cap = post.caption
        if cap is None:
            return
        if not cap.is_valid or cap.needs_review:
            set_status(db, post, PostStatus.NEEDS_REVIEW, "legenda requer revisão: " + "; ".join(cap.issues))
        elif post.review_notes:
            set_status(db, post, PostStatus.NEEDS_REVIEW, "revisão necessária: " + "; ".join(post.review_notes))
        elif self.settings.require_manual_approval:
            set_status(db, post, PostStatus.PENDING_APPROVAL, "aguardando aprovação manual")
        else:
            self._approve(db, post, "automático")

    # ---------------------------------------------------------- aprovação
    def _approve(self, db: Session, post: Post, by: str) -> None:
        if post.caption is None or not post.caption.text or not post.caption.is_valid:
            raise StateError("não é possível aprovar sem uma legenda válida; edite a legenda primeiro")
        if post.artwork is None or not (post.artwork.jpg_bytes or post.artwork.jpg_path):
            raise StateError("não é possível aprovar sem a imagem gerada")
        dup = find_duplicate(db, post)
        if dup is not None:
            set_status(db, post, PostStatus.DUPLICATE_BLOCKED, f"conteúdo idêntico ao de {dup.id}")
            return
        post.approved_by = by
        post.approved_at = utcnow()
        if post.scheduled_at:
            set_status(db, post, PostStatus.SCHEDULED, f"aprovada por {by}; agendada")
        else:
            set_status(db, post, PostStatus.APPROVED, f"aprovada por {by}")

    def approve(self, db: Session, post: Post, by: str = "admin") -> Post:
        if post.status not in APPROVABLE_STATES:
            raise StateError(f"não é possível aprovar no estado {post.status}")
        post.review_notes = []
        self._approve(db, post, by)
        db.commit()
        return post

    def reject(self, db: Session, post: Post, reason: str, by: str = "admin") -> Post:
        if post.status not in REJECTABLE_STATES:
            raise StateError(f"não é possível rejeitar no estado {post.status}")
        set_status(db, post, PostStatus.REJECTED, f"rejeitada por {by}: {reason}")
        db.commit()
        return post

    def cancel(self, db: Session, post: Post, by: str = "admin") -> Post:
        if post.status not in {PostStatus.APPROVED.value, PostStatus.SCHEDULED.value, PostStatus.CAPTION_FAILED.value,
                               PostStatus.PENDING_APPROVAL.value, PostStatus.DRAFT.value}:
            raise StateError(f"não é possível cancelar no estado {post.status}")
        set_status(db, post, PostStatus.CANCELLED, f"cancelada por {by}")
        db.commit()
        return post

    def schedule(self, db: Session, post: Post, when: datetime) -> Post:
        when_utc = local_to_utc(when)
        if when_utc <= utcnow():
            raise ContentError("o horário precisa estar no futuro")
        if post.status in {PostStatus.APPROVED.value, PostStatus.SCHEDULED.value}:
            post.scheduled_at = when_utc
            post.next_attempt_at = None
            set_status(db, post, PostStatus.SCHEDULED, f"agendada para {utc_to_local(when_utc).isoformat()}")
        elif post.status in {PostStatus.PENDING_APPROVAL.value, PostStatus.NEEDS_REVIEW.value, PostStatus.DRAFT.value,
                             PostStatus.CAPTION_FAILED.value}:
            post.scheduled_at = when_utc  # será agendada quando aprovada
            db.add(PostEvent(post_id=post.id, from_status=post.status, to_status=post.status,
                             message=f"horário reservado: {utc_to_local(when_utc).isoformat()} (aguardando aprovação)"))
        else:
            raise StateError(f"não é possível agendar no estado {post.status}")
        db.commit()
        return post

    # ------------------------------------------------------ agenda automática
    def next_free_slot(self, db: Session, now: datetime | None = None, horizon_days: int = 30) -> datetime | None:
        """Próximo horário livre. No modo adaptativo a ordem dos horários vem do que o sistema aprendeu."""
        from app.services import timing

        s = self.settings
        now = now or utcnow()
        busy = [t for (t,) in db.execute(select(Post.scheduled_at).where(
            Post.status.in_([PostStatus.SCHEDULED.value, PostStatus.PUBLISHING.value, PostStatus.PROCESSING.value]),
            Post.scheduled_at.is_not(None)))]
        busy += [t for (t,) in db.execute(select(Post.published_at).where(
            Post.published_at.is_not(None), Post.published_at >= now - timedelta(days=2)))]
        stats = timing.slot_stats(db, s) if s.posting_strategy == "adaptive" else None
        local_now = utc_to_local(now)
        for d in range(horizon_days):
            day = (local_now + timedelta(days=d)).date()
            if day.weekday() not in s.weekdays():
                continue
            if len([b for b in busy if utc_to_local(b).date() == day]) >= s.max_posts_per_day:
                continue
            slots = timing.ranked_slots(stats, s, day) if stats is not None else s.slot_times()
            for h, m in slots:
                cand = local_to_utc(datetime(day.year, day.month, day.day, h, m, tzinfo=tz()))
                if cand <= now + timedelta(minutes=10):
                    continue
                if any(abs((cand - b).total_seconds()) < s.min_minutes_between_posts * 60 for b in busy):
                    continue
                return cand
        return None

    def auto_schedule(self, db: Session, now: datetime | None = None) -> int:
        """Coloca na fila as aprovadas sem horário. Assuntos do momento vão primeiro."""
        s = self.settings
        if not s.auto_schedule:
            return 0
        now = now or utcnow()
        posts = list(db.scalars(select(Post).where(Post.status == PostStatus.APPROVED.value,
                                                   Post.scheduled_at.is_(None))).all())
        far = datetime.max
        posts.sort(key=lambda p: (p.content_kind != ContentKind.TREND.value, p.expires_at or far, p.approved_at or far))
        n = 0
        for p in posts:
            slot = self.next_free_slot(db, now)
            note = "agendamento automático"
            if p.expires_at and s.trend_publish_asap:
                # assunto do momento esfria rápido: se o próximo horário está a mais de 3 h, sai já,
                # exceto de madrugada (23h-07h), quando espera o próximo horário
                soon = now + timedelta(minutes=5)
                quiet = not (7 <= utc_to_local(soon).hour < 23)
                limit = min(now + timedelta(hours=3), p.expires_at - timedelta(hours=2))
                if (slot is None or slot > limit) and not quiet:
                    slot = soon
                    note = "assunto do momento: o quanto antes (respeitando intervalo mínimo e limite diário)"
            if slot is None:
                break
            p.scheduled_at = slot
            set_status(db, p, PostStatus.SCHEDULED, f"{note} para {utc_to_local(slot).isoformat()}")
            db.flush()
            n += 1
        db.commit()
        return n

    def fill_missing_captions(self, db: Session, limit: int = 3) -> int:
        """Modo automático: gera a legenda de rascunhos que ficaram sem (ex.: criados sem chave da Groq)."""
        s = self.settings
        if not s.autonomous or not s.groq_configured:
            return 0
        posts = db.scalars(select(Post).where(Post.status == PostStatus.DRAFT.value, Post.caption_id.is_(None))
                           .order_by(Post.created_at).limit(limit)).all()
        return sum(int(self.attempt_caption(db, p)) for p in posts)

    def approve_pending(self, db: Session) -> int:
        """Modo automático: aprova o que ficou aguardando aprovação de quando o modo manual estava ligado."""
        if not self.settings.autonomous:
            return 0
        n = 0
        for p in db.scalars(select(Post).where(Post.status == PostStatus.PENDING_APPROVAL.value)).all():
            cap = p.caption
            if p.review_notes or cap is None or not cap.is_valid or cap.needs_review:
                continue
            try:
                self._approve(db, p, "automático")
                n += 1
            except StateError:
                continue
        db.commit()
        return n
