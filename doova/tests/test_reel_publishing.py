import httpx
import pytest
from urllib.parse import parse_qs
from pydantic import SecretStr

from app.config import Settings
from app.services.meta_service import MetaError, MetaService


def test_create_reel_container_sends_reels_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["data"] = parse_qs(request.content.decode())
        assert request.method == "POST"
        assert request.url.path.endswith("/ig-user/media")
        return httpx.Response(200, json={"id": "container-123"})

    settings = Settings(meta_ig_user_id="ig-user", meta_access_token=SecretStr("test-token"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        container = MetaService(settings, client=client).create_reel_container(
            "https://res.cloudinary.com/demo/video/upload/reel.mp4", "Legenda", True
        )

    assert container == "container-123"
    assert seen["data"]["media_type"] == ["REELS"]
    assert seen["data"]["video_url"] == ["https://res.cloudinary.com/demo/video/upload/reel.mp4"]
    assert seen["data"]["share_to_feed"] == ["true"]


def test_create_reel_container_rejects_non_https_url():
    settings = Settings(meta_ig_user_id="ig-user", meta_access_token=SecretStr("test-token"))
    with pytest.raises(MetaError, match="HTTPS"):
        MetaService(settings).create_reel_container("http://example.com/reel.mp4", "Legenda")


def test_create_reel_container_includes_configured_location_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["data"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"id": "container-123"})

    settings = Settings(meta_ig_user_id="ig-user", meta_access_token=SecretStr("test-token"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        MetaService(settings, client=client).create_reel_container(
            "https://res.cloudinary.com/demo/video/upload/reel.mp4", "Legenda",
            location_id="goiania-place-id",
        )

    assert seen["data"]["location_id"] == ["goiania-place-id"]
