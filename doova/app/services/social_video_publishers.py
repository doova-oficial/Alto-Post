"""Publicação de Reels nas APIs oficiais do Facebook e TikTok."""
from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.logging_config import redact

FACEBOOK_REELS_GRAPH_URL = "https://graph.facebook.com"
TIKTOK_API_URL = "https://open.tiktokapis.com"
TIKTOK_CHUNK_SIZE = 10 * 1024 * 1024
MAX_TIKTOK_VIDEO_BYTES = 512 * 1024 * 1024


class SocialVideoPublishError(RuntimeError):
    pass


def _response_json(response: httpx.Response, platform: str, secrets: list[str],
                   check_body_error: bool = True) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise SocialVideoPublishError(f"{platform}: resposta inválida (HTTP {response.status_code})") from exc
    if not isinstance(body, dict):
        raise SocialVideoPublishError(f"{platform}: formato de resposta inesperado")
    error = body.get("error")
    if response.is_error or (check_body_error and error):
        message = error.get("message", "erro da API") if isinstance(error, dict) else "erro da API"
        raise SocialVideoPublishError(
            f"{platform}: HTTP {response.status_code}: {redact(str(message)[:400], secrets)}"
        )
    return body


class FacebookReelPublisher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.facebook_configured:
            raise SocialVideoPublishError("Facebook exige FACEBOOK_PAGE_ID e FACEBOOK_PAGE_ACCESS_TOKEN")
        self.settings = settings
        self.page_id = settings.facebook_page_id
        self.token = settings.facebook_page_access_token.get_secret_value()
        self._client = client or httpx.Client(timeout=settings.meta_timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _graph(self, path: str) -> str:
        return f"{FACEBOOK_REELS_GRAPH_URL}/{self.settings.meta_graph_api_version}/{path.lstrip('/')}"

    def publish(self, video_url: str, caption: str, metadata: dict[str, Any]) -> dict[str, Any]:
        video_id = metadata.get("facebook_video_id")
        if video_id:
            return self.status(str(video_id))
        if not video_url.startswith("https://"):
            raise SocialVideoPublishError("Facebook exige uma URL HTTPS pública para o vídeo")
        try:
            started = _response_json(self._client.post(
                self._graph(f"{self.page_id}/video_reels"),
                data={"upload_phase": "start", "access_token": self.token},
            ), "Facebook", [self.token])
            video_id = started.get("video_id")
            upload_url = started.get("upload_url")
            upload_host = urlsplit(upload_url).hostname if isinstance(upload_url, str) else None
            if not video_id or not isinstance(upload_url, str) or upload_host != "rupload.facebook.com":
                raise SocialVideoPublishError("Facebook não retornou video_id/upload_url válidos")
            uploaded = _response_json(self._client.post(
                upload_url,
                headers={"Authorization": f"OAuth {self.token}", "file_url": video_url},
            ), "Facebook", [self.token])
            if uploaded.get("success") is not True:
                raise SocialVideoPublishError("Facebook não confirmou o upload do vídeo")
            _response_json(self._client.post(
                self._graph(f"{self.page_id}/video_reels"),
                data={
                    "upload_phase": "finish",
                    "video_id": str(video_id),
                    "video_state": "PUBLISHED",
                    "description": caption[:2200],
                    "access_token": self.token,
                },
            ), "Facebook", [self.token])
        except httpx.HTTPError as exc:
            raise SocialVideoPublishError(f"Facebook: falha de rede ({type(exc).__name__})") from exc
        return {"status": "processing", "video_id": str(video_id)}

    def status(self, video_id: str) -> dict[str, Any]:
        try:
            body = _response_json(self._client.get(
                self._graph(video_id),
                params={"fields": "status,permalink_url", "access_token": self.token},
            ), "Facebook", [self.token])
        except httpx.HTTPError as exc:
            raise SocialVideoPublishError(f"Facebook: falha ao consultar status ({type(exc).__name__})") from exc
        status = body.get("status") or {}
        publishing = status.get("publishing_phase") or {}
        processing = status.get("processing_phase") or {}
        if publishing.get("publish_status") == "published":
            return {"status": "published", "video_id": video_id, "permalink": body.get("permalink_url")}
        if status.get("video_status") in ("error", "expired", "upload_failed") \
                or publishing.get("status") == "error" or processing.get("status") == "error":
            details = publishing.get("error") or processing.get("error") or {}
            raise SocialVideoPublishError(
                f"Facebook falhou ao processar o Reel: {str(details.get('message') or status.get('video_status'))[:300]}"
            )
        return {"status": "processing", "video_id": video_id}


class TikTokVideoPublisher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.tiktok_configured:
            raise SocialVideoPublishError("TikTok exige TIKTOK_ACCESS_TOKEN")
        self.settings = settings
        self.token = settings.tiktok_access_token.get_secret_value()
        self._client = client or httpx.Client(timeout=settings.video_timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                f"{TIKTOK_API_URL}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {self.token}", **kwargs.pop("headers", {})},
                timeout=self.settings.video_timeout_seconds,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise SocialVideoPublishError(f"TikTok: falha de rede ({type(exc).__name__})") from exc
        body = _response_json(response, "TikTok", [self.token], check_body_error=False)
        error = body.get("error") or {}
        if error.get("code") not in (None, "ok"):
            message = redact(str(error.get("message") or error.get("code"))[:400], [self.token])
            raise SocialVideoPublishError(f"TikTok: {message}")
        return body

    def publish(self, video_url: str, caption: str, metadata: dict[str, Any]) -> dict[str, Any]:
        publish_id = metadata.get("tiktok_publish_id")
        if publish_id:
            return self.status(str(publish_id))
        info = self._request(
            "POST",
            "/v2/post/publish/creator_info/query/",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={},
        )
        data = info.get("data") or {}
        options = data.get("privacy_level_options") or []
        privacy = self.settings.tiktok_privacy_level
        if privacy not in options:
            raise SocialVideoPublishError(
                f"TikTok não oferece a privacidade configurada ({privacy}); opções disponíveis: {options}"
            )
        with tempfile.TemporaryDirectory(prefix="doova-tiktok-") as directory:
            video_path = Path(directory) / "reel.mp4"
            self._download_video(video_url, video_path)
            size = video_path.stat().st_size
            chunk_size = min(TIKTOK_CHUNK_SIZE, size)
            total_chunks = math.ceil(size / chunk_size)
            initialized = self._request(
                "POST",
                "/v2/post/publish/video/init/",
                headers={"Content-Type": "application/json; charset=UTF-8"},
                json={
                    "post_info": {"title": _truncate_utf16(caption, 2200), "privacy_level": privacy},
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": total_chunks,
                    },
                },
            )
            result = initialized.get("data") or {}
            publish_id = result.get("publish_id")
            upload_url = result.get("upload_url")
            upload_host = urlsplit(upload_url).hostname if isinstance(upload_url, str) else None
            if not publish_id or upload_host != "open-upload.tiktokapis.com":
                raise SocialVideoPublishError("TikTok não retornou publish_id/upload_url válidos")
            self._upload_chunks(video_path, upload_url, size, chunk_size, total_chunks)
        return self.status(str(publish_id))

    def _download_video(self, video_url: str, destination: Path) -> None:
        if not video_url.startswith("https://"):
            raise SocialVideoPublishError("TikTok exige uma URL HTTPS pública para o vídeo")
        try:
            with self._client.stream(
                "GET", video_url, timeout=self.settings.video_timeout_seconds, follow_redirects=True,
            ) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > MAX_TIKTOK_VIDEO_BYTES:
                    raise SocialVideoPublishError("Vídeo excede o limite de upload do TikTok")
                total = 0
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_TIKTOK_VIDEO_BYTES:
                            raise SocialVideoPublishError("Vídeo excede o limite de upload do TikTok")
                        output.write(chunk)
        except httpx.HTTPError as exc:
            raise SocialVideoPublishError(f"TikTok: falha ao baixar o vídeo ({type(exc).__name__})") from exc
        if total == 0:
            raise SocialVideoPublishError("TikTok recebeu um vídeo vazio")

    def _upload_chunks(self, path: Path, upload_url: str, size: int,
                       chunk_size: int, total_chunks: int) -> None:
        try:
            with path.open("rb") as source:
                for index in range(total_chunks):
                    start = index * chunk_size
                    chunk = source.read(min(chunk_size, size - start))
                    end = start + len(chunk) - 1
                    response = self._client.put(
                        upload_url,
                        content=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                        timeout=self.settings.video_timeout_seconds,
                    )
                    if response.is_error:
                        raise SocialVideoPublishError(
                            f"TikTok recusou o bloco {index + 1}/{total_chunks} (HTTP {response.status_code})"
                        )
        except httpx.HTTPError as exc:
            raise SocialVideoPublishError(f"TikTok: falha no upload ({type(exc).__name__})") from exc

    def status(self, publish_id: str) -> dict[str, Any]:
        body = self._request(
            "POST",
            "/v2/post/publish/status/fetch/",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={"publish_id": publish_id},
        )
        data = body.get("data") or {}
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
            post_id = ids[0] if ids else None
            return {"status": "published", "publish_id": publish_id, "post_id": post_id}
        if status == "FAILED":
            raise SocialVideoPublishError(f"TikTok falhou ao publicar: {str(data.get('fail_reason') or 'erro desconhecido')[:300]}")
        return {"status": "processing", "publish_id": publish_id, "platform_status": status}


def _truncate_utf16(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[:limit * 2].decode("utf-16-le", errors="ignore")
