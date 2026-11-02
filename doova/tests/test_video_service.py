from __future__ import annotations

import httpx
import pytest
import sys
import types


def _pexels_video() -> dict:
    return {
        "id": 101,
        "url": "https://www.pexels.com/video/101/",
        "duration": 8,
        "user": {"name": "Criador Pexels"},
        "video_files": [
            {"link": "https://cdn.example/low.mp4", "file_type": "video/mp4", "width": 640, "height": 1138},
            {"link": "https://cdn.example/hd.mp4", "file_type": "video/mp4", "width": 1080, "height": 1920},
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
        assert request.url.path == "/v1/videos/search"
        assert request.url.params["query"] == "chuva"
        assert "orientation" not in request.url.params
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


def test_search_videos_can_require_pexels(monkeypatch):
    from app.config import reset_settings_cache
    from app.services.video_service import VideoSearchError, search_videos

    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay-test-key")
    monkeypatch.setenv("VIDEO_PIXABAY_FALLBACK_ENABLED", "false")
    reset_settings_cache()
    from app.config import get_settings

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.pexels.com"
        return httpx.Response(200, json={"videos": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(VideoSearchError, match="nenhum vídeo"):
            search_videos("chuva", get_settings(), client=client)


def test_search_videos_expands_portuguese_query_when_pexels_is_empty(monkeypatch):
    from app.config import get_settings, reset_settings_cache
    from app.services.video_service import search_videos

    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    monkeypatch.setenv("VIDEO_PIXABAY_FALLBACK_ENABLED", "false")
    reset_settings_cache()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        if request.url.params["query"] == "rain":
            return httpx.Response(200, json={"videos": [_pexels_video()]})
        return httpx.Response(200, json={"videos": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidates = search_videos("chuva forte", get_settings(), client=client)

    assert calls[:2] == ["chuva forte", "chuva"]
    assert "rain" in calls
    assert candidates[0].provider == "pexels"


def test_tts_generates_mp3_with_gtts(monkeypatch, tmp_path):
    from app.config import reset_settings_cache
    from app.services import video_service

    calls: dict[str, object] = {}

    class FakeGTTS:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def save(self, path: str) -> None:
            with open(path, "wb") as audio:
                audio.write(b"ID3 fake mp3")

    monkeypatch.setitem(sys.modules, "gtts", types.SimpleNamespace(gTTS=FakeGTTS))
    monkeypatch.setenv("VIDEO_TTS_PROVIDER", "gtts")
    reset_settings_cache()
    output = tmp_path / "narration.mp3"

    from app.config import get_settings

    video_service._tts_narration("Oi", get_settings(), output, 5)

    assert output.read_bytes().startswith(b"ID3")
    assert calls == {"text": "Oi", "lang": "pt", "tld": "com.br", "slow": False}


def test_auto_tts_does_not_fallback_to_gtts(monkeypatch, tmp_path):
    from app.config import get_settings, reset_settings_cache
    from app.services import video_service

    monkeypatch.setenv("VIDEO_TTS_PROVIDER", "auto")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    reset_settings_cache()

    with pytest.raises(video_service.VideoSearchError, match="ElevenLabs não configurado"):
        video_service._tts_narration("Oi", get_settings(), tmp_path / "narration.mp3", 5)


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

    video_service._tts_narration(
        "Oi", get_settings(), output, 5, elevenlabs_text="[tom acolhedor] Oi"
    )

    assert output.read_bytes().startswith(b"ID3")
    assert calls["url"].endswith("/voice-br")
    assert calls["headers"]["xi-api-key"] == "eleven-test-key"
    assert calls["json"]["text"] == "[tom acolhedor] Oi"


def test_elevenlabs_402_explains_library_voice_restriction(monkeypatch, tmp_path):
    from app.config import get_settings, reset_settings_cache
    from app.services import video_service

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "library-voice")
    reset_settings_cache()

    class FakeResponse:
        status_code = 402
        content = b""
        text = '{"detail":{"code":"paid_plan_required"}}'

    monkeypatch.setattr(video_service.httpx, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(video_service.VideoSearchError, match="Voice Library"):
        video_service._tts_narration("Oi", get_settings(), tmp_path / "neural.mp3", 5)


def test_elevenlabs_narration_prompt_is_clean(monkeypatch):
    from app.config import get_settings, reset_settings_cache
    from app.services.video_service import _elevenlabs_narration_prompt

    texts = ["Gancho", "Ajuda", "Doova", "Acao"]
    clean = ". ".join(texts)

    monkeypatch.setenv("VIDEO_TTS_PROVIDER", "gtts")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    reset_settings_cache()
    assert _elevenlabs_narration_prompt(texts, get_settings()) == clean

    monkeypatch.setenv("VIDEO_TTS_PROVIDER", "auto")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-br")
    reset_settings_cache()
    prompt = _elevenlabs_narration_prompt(texts, get_settings())

    assert prompt == clean
    assert "[tom" not in prompt
    assert all(text in prompt for text in texts)


def test_generated_video_image_is_created_dynamically(tmp_path):
    from PIL import Image

    from app.config import get_settings
    from app.services.video_service import _generate_video_image

    output = tmp_path / "generated.png"
    _generate_video_image("chuva", "chuva forte", "Encontre serviços no Doova.", output,
                          get_settings(), 0)

    assert output.exists()
    with Image.open(output) as image:
        assert image.size == (get_settings().video_width, get_settings().video_height)


def test_ffmpeg_uses_image_loop_for_generated_cards(tmp_path):
    from app.services.video_service import _ffmpeg_loop_input

    assert _ffmpeg_loop_input(tmp_path / "generated.png") == [
        "-loop", "1", "-framerate", "30", "-i", str(tmp_path / "generated.png")
    ]


def test_ass_karaoke_marks_each_word_with_timing():
    from app.config import get_settings
    from app.services.video_service import _ass

    result = _ass([{"start": 0, "end": 2, "text": "Chuva chegou"}], get_settings(), "Inter")

    assert "Style: Default,Inter,43,&H0000FFFF,&H00FFFFFF" in result
    assert "{\\k" in result
    assert "Chuva" in result and "chegou" in result


def test_script_ends_with_doova_install_cta():
    from app.services.video_service import _script_texts

    _, texts = _script_texts("chuva", "chuva")

    assert texts[-1] == "Instale o app Doova e encontre ajuda ou ofereça seu serviço."


def test_render_video_sources_builds_cuts_between_placeholders(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services import video_service

    sources = [tmp_path / "source-0.mp4", tmp_path / "source-1.mp4"]
    for source in sources:
        source.write_bytes(b"fake")
    calls: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"fake output")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    from pathlib import Path

    monkeypatch.setattr(video_service.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(video_service.subprocess, "run", fake_run)
    output = tmp_path / "output.mp4"

    video_service._render_video_sources(
        sources,
        [{"start": 0, "end": 4, "text": "Chuva chegou"}],
        get_settings(),
        output,
        4,
    )

    command = calls["cmd"]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "xfade=transition=fade" in filter_graph
    assert "duration=0.350" in filter_graph
    assert "setpts=PTS-STARTPTS,fps=30,format=yuv420p,settb=AVTB,setsar=1" in filter_graph
    assert any(str(item).endswith("source-0.mp4") for item in command)
    assert any(str(item).endswith("source-1.mp4") for item in command)
    assert output.read_bytes() == b"fake output"


def test_video_request_supports_forcing_a_previous_stock_video():
    from app.routes.videos import VideoTrendRequest

    request = VideoTrendRequest(trend_term="chuva", theme_key="chuva", force=True)

    assert request.force is True
