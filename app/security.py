"""Autenticação da API administrativa e limite de requisições."""
from __future__ import annotations

import hmac
import threading
import time
from collections import deque

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import get_settings


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


class RateLimiter:
    """Janela deslizante em memória (por instância). Suficiente para um único serviço."""

    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, window - (now - q[0])
            q.append(now)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def _enforce(request: Request, bucket: str, limit: int) -> None:
    ok, retry = limiter.check(f"{bucket}:{_client_ip(request)}", limit)
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "muitas requisições; tente novamente em instantes",
                            headers={"Retry-After": str(int(retry) + 1)})


def rate_limit(request: Request) -> None:
    _enforce(request, "geral", get_settings().rate_limit_per_minute)


def rate_limit_generation(request: Request) -> None:
    """Limite mais rígido para endpoints que geram imagens, chamam a Groq ou publicam."""
    _enforce(request, "geracao", get_settings().rate_limit_generation_per_minute)


def require_admin(request: Request, authorization: str | None = Header(default=None),
                  x_admin_token: str | None = Header(default=None)) -> str:
    settings = get_settings()
    expected = settings.admin_api_token.get_secret_value() if settings.admin_api_token else ""
    if not expected or len(expected) < 24:
        # sem token configurado, nenhuma operação administrativa é liberada
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "ADMIN_API_TOKEN não configurado (mínimo de 24 caracteres)")
    provided = x_admin_token or ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        # conta tentativas inválidas para dificultar força bruta
        _enforce(request, "auth_falha", 10)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token administrativo inválido ou ausente",
                            headers={"WWW-Authenticate": "Bearer"})
    rate_limit(request)
    return "admin"


AdminDep = Depends(require_admin)
GenerationLimit = Depends(rate_limit_generation)