"""Fila de publicação no Instagram.

Proteções implementadas:
* somente publicações em ``scheduled`` (aprovadas, com arte e legenda válidas) entram na fila;
* nova validação imediatamente antes de publicar (pré-voo) — problemas levam a ``needs_review``;
* reserva atômica no banco (``UPDATE ... WHERE status='scheduled'``) + trava de tarefa,
  impedindo que dois processos publiquem a mesma publicação;
* bloqueio de conteúdo idêntico (hash da variante + legenda) e ``ig_media_id`` único;
* limite próprio de frequência (por 24 h e intervalo mínimo) e consulta ao limite da Meta;
* tentativas limitadas com backoff; falhas ambíguas vão para ``verifying`` e só são repetidas
  depois de confirmar na conta que a publicação não aconteceu;
* modo de simulação (``DRY_RUN=true``): percorre o mesmo fluxo e nada é enviado à Meta;
* pausa global persistida no banco.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, update

from app.config import Settings, get_settings
from app.database import session_scope, utcnow
from app.models import Post, PostEvent, PostStatus, VerifiedOpportunity
from app.services.caption_validator import validate_caption
from app.services.content_service import find_duplicate, set_status
from app.services.meta_service import MetaError, MetaService
from app.services.state import acquire_lock, is_paused, release_lock

logger = logging.getLogger(__name__)
STALE_LOCK_MINUTES = 15
PUBLISH_LOCK = "publisher"


class PublisherBusy(RuntimeError):
    pass


from app.models import StockPhoto  # noqa: E402
from app.services.image_search import caption_credit  # noqa: E402


def _parse_ig_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


class Publisher:
    def __init__(self, settings: Settings | None = None, meta: MetaService | None = None):
        self.settings = settings or get_settings()
        self._meta = meta

    @property
    def meta(self) -> MetaService:
        if self._meta is None:
            self._meta = MetaService(self.settings)
        return self._meta

    # ------------------------------------------------------------ helpers
    def image_url(self, artwork) -> str:
        base = self.settings.public_base_url or ""
        return f"{base}/media/{artwork.public_token}.jpg"

    def config_issue(self) -> str | None:
        s = self.settings
        if not s.meta_configured:
            return "Meta não configurada (META_IG_USER_ID e META_ACCESS_TOKEN)"
        if not s.public_base_url or not s.public_base_url.startswith("https://"):
            return "PUBLIC_BASE_URL precisa ser a URL pública HTTPS deste serviço"
        return None

    def preflight(self, db, post: Post) -> list[str]:
        s = self.settings
        problems = []
        art = post.artwork
        if art is None or not (art.jpg_bytes or (art.jpg_path and __import__("os").path.exists(art.jpg_path))):
            problems.append("imagem ausente")
        cap = post.caption
        if cap is None or not (cap.text or "").strip():
            problems.append("legenda ausente")
        else:
            chk = validate_caption(cap.text, allow_emojis=s.caption_allow_emojis, max_hashtags=s.caption_max_hashtags,
                                   min_chars=s.caption_min_chars, max_chars=s.caption_max_chars,
                                   verified_opportunity=bool(post.verified_opportunity_id))
            if not cap.is_valid or chk.needs_review or chk.text != cap.text:
                problems.append("legenda inválida: " + "; ".join(chk.issues or ["revalidação falhou"]))
        if s.require_manual_approval and not post.approved_by:
            problems.append("aprovação manual pendente")
        if post.photo_id:
            photo = db.get(StockPhoto, post.photo_id)
            if photo is None:
                problems.append("foto ausente")
            elif cap is not None and caption_credit(photo) not in (cap.text or ""):
                problems.append("crédito da foto ausente na legenda")
        if post.expires_at and post.expires_at <= utcnow():
            problems.append("assunto do momento expirado")
        if post.content_kind == "opportunity":
            opp = db.get(VerifiedOpportunity, post.verified_opportunity_id) if post.verified_opportunity_id else None
            if opp is None or not opp.active or (opp.expires_at and opp.expires_at <= utcnow()):
                problems.append("oportunidade não verificada, desativada ou expirada")
        return problems

    def rate_limit_wait(self, db, now: datetime, simulated: bool) -> datetime | None:
        s = self.settings
        states = [PostStatus.SIMULATED.value] if simulated else [
            PostStatus.PUBLISHED.value, PostStatus.PUBLISHING.value, PostStatus.PROCESSING.value,
            PostStatus.VERIFYING.value]
        stamp = func.coalesce(Post.published_at, Post.locked_at)
        recent = [t for (t,) in db.execute(select(stamp).where(Post.status.in_(states), stamp >= now - timedelta(hours=24)))
                  if t is not None]
        waits = []
        if len(recent) >= s.max_posts_per_day:
            waits.append(sorted(recent)[len(recent) - s.max_posts_per_day] + timedelta(hours=24))
        if recent and s.min_minutes_between_posts:
            last = max(recent)
            if now < last + timedelta(minutes=s.min_minutes_between_posts):
                waits.append(last + timedelta(minutes=s.min_minutes_between_posts))
        return max(waits) if waits else None

    # --------------------------------------------------------------- ciclo
    def run_cycle(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with session_scope() as db:
            if is_paused(db):
                return {"paused": True}
        if not acquire_lock(PUBLISH_LOCK, 600):
            return {"busy": True}
        try:
            summary: dict[str, Any] = {"recovered": self.recover_stale(now), "results": []}
            with session_scope() as db:
                inflight = db.scalars(select(Post.id).where(
                    Post.status.in_([PostStatus.PROCESSING.value, PostStatus.VERIFYING.value]))).all()
            for pid in inflight:
                summary["results"].append(self._continue(pid, now))
            with session_scope() as db:
                due = db.scalars(select(Post.id).where(
                    Post.status == PostStatus.SCHEDULED.value, Post.scheduled_at <= now,
                    or_(Post.next_attempt_at.is_(None), Post.next_attempt_at <= now)
                ).order_by(Post.scheduled_at).limit(10)).all()
            for pid in due:
                res = self._publish(pid, now)
                summary["results"].append(res)
                if res["outcome"] in ("published", "simulated", "processing", "verifying", "rate_limited"):
                    break  # no máximo uma publicação por ciclo
            return summary
        finally:
            release_lock(PUBLISH_LOCK)

    def publish_now(self, post_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Publicação imediata solicitada pelo administrador (passa pelas mesmas proteções)."""
        now = now or utcnow()
        with session_scope() as db:
            if is_paused(db):
                return {"post_id": post_id, "outcome": "paused", "message": "automação pausada"}
            post = db.get(Post, post_id)
            if post is None:
                return {"post_id": post_id, "outcome": "missing"}
            if post.status not in (PostStatus.APPROVED.value, PostStatus.SCHEDULED.value):
                return {"post_id": post_id, "outcome": "skipped",
                        "message": f"somente publicações aprovadas podem ser publicadas (estado: {post.status})"}
            post.scheduled_at = now
            post.next_attempt_at = None
            if post.status != PostStatus.SCHEDULED.value:
                set_status(db, post, PostStatus.SCHEDULED, "publicação imediata solicitada")
        if not acquire_lock(PUBLISH_LOCK, 600):
            raise PublisherBusy("outra publicação está em andamento; tente novamente em instantes")
        try:
            return self._publish(post_id, now)
        finally:
            release_lock(PUBLISH_LOCK)

    def recover_stale(self, now: datetime) -> int:
        n = 0
        with session_scope() as db:
            stale = db.scalars(select(Post).where(Post.status == PostStatus.PUBLISHING.value,
                                                  Post.locked_at < now - timedelta(minutes=STALE_LOCK_MINUTES))).all()
            for p in stale:
                if p.ig_container_id:
                    set_status(db, p, PostStatus.VERIFYING, "processo interrompido após criar contêiner; verificando")
                else:
                    set_status(db, p, PostStatus.SCHEDULED, "processo interrompido antes de criar contêiner; reagendada")
                p.lock_token = None
                n += 1
        return n

    # ---------------------------------------------------------- publicação
    def _publish(self, post_id: str, now: datetime) -> dict[str, Any]:
        s = self.settings
        with session_scope() as db:
            post = db.get(Post, post_id)
            if post is None:
                return {"post_id": post_id, "outcome": "missing"}
            if post.status != PostStatus.SCHEDULED.value:
                return {"post_id": post_id, "outcome": "skipped", "message": f"estado {post.status}"}
            if post.trend_term:
                from app.services.trend_content import trend_recheck

                why = trend_recheck(db, post, s)
                if why:
                    set_status(db, post, PostStatus.CANCELLED, f"assunto do momento cancelado antes de publicar: {why}")
                    return {"post_id": post_id, "outcome": "cancelled", "message": why}
            problems = self.preflight(db, post)
            if problems:
                post.review_notes = list(post.review_notes or []) + problems
                set_status(db, post, PostStatus.NEEDS_REVIEW, "bloqueada antes de publicar: " + "; ".join(problems))
                return {"post_id": post_id, "outcome": "blocked", "message": "; ".join(problems)}
            dup = find_duplicate(db, post)
            if dup is not None:
                set_status(db, post, PostStatus.DUPLICATE_BLOCKED, f"conteúdo idêntico ao de {dup.id}")
                return {"post_id": post_id, "outcome": "duplicate", "message": dup.id}
            wait = self.rate_limit_wait(db, now, simulated=s.dry_run)
            if wait:
                post.next_attempt_at = wait
                return {"post_id": post_id, "outcome": "rate_limited", "next_attempt_at": wait.isoformat() + "Z"}
            if not s.dry_run:
                issue = self.config_issue()
                if issue:
                    post.last_error = issue
                    post.next_attempt_at = now + timedelta(hours=1)
                    return {"post_id": post_id, "outcome": "not_configured", "message": issue}
            token = uuid.uuid4().hex
            res = db.execute(update(Post).where(Post.id == post_id, Post.status == PostStatus.SCHEDULED.value)
                             .values(status=PostStatus.PUBLISHING.value, lock_token=token, locked_at=now,
                                     dry_run=s.dry_run))
            if res.rowcount != 1:
                return {"post_id": post_id, "outcome": "skipped", "message": "reservada por outro processo"}
            db.add(PostEvent(post_id=post_id, from_status=PostStatus.SCHEDULED.value,
                             to_status=PostStatus.PUBLISHING.value,
                             message="reservada para publicação" + (" (simulação)" if s.dry_run else "")))
        if s.dry_run:
            return self._simulate(post_id, now)
        return self._publish_real(post_id, now)

    def _simulate(self, post_id: str, now: datetime) -> dict[str, Any]:
        with session_scope() as db:
            post = db.get(Post, post_id)
            url = self.image_url(post.artwork)
            post.published_at = now
            post.lock_token = None
            set_status(db, post, PostStatus.SIMULATED,
                       f"SIMULAÇÃO: nada foi enviado ao Instagram. Imagem que seria usada: {url}")
        return {"post_id": post_id, "outcome": "simulated", "status": PostStatus.SIMULATED.value}

    def _publish_real(self, post_id: str, now: datetime) -> dict[str, Any]:
        with session_scope() as db:
            post = db.get(Post, post_id)
            image_url = self.image_url(post.artwork)
            caption, alt = post.caption.text, post.alt_text
        try:
            lim = self.meta.publishing_limit()
            if lim.get("quota_total") and (lim.get("quota_usage") or 0) >= lim["quota_total"]:
                with session_scope() as db:
                    post = db.get(Post, post_id)
                    post.next_attempt_at = now + timedelta(hours=1)
                    set_status(db, post, PostStatus.SCHEDULED, "limite de publicação da Meta atingido; nova tentativa em 1 h")
                return {"post_id": post_id, "outcome": "rate_limited", "message": "limite da Meta"}
        except MetaError as e:
            logger.warning("não foi possível consultar content_publishing_limit: %s", e)
        try:
            cid = self.meta.create_image_container(image_url, caption, alt)
        except MetaError as e:
            return self._fail_attempt(post_id, now, e)
        with session_scope() as db:
            post = db.get(Post, post_id)
            post.ig_container_id = cid
            post.container_checks = 0
            set_status(db, post, PostStatus.PROCESSING, f"contêiner {cid} criado")
        return self._continue(post_id, now)

    def _fail_attempt(self, post_id: str, now: datetime, err: Exception) -> dict[str, Any]:
        s = self.settings
        retryable = getattr(err, "retryable", False)
        with session_scope() as db:
            post = db.get(Post, post_id)
            post.attempts = (post.attempts or 0) + 1
            post.last_error = str(err)[:2000]
            post.ig_container_id = None
            post.lock_token = None
            if retryable and post.attempts < s.publish_max_attempts:
                post.next_attempt_at = now + timedelta(minutes=5 * 2 ** (post.attempts - 1))
                set_status(db, post, PostStatus.SCHEDULED,
                           f"tentativa {post.attempts} falhou ({err}); nova tentativa às {post.next_attempt_at.isoformat()}Z")
                return {"post_id": post_id, "outcome": "retry", "message": str(err)}
            set_status(db, post, PostStatus.FAILED, f"falha definitiva após {post.attempts} tentativa(s): {err}")
            return {"post_id": post_id, "outcome": "failed", "message": str(err)}

    def _continue(self, post_id: str, now: datetime) -> dict[str, Any]:
        s = self.settings
        with session_scope() as db:
            post = db.get(Post, post_id)
            status, cid, locked = post.status, post.ig_container_id, post.locked_at
        if status == PostStatus.VERIFYING.value:
            return self._verify(post_id, now)
        if status != PostStatus.PROCESSING.value or not cid:
            return {"post_id": post_id, "outcome": "skipped"}
        try:
            st = self.meta.wait_container(cid, max_checks=s.container_max_checks,
                                          interval=s.container_check_interval_seconds)
        except MetaError as e:
            if e.retryable:
                with session_scope() as db:
                    db.get(Post, post_id).last_error = str(e)[:2000]
                return {"post_id": post_id, "outcome": "processing", "message": str(e)}
            return self._fail_attempt(post_id, now, e)
        with session_scope() as db:
            db.get(Post, post_id).container_checks += 1
        if st.status_code == "FINISHED":
            return self._publish_container(post_id, cid, now)
        if st.status_code == "PUBLISHED":
            with session_scope() as db:
                set_status(db, db.get(Post, post_id), PostStatus.VERIFYING, "contêiner já publicado; localizando mídia")
            return self._verify(post_id, now)
        if st.status_code in ("ERROR", "EXPIRED"):
            return self._fail_attempt(post_id, now, MetaError(
                f"contêiner {st.status_code}: {st.raw.get('status', '')}", retryable=True))
        if locked and now - locked > timedelta(hours=23):
            return self._fail_attempt(post_id, now, MetaError("contêiner não concluiu em 23 h", retryable=True))
        return {"post_id": post_id, "outcome": "processing", "message": st.status_code}

    def _publish_container(self, post_id: str, cid: str, now: datetime) -> dict[str, Any]:
        s = self.settings
        with session_scope() as db:
            db.get(Post, post_id).publish_attempted_at = now
        try:
            media_id = self.meta.publish_container(cid)
        except MetaError as e:
            if e.ambiguous:
                with session_scope() as db:
                    post = db.get(Post, post_id)
                    post.last_error = str(e)[:2000]
                    set_status(db, post, PostStatus.VERIFYING,
                               f"resultado incerto ao publicar ({e}); será verificado antes de qualquer nova tentativa")
                return {"post_id": post_id, "outcome": "verifying", "message": str(e)}
            if e.retryable:
                with session_scope() as db:
                    post = db.get(Post, post_id)
                    post.attempts = (post.attempts or 0) + 1
                    post.last_error = str(e)[:2000]
                    if post.attempts >= s.publish_max_attempts:
                        set_status(db, post, PostStatus.FAILED, f"falha ao publicar: {e}")
                        return {"post_id": post_id, "outcome": "failed", "message": str(e)}
                return {"post_id": post_id, "outcome": "processing", "message": str(e)}
            return self._fail_attempt(post_id, now, e)
        return self._mark_published(post_id, media_id, now)

    def _mark_published(self, post_id: str, media_id: str, now: datetime) -> dict[str, Any]:
        permalink = None
        try:
            permalink = self.meta.get_media(media_id).get("permalink")
        except MetaError as e:
            logger.warning("publicado, mas não foi possível obter o permalink: %s", e)
        with session_scope() as db:
            post = db.get(Post, post_id)
            post.ig_media_id = media_id
            post.permalink = permalink
            post.published_at = post.published_at or now
            post.lock_token = None
            post.last_error = None
            set_status(db, post, PostStatus.PUBLISHED, f"publicada no Instagram (mídia {media_id})")
        return {"post_id": post_id, "outcome": "published", "ig_media_id": media_id, "permalink": permalink}

    def _verify(self, post_id: str, now: datetime) -> dict[str, Any]:
        with session_scope() as db:
            post = db.get(Post, post_id)
            cid, caption = post.ig_container_id, post.caption.text if post.caption else ""
            since = (post.publish_attempted_at or post.locked_at or now) - timedelta(minutes=10)
            known = {m for (m,) in db.execute(select(Post.ig_media_id).where(Post.ig_media_id.is_not(None)))}
        try:
            target = caption.strip()[:200]
            for m in self.meta.recent_media(25):
                ts = _parse_ig_ts(m.get("timestamp"))
                if m.get("id") in known or (ts and ts < since):
                    continue
                if (m.get("caption") or "").strip()[:200] == target:
                    return self._mark_published(post_id, str(m["id"]), now)
            st = self.meta.container_status(cid) if cid else None
        except MetaError as e:
            if e.retryable:
                return {"post_id": post_id, "outcome": "verifying", "message": str(e)}
            with session_scope() as db:
                set_status(db, db.get(Post, post_id), PostStatus.NEEDS_REVIEW, f"verificação falhou: {e}")
            return {"post_id": post_id, "outcome": "blocked", "message": str(e)}
        code = st.status_code if st else "NONE"
        if code == "FINISHED":
            with session_scope() as db:
                set_status(db, db.get(Post, post_id), PostStatus.PROCESSING,
                           "verificado: não publicada; contêiner ainda válido")
            return self._publish_container(post_id, cid, now)
        if code == "PUBLISHED":
            with session_scope() as db:
                post = db.get(Post, post_id)
                post.container_checks += 1
                if post.container_checks > 10:
                    set_status(db, post, PostStatus.NEEDS_REVIEW,
                               "a Meta indica contêiner publicado, mas a mídia não foi localizada; confira o perfil")
                    return {"post_id": post_id, "outcome": "blocked"}
            return {"post_id": post_id, "outcome": "verifying", "message": "aguardando mídia aparecer na conta"}
        return self._fail_attempt(post_id, now, MetaError(f"verificado: não publicada (contêiner {code})", retryable=True))