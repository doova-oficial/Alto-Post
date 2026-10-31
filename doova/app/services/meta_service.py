"""Integração oficial com a Instagram Graph API (Meta).

Documentação consultada (out/2026, Graph API v26.0):
* Publicação: https://developers.facebook.com/docs/instagram-platform/content-publishing/
  - POST /<IG_ID>/media (image_url, caption, alt_text) → id do contêiner
  - GET  /<IG_CONTAINER_ID>?fields=status_code  (EXPIRED, ERROR, FINISHED, IN_PROGRESS, PUBLISHED)
  - POST /<IG_ID>/media_publish (creation_id) → id da mídia
  - GET  /<IG_ID>/content_publishing_limit  (100 publicações por API em 24 h móveis)
  - Somente JPEG é aceito para imagens.
* Insights: https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights/
  - Posts de feed: reach, views, likes, comments, shares, saved, total_interactions,
    profile_visits, profile_activity, follows. ``impressions`` foi descontinuada para mídias
    criadas após 02/07/2024. Métricas indisponíveis retornam conjunto vazio (não zero) e
    podem atrasar até 48 h.
* Login: Instagram API with Instagram Login (graph.instagram.com, sem Página do Facebook)
  ou with Facebook Login (graph.facebook.com). Tokens de longa duração valem 60 dias.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.logging_config import redact

logger = logging.getLogger(__name__)

FEED_METRICS = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions",
                "profile_visits", "profile_activity", "follows"]


class MetaError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None,
                 retryable: bool = False, ambiguous: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous  # pode ter sido aplicado no servidor; verificar antes de repetir


class MetaNotConfigured(MetaError):
    pass


@dataclass
class ContainerStatus:
    status_code: str
    raw: dict[str, Any]


# Códigos de erro da Graph API tratados como temporários
_RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80002}


class MetaService:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None, sleep=time.sleep):
        self.settings = settings or get_settings()
        self._client = client
        self._sleep = sleep

    # --------------------------------------------------------------- base
    @property
    def base(self) -> str:
        return f"{self.settings.meta_host}/{self.settings.meta_graph_api_version}"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.meta_timeout_seconds)
        return self._client

    def _token(self, override: str | None = None) -> str:
        if override:
            return override
        from app.services.token_store import current_access_token

        tok = current_access_token(self.settings)
        if not tok:
            raise MetaNotConfigured("META_ACCESS_TOKEN não configurado")
        return tok

    def _ig_id(self) -> str:
        if not self.settings.meta_ig_user_id:
            raise MetaNotConfigured("META_IG_USER_ID não configurado")
        return self.settings.meta_ig_user_id

    def _request(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
                 mutating: bool = False, token: str | None = None, base: str | None = None) -> dict[str, Any]:
        url = f"{base or self.base}/{path.lstrip('/')}"
        tok = self._token(token)
        headers = {"Authorization": f"Bearer {tok}"}
        try:
            resp = self._http().request(method, url, params=params, data=data, headers=headers,
                                        timeout=self.settings.meta_timeout_seconds)
        except httpx.TimeoutException as e:
            raise MetaError(f"timeout em {path}", retryable=True, ambiguous=mutating) from e
        except httpx.TransportError as e:
            raise MetaError(f"falha de rede em {path}: {type(e).__name__}", retryable=True, ambiguous=mutating) from e
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = err.get("code")
            msg = redact(str(err.get("message") or body)[:500], [tok])
            retryable = resp.status_code >= 500 or resp.status_code == 429 or code in _RETRYABLE_CODES
            raise MetaError(f"Graph API {resp.status_code} (code={code}): {msg}", status=resp.status_code,
                            code=code, retryable=retryable, ambiguous=mutating and resp.status_code >= 500)
        return body

    # ---------------------------------------------------------- publicação
    def create_image_container(self, image_url: str, caption: str, alt_text: str | None = None) -> str:
        if not image_url.lower().startswith("https://"):
            raise MetaError("image_url precisa ser uma URL pública HTTPS")
        data = {"image_url": image_url, "caption": caption}
        if alt_text:
            data["alt_text"] = alt_text[:1000]
        body = self._request("POST", f"{self._ig_id()}/media", data=data, mutating=True)
        if "id" not in body:
            raise MetaError(f"resposta sem id do contêiner: {body}")
        return str(body["id"])

    def container_status(self, container_id: str) -> ContainerStatus:
        body = self._request("GET", container_id, params={"fields": "status_code,status"})
        return ContainerStatus(str(body.get("status_code", "UNKNOWN")), body)

    def wait_container(self, container_id: str, max_checks: int | None = None, interval: float = 5.0) -> ContainerStatus:
        max_checks = max_checks or self.settings.container_max_checks
        st = ContainerStatus("UNKNOWN", {})
        for i in range(max_checks):
            st = self.container_status(container_id)
            if st.status_code in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                return st
            if i < max_checks - 1:
                self._sleep(interval)
        return st

    def publish_container(self, container_id: str) -> str:
        body = self._request("POST", f"{self._ig_id()}/media_publish", data={"creation_id": container_id},
                             mutating=True)
        if "id" not in body:
            raise MetaError(f"resposta sem id da mídia: {body}", ambiguous=True)
        return str(body["id"])

    def publishing_limit(self) -> dict[str, Any]:
        body = self._request("GET", f"{self._ig_id()}/content_publishing_limit",
                             params={"fields": "config,quota_usage"})
        data = (body.get("data") or [{}])[0]
        return {"quota_usage": data.get("quota_usage"), "quota_total": (data.get("config") or {}).get("quota_total"),
                "quota_duration": (data.get("config") or {}).get("quota_duration")}

    def get_media(self, media_id: str) -> dict[str, Any]:
        return self._request("GET", media_id,
                             params={"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count"})

    def recent_media(self, limit: int = 25) -> list[dict[str, Any]]:
        body = self._request("GET", f"{self._ig_id()}/media",
                             params={"fields": "id,caption,timestamp,permalink", "limit": limit})
        return body.get("data", [])

    def find_published_by_caption(self, caption: str, limit: int = 25) -> dict[str, Any] | None:
        """Usado após falhas ambíguas: verifica se a publicação já aconteceu antes de repetir."""
        target = (caption or "").strip()[:200]
        for m in self.recent_media(limit):
            if (m.get("caption") or "").strip()[:200] == target:
                return m
        return None

    def account_info(self) -> dict[str, Any]:
        fields = "id,username,account_type,media_count" if self.settings.meta_login_type == "instagram" else "id,username,media_count"
        return self._request("GET", self._ig_id(), params={"fields": fields})

    # ------------------------------------------------------------- métricas
    def media_insights(self, media_id: str, metrics: list[str] | None = None) -> tuple[dict[str, int], list[str]]:
        """Retorna (valores, indisponíveis). Uma métrica não suportada não derruba as demais."""
        metrics = list(metrics or FEED_METRICS)
        values: dict[str, int] = {}
        unavailable: list[str] = []
        try:
            values = self._insights(media_id, metrics)
        except MetaError as e:
            if e.code != 100 and e.status not in (400,):
                raise
            # alguma métrica inválida para esta mídia/conta: consulta individualmente
            for m in metrics:
                try:
                    values.update(self._insights(media_id, [m]))
                except MetaError as e2:
                    if e2.retryable:
                        raise
                    unavailable.append(m)
        for m in metrics:
            if m not in values and m not in unavailable:
                unavailable.append(m)  # conjunto vazio = métrica indisponível (não é zero)
        return values, unavailable

    def _insights(self, media_id: str, metrics: list[str]) -> dict[str, int]:
        body = self._request("GET", f"{media_id}/insights", params={"metric": ",".join(metrics), "period": "lifetime"})
        out: dict[str, int] = {}
        for item in body.get("data", []):
            name = item.get("name")
            val = None
            if item.get("values"):
                val = item["values"][0].get("value")
            elif item.get("total_value"):
                val = item["total_value"].get("value")
            if isinstance(val, (int, float)):
                out[name] = int(val)
        return out

    # ----------------------------------------------------------------- token
    def refresh_long_lived_token(self, token: str) -> tuple[str, int | None]:
        """Renova o token de longa duração (Instagram Login: ig_refresh_token; Facebook Login: fb_exchange_token)."""
        s = self.settings
        if s.meta_login_type == "unified":
            raise MetaError(
                "Unified Login usa token de usuário do sistema (SUAT); "
                "este tipo de token não deve passar pela renovação de tokens long-lived. "
                "Gere/substitua o token no painel da Meta quando necessário."
            )
        if s.meta_login_type == "instagram":
            resp = self._http().get("https://graph.instagram.com/refresh_access_token",
                                    params={"grant_type": "ig_refresh_token", "access_token": token})
        else:
            if not (s.meta_app_id and s.meta_app_secret):
                raise MetaError("META_APP_ID e META_APP_SECRET são necessários para renovar token do Facebook Login")
            resp = self._http().get(f"https://graph.facebook.com/{s.meta_graph_api_version}/oauth/access_token",
                                    params={"grant_type": "fb_exchange_token", "client_id": s.meta_app_id,
                                            "client_secret": s.meta_app_secret.get_secret_value(),
                                            "fb_exchange_token": token})
        if resp.status_code >= 400:
            raise MetaError(redact(f"falha ao renovar token: HTTP {resp.status_code} {resp.text[:300]}", [token]),
                            status=resp.status_code)
        body = resp.json()
        return body["access_token"], body.get("expires_in")