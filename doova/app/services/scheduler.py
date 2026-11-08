"""Tarefas agendadas.

Duas formas equivalentes de execução:
* **APScheduler** dentro do processo web (quando ``SCHEDULER_ENABLED=true``), ideal num
  serviço sempre ligado (Render Starter ou superior);
* **tick externo** (``POST /api/scheduler/tick`` ou ``python -m app.cli tick``), para
  plataformas que hibernam — um cron chama o tick periodicamente.

Todas as tarefas usam travas no banco (``job_locks``) e são idempotentes, então reinícios,
deploys com duas instâncias ou tick e agendador simultâneos não duplicam trabalho.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import session_scope, utcnow
from app.models import Post, PostStatus
from app.services.content_service import set_status
from app.services.state import acquire_lock, get_state, is_paused, release_lock, set_state

logger = logging.getLogger(__name__)
_scheduler = None


def _guard(name: str, ttl: int, fn: Callable[[], Any]) -> Any:
    if not acquire_lock(f"job:{name}", ttl):
        return {"skipped": "em execução em outro processo"}
    try:
        res = fn()
        with session_scope() as db:
            set_state(db, f"last_run:{name}", {"at": utcnow().isoformat() + "Z", "result": _short(res)})
        return res
    except Exception as e:
        logger.exception("tarefa %s falhou", name)
        with session_scope() as db:
            set_state(db, f"last_run:{name}", {"at": utcnow().isoformat() + "Z", "error": str(e)[:500]})
        return {"error": str(e)[:500]}
    finally:
        release_lock(f"job:{name}")


def _short(res: Any) -> Any:
    s = str(res)
    return res if len(s) < 1500 else s[:1500]


# ------------------------------------------------------------- tarefas
def job_publish() -> Any:
    from app.services.publisher import Publisher

    return Publisher().run_cycle()


def job_maintenance() -> Any:
    from app.services.content_service import ContentService

    with session_scope() as db:
        if is_paused(db):
            return {"paused": True}
        cs = ContentService()
        from app.services.trend_content import expire_trend_posts

        return {"trend_posts_expired": expire_trend_posts(db), "captions_retried_ok": cs.retry_failed_captions(db),
                "captions_filled": cs.fill_missing_captions(db), "auto_approved": cs.approve_pending(db),
                "auto_scheduled": cs.auto_schedule(db)}


def job_metrics() -> Any:
    s = get_settings()
    if not s.meta_configured:
        return {"skipped": "Meta não configurada"}
    from app.services.analytics_service import collect_metrics

    with session_scope() as db:
        return collect_metrics(db, settings=s)


def job_video_publish() -> Any:
    """Publica Reels renderizados nos destinos configurados.

    Espelha o job_publish de imagens: job_trends_auto gera o asset e, no modo Android,
    o worker externo renderiza o arquivo; este job publica somente Reels prontos e
    respeita o post aprovado e seu horário agendado.
    """
    s = get_settings()
    if not s.video_auto_publish:
        return {"skipped": "VIDEO_AUTO_PUBLISH=false"}
    if not s.video_publish_targets:
        return {"skipped": "nenhuma plataforma de vídeo configurada"}
    from app.services.video_service import (
        VideoPublishError,
        publish_video_to_targets,
        sync_linked_post_after_reel,
        unpublished_rendered_videos,
    )

    with session_scope() as db:
        if is_paused(db):
            return {"paused": True}
        targets = s.video_publish_targets
        pending = unpublished_rendered_videos(db, targets=targets)
        published, errors, deferred, processing = [], [], [], []
        for asset in pending:
            linked_post = db.scalar(select(Post).where(Post.video_asset_id == asset.id))
            if linked_post is not None:
                now = utcnow()
                if (linked_post.status != PostStatus.SCHEDULED.value or not linked_post.scheduled_at
                        or linked_post.scheduled_at > now):
                    deferred.append(asset.id)
                    continue
                if linked_post.expires_at and linked_post.expires_at <= now:
                    set_status(db, linked_post, PostStatus.CANCELLED, "assunto do momento expirou antes da publicação")
                    db.commit()
                    deferred.append(asset.id)
                    continue
                if linked_post.caption is None or not linked_post.caption.is_valid:
                    errors.append({"video_id": asset.id, "error": "publicação vinculada sem legenda válida"})
                    continue
            try:
                result = publish_video_to_targets(
                    db, asset, s, caption_override=linked_post.caption.text if linked_post else None,
                    platforms=targets,
                )
            except VideoPublishError as e:
                errors.append({"video_id": asset.id, "error": str(e)[:300]})
                continue
            sync_linked_post_after_reel(db, asset, result)
            result_status = result.get("status")
            if result_status == "published":
                published.append(asset.id)
            elif result_status == "processing":
                processing.append({"video_id": asset.id, "platforms": result["platforms"]})
            elif result_status == "partial":
                errors.append({"video_id": asset.id, "platforms": result["platforms"]})
            elif result_status == "failed":
                errors.append({"video_id": asset.id, "platforms": result["platforms"]})
        return {"published": published, "processing": processing, "errors": errors,
                "deferred": deferred, "eligible": len(pending), "targets": targets}


def job_video_metrics() -> Any:
    s = get_settings()
    if not s.meta_configured:
        return {"skipped": "Meta não configurada"}
    from app.services.video_analytics_service import collect_video_metrics

    with session_scope() as db:
        return collect_video_metrics(db, settings=s)


def job_token_refresh() -> Any:
    s = get_settings()
    if not s.meta_configured:
        return {"skipped": "Meta não configurada"}
    from app.services.token_store import refresh_token

    return refresh_token(s)


def job_backup() -> Any:
    from app.services.backup import run_backup

    return run_backup()


def job_trends_auto() -> Any:
    s = get_settings()
    if not s.trend_auto_enabled:
        return {"skipped": "TREND_AUTO_ENABLED=false"}
    from app.services.trend_content import run_trend_auto
    from app.services.trend_content import run_catalog_reel_auto

    with session_scope() as db:
        if is_paused(db):
            return {"paused": True}
        res = run_trend_auto(db, s)
        catalog = run_catalog_reel_auto(db, s)
        return {
            "created": [*res.get("created", []), *catalog.get("created", [])],
            "trend_created": len(res.get("created", [])),
            "catalog_created": len(catalog.get("created", [])),
            "evaluated": len(res.get("report", [])),
            "skipped": res.get("skipped"),
            "catalog_skipped": catalog.get("skipped"),
            "catalog_report": catalog.get("report", []),
        }


def job_watchdog() -> Any:
    from app.services.alerts import watchdog

    with session_scope() as db:
        return watchdog(db)


JOBS: dict[str, tuple[Callable[[], Any], int]] = {
    "publish": (job_publish, 900),
    "maintenance": (job_maintenance, 600),
    "metrics": (job_metrics, 1800),
    "token_refresh": (job_token_refresh, 600),
    "backup": (job_backup, 600),
    "trends_auto": (job_trends_auto, 1200),
    "watchdog": (job_watchdog, 600),
    "video_publish": (job_video_publish, 900),
    "video_metrics": (job_video_metrics, 1800),
}


def run_job(name: str) -> Any:
    fn, ttl = JOBS[name]
    return _guard(name, ttl, fn)


def intervals(settings: Settings) -> dict[str, timedelta]:
    return {
        "publish": timedelta(seconds=settings.publisher_interval_seconds),
        "maintenance": timedelta(minutes=5),
        "metrics": timedelta(hours=settings.metrics_interval_hours),
        "token_refresh": timedelta(days=7),
        "backup": timedelta(days=1),
        "trends_auto": timedelta(hours=settings.trend_auto_interval_hours),
        "watchdog": timedelta(minutes=15),
        "video_publish": timedelta(seconds=settings.publisher_interval_seconds),
        "video_metrics": timedelta(hours=settings.metrics_interval_hours),
    }


def tick(settings: Settings | None = None, force: list[str] | None = None) -> dict[str, Any]:
    """Executa as tarefas vencidas. Seguro para chamar a qualquer momento (idempotente)."""
    settings = settings or get_settings()
    now = utcnow()
    results = {}
    for name, every in intervals(settings).items():
        with session_scope() as db:
            last = get_state(db, f"last_run:{name}")
        due = True
        if last and last.get("at") and name not in (force or []):
            from datetime import datetime

            last_at = datetime.fromisoformat(last["at"].rstrip("Z"))
            due = now - last_at >= every - timedelta(seconds=5)
        if name == "publish":
            due = True  # o próprio publicador decide se há algo vencido
        results[name] = run_job(name) if due else {"not_due": True}
    return results


# ---------------------------------------------------------- APScheduler
def start_scheduler(settings: Settings | None = None):
    global _scheduler
    settings = settings or get_settings()
    if _scheduler is not None:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    sch = BackgroundScheduler(timezone=settings.timezone,
                              job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    iv = intervals(settings)
    sch.add_job(run_job, "interval", args=["publish"], seconds=int(iv["publish"].total_seconds()), id="publish")
    sch.add_job(run_job, "interval", args=["maintenance"], minutes=5, id="maintenance")
    sch.add_job(run_job, "interval", args=["metrics"], hours=settings.metrics_interval_hours, id="metrics")
    sch.add_job(run_job, "cron", args=["token_refresh"], day_of_week="mon", hour=4, minute=10, id="token_refresh")
    sch.add_job(run_job, "cron", args=["backup"], hour=3, minute=30, id="backup")
    sch.add_job(run_job, "interval", args=["trends_auto"], hours=settings.trend_auto_interval_hours, id="trends_auto")
    sch.add_job(run_job, "interval", args=["watchdog"], minutes=15, id="watchdog")
    sch.add_job(run_job, "interval", args=["video_publish"],
               seconds=int(iv["video_publish"].total_seconds()), id="video_publish")
    sch.add_job(run_job, "interval", args=["video_metrics"],
               hours=settings.metrics_interval_hours, id="video_metrics")
    sch.start()
    _scheduler = sch
    logger.info("agendador iniciado: %s", [j.id for j in sch.get_jobs()])
    return sch


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_info() -> dict[str, Any]:
    jobs = []
    if _scheduler is not None:
        for j in _scheduler.get_jobs():
            jobs.append({"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None})
    with session_scope() as db:
        last = {n: get_state(db, f"last_run:{n}") for n in JOBS}
        paused = is_paused(db)
        pause_info = get_state(db, "automation", {})
    return {"running_in_process": _scheduler is not None, "jobs": jobs, "last_runs": last, "paused": paused,
            "pause_info": pause_info}