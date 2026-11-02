from __future__ import annotations

import httpx
import sys
import types


def _pexels_video() -> dict:
    return {
        "id": 101,
        "url": "https://www.pexels.com/video/101/",
        "duration": 8,
        "user": {"name": "Criador Pexels"},
        "video_files": [
            {"link": "https://cdn.example/low.mp4", "file_type": "mp4", "width": 640, "height": 1138},
            {"link": "https://cdn.example/hd.mp4", "file_type": "mp4", "width": 1080, "height": 1920},
        ],
    }


def _pixabay_video() -> dict:
    return {
        "id": 202,
        "tags": "chuva",
        "user": "Criador Pixabay",
        "pageURL": "https://pixabay.com/videos/202/",
        "duration": 7,
        "videos": {"medium": {"url": "https://cdn.example/pixabay.mp4", "width": 1280, "height": 720}},
    }


def test_search_videos_prefers_pexels(monkeypatch):
    from app.config import reset_settings_cache
    from app.services.video_service import search_videos

    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay-test-key")
    reset_settings_cache()
    from app.config import get_settings

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        assert request.headers["Authorization"] == "pexels-test-key"
        assert request.url.params["query"] == "chuva"
        return httpx.Response(200, json={"videos": [_pexels_video()]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidates = search_videos("chuva", get_settings(), client=client)

    assert calls == ["api.pexels.com"]
    assert candidates[0].provider == "pexels"
    assert candidates[0].download_url.endswith("hd.mp4")
    assert candidates[0].width == 1080


def test_search_videos_falls_back_to_pixabay_when_pexels_is_empty(monkeypatch):
    from app.config import reset_settings_cache
    from app.services.video_service import search_videos

    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay-test-key")
    reset_settings_cache()
    from app.config import get_settings

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        if request.url.host == "api.pexels.com":
            return httpx.Response(200, json={"videos": []})
        assert request.url.params["key"] == "pixabay-test-key"
        return httpx.Response(200, json={"hits": [_pixabay_video()]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidates = search_videos("chuva", get_settings(), client=client)

    assert calls == ["api.pexels.com", "pixabay.com"]
    assert candidates[0].provider == "pixabay"
    assert candidates[0].download_url.endswith("pixabay.mp4")


def test_tts_generates_mp3_with_gtts(monkeypatch, tmp_path):
    from app.services import video_service

    calls: dict[str, object] = {}

    class FakeGTTS:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def save(self, path: str) -> None:
            with open(path, "wb") as audio:
                audio.write(b"ID3 fake mp3")

    monkeypatch.setitem(sys.modules, "gtts", types.SimpleNamespace(gTTS=FakeGTTS))
    output = tmp_path / "narration.mp3"

    from app.config import get_settings

    video_service._tts_narration("Oi", get_settings(), output, 5)

    assert output.read_bytes().startswith(b"ID3")
    assert calls == {"text": "Oi", "lang": "pt", "tld": "com.br", "slow": False}


def test_tts_prefers_elevenlabs_when_configured(monkeypatch, tmp_path):
    from app.config import get_settings, reset_settings_cache
    from app.services import video_service

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-br")
    reset_settings_cache()
    calls: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b"ID3 neural mp3"
        text = ""

    def fake_post(url, **kwargs):
        calls.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(video_service.httpx, "post", fake_post)
    output = tmp_path / "neural.mp3"

    video_service._tts_narration("Oi", get_settings(), output, 5)

    assert output.read_bytes().startswith(b"ID3")
    assert calls["url"].endswith("/voice-br")
    assert calls["headers"]["xi-api-key"] == "eleven-test-key"
