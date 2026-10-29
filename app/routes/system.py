"""Agendador, pausa, integrações e configuração."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas import PauseRequest
from app.security import AdminDep, GenerationLimit
from app.services import scheduler
from app.services.state import set_paused

router = APIRouter(prefix="/api", dependencies=[AdminDep], tags=["sistema"])


@router.get("/scheduler", summary="Estado do agendador e últimas execuções")
def scheduler_status():
    return scheduler.scheduler_info()


@router.post("/scheduler/pause", summary="Pausar publicação automática")
def pause(req: PauseRequest, db: Session = Depends(get_db)):
    v = set_paused(db, True, reason=req.reason)
    db.commit()
    return v


@router.post("/scheduler/resume", summary="Retomar publicação automática")
def resume(db: Session = Depends(get_db)):
    v = set_paused(db, False)
    db.commit()
    return v


@router.post("/scheduler/tick", summary="Executar tarefas vencidas (para cron externo)", dependencies=[GenerationLimit])
def tick():
    return scheduler.tick()


@router.post("/scheduler/run/{job}", summary="Executar uma tarefa específica agora", dependencies=[GenerationLimit])
def run(job: str):
    if job not in scheduler.JOBS:
        raise HTTPException(404, f"tarefa desconhecida; use uma de {sorted(scheduler.JOBS)}")
    return scheduler.run_job(job)


@router.get("/config", summary="Configuração efetiva (sem segredos)")
def config():
    s = get_settings()
    return {
        "app_env": s.app_env, "timezone": s.timezone, "database": "sqlite" if s.is_sqlite else "postgresql",
        "dry_run": s.dry_run, "require_manual_approval": s.require_manual_approval,
        "max_posts_per_day": s.max_posts_per_day, "min_minutes_between_posts": s.min_minutes_between_posts,
        "auto_schedule": s.auto_schedule, "posting_slots": s.posting_slots, "posting_weekdays": s.posting_weekdays,
        "groq_model": s.groq_model, "groq_configured": s.groq_configured, "caption_allow_emojis": s.caption_allow_emojis,
        "caption_max_hashtags": s.caption_max_hashtags, "meta_login_type": s.meta_login_type,
        "meta_graph_api_version": s.meta_graph_api_version, "meta_configured": s.meta_configured,
        "public_base_url": s.public_base_url, "scheduler_enabled": s.scheduler_enabled,
        "analysis_target_age_hours": s.analysis_target_age_hours,
        "analysis_min_samples_per_group": s.analysis_min_samples_per_group,
        "autonomous": s.autonomous, "posting_strategy": s.posting_strategy,
        "posting_candidate_slots": s.posting_candidate_slots, "timing_min_samples": s.timing_min_samples,
        "timing_explore_rate": s.timing_explore_rate, "trend_auto_enabled": s.trend_auto_enabled,
        "alert_webhook_configured": bool(s.alert_webhook_url and s.alert_webhook_url.get_secret_value()),
        "auto_pause_failures": s.auto_pause_failures, "queue_min_days": s.queue_min_days,
    }


@router.get("/timing", summary="Horários de publicação: o que o sistema aprendeu e o que vem a seguir")
def timing_view(db: Session = Depends(get_db)):
    from app.services.timing import timing_report

    return timing_report(db)


@router.get("/alerts", summary="Últimos alertas da vigilância automática")
def alerts_view(db: Session = Depends(get_db)):
    from app.services.state import get_state

    s = get_settings()
    return {"webhook_configured": bool(s.alert_webhook_url and s.alert_webhook_url.get_secret_value()),
            "alerts": get_state(db, "alerts_log", []) or []}


@router.post("/alerts/test", summary="Enviar um alerta de teste para o webhook", dependencies=[GenerationLimit])
def alerts_test():
    from app.services.alerts import send

    return {"result": send("teste", "Se você recebeu esta mensagem, os alertas do Doova estão funcionando.")}


@router.get("/integrations/meta/check", summary="Verificar conexão com a conta do Instagram (somente leitura)")
def meta_check():
    from app.services.meta_service import MetaError, MetaService
    from app.services.token_store import token_status

    s = get_settings()
    if not s.meta_configured:
        raise HTTPException(409, "META_IG_USER_ID/META_ACCESS_TOKEN não configurados")
    m = MetaService(s)
    try:
        return {"account": m.account_info(), "publishing_limit": m.publishing_limit(), "token": token_status(s)}
    except MetaError as e:
        raise HTTPException(502, str(e))


@router.post("/integrations/meta/refresh-token", summary="Renovar o token de longa duração da Meta")
def meta_refresh():
    from app.services.meta_service import MetaError
    from app.services.token_store import refresh_token

    try:
        return refresh_token()
    except MetaError as e:
        raise HTTPException(502, str(e))


@router.get("/integrations/groq/models", summary="Listar modelos disponíveis na conta Groq")
def groq_models():
    from app.services.groq_service import CaptionGenerationError, GroqCaptionService

    s = get_settings()
    try:
        models = GroqCaptionService(s).list_models()
    except CaptionGenerationError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"falha ao consultar a Groq: {type(e).__name__}")
    return {"configured_model": s.groq_model, "configured_model_available": s.groq_model in models,
            "models": models}