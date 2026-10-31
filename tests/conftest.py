"""Infraestrutura de testes: banco SQLite temporário e simuladores da Groq e da Meta.

Nenhum teste acessa APIs reais. Variáveis sensíveis são sobrescritas para que um
arquivo .env local nunca seja usado durante os testes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

ADMIN_TOKEN = "teste-token-administrativo-0123456789"
CAPTION_OK = ("Encontrar quem faz um bom serviço perto de casa nem sempre é simples. O Doova nasce em Iporá "
              "para aproximar prestadores e clientes. Siga o perfil e acompanhe. #Iporá #Doova")
MANUAL_CAPTION = ("O Doova está chegando em Iporá para aproximar quem oferece serviços de quem precisa contratar. "
                  "Acompanhe o perfil.")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    values = {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'teste.db'}",
        "GENERATED_DIR": str(tmp_path / "generated"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "ADMIN_API_TOKEN": ADMIN_TOKEN,
        "GROQ_API_KEY": "gsk_testeFalsoNaoUsar1234567890",
        "GROQ_MODEL": "openai/gpt-oss-120b",
        "GROQ_MIN_INTERVAL_SECONDS": "0",
        "SCHEDULER_ENABLED": "false",
        "GOOGLE_TRENDS_RSS_ENABLED": "false",
        "DRY_RUN": "true",
        "REQUIRE_MANUAL_APPROVAL": "true",
        "AUTO_SCHEDULE": "false",
        "MAX_POSTS_PER_DAY": "2",
        "MIN_MINUTES_BETWEEN_POSTS": "180",
        "META_IG_USER_ID": "",
        "META_ACCESS_TOKEN": "",
        "META_APP_SECRET": "",
        "PUBLIC_BASE_URL": "",
        "TOKEN_ENCRYPTION_KEY": "",
        "CONTAINER_CHECK_INTERVAL_SECONDS": "0",
        "ANALYSIS_PERMUTATIONS": "2000",
        "DOOVA_FONT_DIR": "",
        "TREND_AUTO_ENABLED": "false",
        "PIXABAY_API_KEY": "",
        "IMAGE_SEARCH_ENABLED": "true",
        "CONTACT_EMAIL": "",
        "TREND_BLOCKLIST_EXTRA": "",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    from app.config import reset_settings_cache
    from app.database import create_all, init_engine
    from app.routes import deps
    from app.security import limiter

    reset_settings_cache()
    init_engine()
    create_all()
    limiter.reset()
    deps.set_factories()
    yield
    deps.set_factories()
    reset_settings_cache()


def reload_settings(monkeypatch, **env):
    from app.config import get_settings, reset_settings_cache

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return get_settings()


# ----------------------------------------------------------------- Groq
def groq_response(text: str, finish: str = "stop") -> dict:
    return {"id": "x", "object": "chat.completion", "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
            "usage": {"total_tokens": 100}}


class GroqStub:
    """Transporte HTTP falso: devolve as respostas na ordem; a última se repete."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, json=groq_response(item))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def make_groq(responses=None, settings=None):
    from app.services.groq_service import GroqCaptionService

    stub = GroqStub(responses or [CAPTION_OK])
    sleeps: list[float] = []
    svc = GroqCaptionService(settings, client=stub.client(), sleep=sleeps.append)
    svc.stub, svc.sleeps = stub, sleeps
    return svc


def make_content(responses=None, settings=None):
    from app.services.content_service import ContentService

    return ContentService(settings, caption_service=make_groq(responses, settings))


# ----------------------------------------------------------------- Meta
class FakeMeta:
    def __init__(self):
        from app.services.meta_service import ContainerStatus

        self.CS = ContainerStatus
        self.calls: list = []
        self.container_states = ["FINISHED"]
        self.publish_errors: list = []
        self.create_errors: list = []
        self.recent: list = []
        self.insights: dict = {}
        self.last_caption = None
        self.counter = 0

    def publishing_limit(self):
        self.calls.append("limit")
        return {"quota_usage": 0, "quota_total": 100}

    def create_image_container(self, url, caption, alt=None):
        self.calls.append(("create", url))
        if self.create_errors:
            raise self.create_errors.pop(0)
        self.last_caption = caption
        self.counter += 1
        return f"cont{self.counter}"

    def _state(self):
        return self.container_states.pop(0) if len(self.container_states) > 1 else self.container_states[0]

    def wait_container(self, cid, max_checks=None, interval=0):
        self.calls.append(("wait", cid))
        return self.CS(self._state(), {})

    def container_status(self, cid):
        self.calls.append(("status", cid))
        return self.CS(self._state(), {})

    def publish_container(self, cid):
        self.calls.append(("publish", cid))
        if self.publish_errors:
            raise self.publish_errors.pop(0)
        return f"media_{cid}"

    def get_media(self, mid):
        return {"id": mid, "permalink": f"https://www.instagram.com/p/{mid}/"}

    def recent_media(self, limit=25):
        self.calls.append("recent")
        return self.recent

    def media_insights(self, mid):
        self.calls.append(("insights", mid))
        return self.insights.get(mid, ({}, ["reach"]))


class ExplodingMeta:
    def __getattr__(self, name):
        raise AssertionError(f"a Meta não deveria ser chamada (método {name})")


def ig_timestamp(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


# ------------------------------------------------------------- helpers
def new_post(db, cs=None, template="autonomos", overrides=None, caption=MANUAL_CAPTION, **kw):
    from app.services.content_service import PostInput

    cs = cs or make_content()
    data = PostInput(template_key=template, overrides=overrides or {}, caption=caption,
                     generate_caption=caption is None, **kw)
    return cs.create_post(db, data)


def scheduled_post(db, cs=None, when=None, **kw):
    """Cria, aprova e coloca na fila uma publicação pronta para sair agora."""
    from app.database import utcnow
    from app.models import PostStatus

    cs = cs or make_content()
    p = new_post(db, cs, **kw)
    cs.approve(db, p, "teste")
    p.scheduled_at = when or utcnow()
    p.status = PostStatus.SCHEDULED.value
    db.commit()
    return p


@pytest.fixture
def db():
    from app.database import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}