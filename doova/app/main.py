"""Aplicação FastAPI do sistema de marketing do Doova.

Documentação interativa: /docs (Swagger) e /redoc. Endpoints administrativos exigem
``Authorization: Bearer <ADMIN_API_TOKEN>`` (ou o cabeçalho ``X-Admin-Token``).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.database import create_all, init_engine
from app.logging_config import setup_logging
from app.routes import content, insights, public, system

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level, secrets_provider=lambda: get_settings().secret_values())
    init_engine(s.database_url)
    create_all()
    s.generated_dir.mkdir(parents=True, exist_ok=True)
    if s.app_env == "production":
        if not s.admin_api_token:
            logger.error("ADMIN_API_TOKEN ausente: a API administrativa ficará bloqueada")
        if s.is_sqlite:
            logger.warning("Produção com SQLite: use PostgreSQL ou um disco persistente para não perder dados")
    logger.info("Doova marketing %s iniciado (dry_run=%s, aprovação manual=%s, horário=%s)", __version__, s.dry_run,
                s.require_manual_approval, s.posting_strategy)
    if s.autonomous and not (s.alert_webhook_url and s.alert_webhook_url.get_secret_value()):
        logger.warning("Modo automático sem ALERT_WEBHOOK_URL: avisos de falha ficarão só em GET /api/alerts e nos logs")
    started = False
    if s.scheduler_enabled and s.app_env != "test":
        from app.services.scheduler import start_scheduler

        start_scheduler(s)
        started = True
    try:
        yield
    finally:
        if started:
            from app.services.scheduler import stop_scheduler

            stop_scheduler()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Doova — Automação de Marketing no Instagram",
        version=__version__,
        description=("API administrativa: gere artes, legendas, publicações, experimentos e relatórios. "
                     "Clique em **Authorize** e informe o ADMIN_API_TOKEN."),
        lifespan=lifespan,
        docs_url="/docs" if s.docs_enabled else None,
        redoc_url="/redoc" if s.docs_enabled else None,
        openapi_url="/openapi.json" if s.docs_enabled else None,
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - rede de segurança
        logger.exception("erro não tratado em %s", request.url.path)
        return JSONResponse({"detail": "erro interno"}, status_code=500)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    app.include_router(public.router)
    app.include_router(content.router)
    app.include_router(insights.router)
    app.include_router(system.router)
    from app.routes import photos

    app.include_router(photos.router)
    from app.routes import videos

    app.include_router(videos.router)
    return app


app = create_app()
