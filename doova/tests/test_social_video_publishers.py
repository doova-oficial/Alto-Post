import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.social_video_publishers import (
    FacebookReelPublisher,
    SocialVideoPublishError,
    TikTokVideoPublisher,
    _truncate_utf16,
)


def _settings(**values):
    return Settings(
        app_env="test",
        facebook_page_id=values.get("facebook_page_id", "page-123"),
        facebook_page_access_token=values.get(
            "facebook_page_access_token", SecretStr("facebook-test-token"),
        ),
        tiktok_access_token=values.get("tiktok_access_token", SecretStr("tiktok-test-token")),
        tiktok_privacy_level=values.get("tiktok_privacy_level", "SELF_ONLY"),
        video_timeout_seconds=5,
    )


def test_facebook_reel_uploads_hosted_video_and_checks_publication():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "graph.facebook.com" and request.method == "POST":
            if request.url.path.endswith("/video_reels"):
                if request.content and b"upload_phase=start" in request.content:
                    return httpx.Response(200, json={
                        "video_id": "fb-video-1",
                        "upload_url": "https://rupload.facebook.com/video-upload/fb-video-1",
                    })
                assert b"upload_phase=finish" in request.content
                assert b"video_state=PUBLISHED" in request.content
                return httpx.Response(200, json={"success": True})
        if request.url.host == "rupload.facebook.com":
            assert request.headers["Authorization"] == "OAuth facebook-test-token"
            assert request.headers["file_url"] == "https://cdn.example/reel.mp4"
            return httpx.Response(200, json={"success": True})
        if request.url.host == "graph.facebook.com" and request.method == "GET":
            return httpx.Response(200, json={
                "status": {
                    "video_status": "ready",
                    "publishing_phase": {"status": "completed", "publish_status": "published"},
                },
                "permalink_url": "https://www.facebook.com/reel/fb-video-1",
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publisher = FacebookReelPublisher(_settings(), client=client)
        started = publisher.publish("https://cdn.example/reel.mp4", "Legenda", {})
        result = publisher.status(started["video_id"])

    assert started == {"status": "processing", "video_id": "fb-video-1"}
    assert result == {
        "status": "published",
        "video_id": "fb-video-1",
        "permalink": "https://www.facebook.com/reel/fb-video-1",
    }
    assert len(calls) == 4


def test_facebook_rejects_upload_url_from_unexpected_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "video_id": "fb-video-1",
            "upload_url": "https://attacker.example/upload",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publisher = FacebookReelPublisher(_settings(), client=client)
        with pytest.raises(SocialVideoPublishError, match="upload_url"):
            publisher.publish("https://cdn.example/reel.mp4", "Legenda", {})


def test_tiktok_direct_post_uploads_chunks_and_checks_status():
    calls = []
    video_bytes = b"\x00\x00\x00\x18ftypisom" + b"x" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=video_bytes)
        if request.url.path.endswith("/creator_info/query/"):
            assert request.headers["Authorization"] == "Bearer tiktok-test-token"
            return httpx.Response(200, json={
                "data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]},
                "error": {"code": "ok", "message": ""},
            })
        if request.url.path.endswith("/video/init/"):
            payload = json.loads(request.read())
            assert payload["post_info"]["privacy_level"] == "SELF_ONLY"
            assert payload["source_info"]["video_size"] == len(video_bytes)
            return httpx.Response(200, json={
                "data": {
                    "publish_id": "tt-publish-1",
                    "upload_url": "https://open-upload.tiktokapis.com/upload/one",
                },
                "error": {"code": "ok", "message": ""},
            })
        if request.url.host == "open-upload.tiktokapis.com":
            assert request.method == "PUT"
            assert request.headers["Content-Range"] == f"bytes 0-{len(video_bytes) - 1}/{len(video_bytes)}"
            assert request.content == video_bytes
            return httpx.Response(201)
        if request.url.path.endswith("/status/fetch/"):
            return httpx.Response(200, json={
                "data": {
                    "status": "PUBLISH_COMPLETE",
                    "publicaly_available_post_id": ["tt-post-1"],
                },
                "error": {"code": "ok", "message": ""},
            })
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publisher = TikTokVideoPublisher(_settings(), client=client)
        result = publisher.publish("https://cdn.example/reel.mp4", "Legenda", {})

    assert result == {
        "status": "published",
        "publish_id": "tt-publish-1",
        "post_id": "tt-post-1",
    }
    assert [request.method for request in calls] == ["POST", "GET", "POST", "PUT", "POST"]


def test_tiktok_rejects_unavailable_privacy_option():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"privacy_level_options": ["SELF_ONLY"]},
            "error": {"code": "ok", "message": ""},
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publisher = TikTokVideoPublisher(_settings(tiktok_privacy_level="PUBLIC_TO_EVERYONE"), client=client)
        with pytest.raises(SocialVideoPublishError, match="não oferece a privacidade"):
            publisher.publish("https://cdn.example/reel.mp4", "Legenda", {})


def test_caption_truncation_respects_tiktok_utf16_character_limit():
    assert _truncate_utf16("a😀b", 3) == "a😀"
