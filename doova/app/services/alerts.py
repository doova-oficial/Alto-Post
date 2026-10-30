"""Vigilância do modo automático: avisa quando algo precisa de você e pausa se der errado.

Sem aprovação manual, ninguém olha cada publicação antes. Esta tarefa (a cada 15 min)
substitui esse olhar com regras objetivas:

- falhas definitivas de publicação: avisa cada uma; com AUTO_PAUSE_FAILURES falhas em
  24 h, pausa toda a publicação e avisa;
- publicações barradas pelas checagens automáticas (needs_review): avisa;
- fila acabando: avisa quando a fila cobre menos de QUEUE_MIN_DAYS dias;
- token da Meta perto de expirar sem renovação automática: avisa.

Os avisos vão para ALERT_WEBHOOK_URL (Discord, Slack, ntfy ou JSON genérico) e ficam
registrados em GET /api/alerts. Cada aviso igual é enviado no máximo uma vez a cada
ALERT_DEDUPE_HOURS.
"""
from __future__ import annotations

import hashlib
import logging
import unicodedata
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Post, PostEvent, PostStatus
from app.services.state import PAUSE_KEY, get_state, is_paused, set_paused, set_state

logger = logging.getLogger(__name__)
LOG_KEY = "alerts_log"


def _kind(url: str, kind: str) -> str:
    if kind != "auto":
        return kind
    u = url.lower()
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    if "hooks.slack.com" in u:
        return "slack"
    if "ntfy" in u:
        return "ntfy"
    return "json"


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def send(title: str, message: str, settings: Settings | None = None, client: httpx.Client | None = None) -> str:
    s = settings or get_settings()
    url = s.alert_webhook_url.get_secret_value() if s.alert_webhook_url else ""
    if not url:
        return "sem webhook configurado"
    kind = _kind(url, s.alert_webhook_kind)
    text = f"[Doova] {title}\n{message}"
    c = client or httpx.Client(timeout=10)
    try:
        if kind == "discord":
            r = c.post(url, json={"content": text[:1900]})
        elif kind == "slack":
            r = c.post(url, json={"text": text})
        elif kind == "ntfy":
            r = c.post(url, content=message.encode("utf-8"),
                       headers={"Title": _ascii(f"Doova: {title}")[:200], "Tags": "warning"})
        else:
            r = c.post(url, json={"source": "doova-marketing", "title": title, "message": message,
                                  "at": utcnow().isoformat() + "Z"})
        return "enviado" if r.status_code < 300 else f"erro HTTP {r.status_code}"
    except httpx.HTTPError as e:
        logger.warning("alerta não enviado: %s", type(e).__name__)
        return f"erro: {type(e).__name__}"
    finally:
        if client is None:
            c.close()


def notify(db: Session, key: str, title: str, message: str, settings: Settings | None = None,
           client: httpx.Client | None = None) -> bool:
    """Envia um aviso (uma vez por ALERT_DEDUPE_HOURS para a mesma chave) e registra no histórico."""
    s = settings or get_settings()
    now = utcnow()
    last = get_state(db, f"alert:{key}")
    if last and last.get("at"):
        if now - datetime.fromisoformat(last["at"].rstrip("Z")) < timedelta(hours=s.alert_dedupe_hours):
            return False
    result = send(title, message, s, client)
    logger.warning("ALERTA %s: %s (%s)", title, message[:300], result)
    set_state(db, f"alert:{key}", {"at": now.isoformat() + "Z"})
    log = list(get_state(db, LOG_KEY, []) or [])
    log.insert(0, {"at": now.isoformat() + "Z", "title": title, "message": message, "delivery": result})
    set_state(db, LOG_KEY, log[:50])
    db.commit()
    return True


def _ids_key(prefix: str, ids: list[str]) -> str:
    return prefix + ":" + hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:16]


def watchdog(db: Session, settings: Settings | None = None, now: datetime | None = None,
             client: httpx.Client | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    now = now or utcnow()
    out: dict[str, Any] = {"alerts": 0}
    if is_paused(db):
        return {"paused": True}

    def alert(key: str, title: str, msg: str) -> None:
        out["alerts"] += int(notify(db, key, title, msg, s, client))

    # 1. falhas definitivas (desde 24 h atrás ou desde a última retomada, o que for mais recente)
    since = now - timedelta(hours=24)
    pst = get_state(db, PAUSE_KEY, {}) or {}
    if pst.get("at"):
        since = max(since, datetime.fromisoformat(pst["at"].rstrip("Z")))
    failed = sorted({pid for (pid,) in db.execute(select(PostEvent.post_id).where(
        PostEvent.to_status == PostStatus.FAILED.value, PostEvent.at >= since)).all()})
    for pid in failed:
        post = db.get(Post, pid)
        alert(f"failed:{pid}", "publicação falhou",
              f"A publicação {pid} falhou de vez: {(post.last_error or '')[:300] if post else ''}")
    if s.auto_pause_failures and len(failed) >= s.auto_pause_failures:
        reason = f"{len(failed)} falhas definitivas de publicação desde {since.isoformat()}Z"
        set_paused(db, True, by="vigilância automática", reason=reason)
        db.commit()
        out["auto_paused"] = True
        alert(_ids_key("autopause", failed), "publicação pausada automaticamente",
              f"{reason}. Nada mais será publicado até você conferir e chamar POST /api/scheduler/resume.")
        return out

    # 2. barradas pelas checagens automáticas
    if s.autonomous:
        rev = db.scalars(select(Post).where(Post.status == PostStatus.NEEDS_REVIEW.value)
                         .order_by(Post.updated_at.desc()).limit(20)).all()
        if rev:
            lines = [f"- {p.id}: {'; '.join((p.review_notes or [])[-2:]) or 'ver legenda'}"[:240] for p in rev]
            alert(_ids_key("review", [p.id for p in rev]), f"{len(rev)} publicação(ões) barrada(s)",
                  "As checagens automáticas barraram e estas não serão publicadas sozinhas:\n" + "\n".join(lines)
                  + "\nVeja em GET /api/posts/pending (corrija e aprove, ou rejeite).")

    # 3. fila acabando
    if s.auto_schedule and s.queue_min_days:
        queued = db.scalar(select(func.count()).select_from(Post).where(
            Post.status.in_([PostStatus.APPROVED.value, PostStatus.SCHEDULED.value]))) or 0
        needed = s.queue_min_days * s.max_posts_per_day
        out["queue"] = queued
        if queued < needed:
            alert(f"queue:{(now - timedelta(hours=3)).date()}", "fila de publicações acabando",
                  f"Há {queued} publicação(ões) prontas; o ideal é ter pelo menos {needed} "
                  f"({s.queue_min_days} dia(s) x {s.max_posts_per_day} por dia). "
                  "Crie mais com POST /api/posts usando um theme_key da biblioteca, ou veja GET /api/suggestions.")

    # 4. token da Meta
    if s.meta_configured:
        try:
            from app.services.token_store import token_status

            ts = token_status(s)
            exp = ts.get("expires_at")
            if exp and not ts.get("auto_refresh"):
                left = datetime.fromisoformat(exp.rstrip("Z")) - now
                if left < timedelta(days=7):
                    alert(f"token:{exp}", "token da Meta perto de expirar",
                          f"O token expira em {max(left.days, 0)} dia(s) e a renovação automática está desligada "
                          "(defina TOKEN_ENCRYPTION_KEY). Sem token, nada é publicado.")
        except Exception as e:  # nunca derruba a vigilância
            logger.warning("não foi possível verificar o token: %s", e)
    return out