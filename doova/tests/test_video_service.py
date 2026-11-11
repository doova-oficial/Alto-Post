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


def test_get_pexels_video_fetches_requested_id(monkeypatch):
    from app.config import get_settings, reset_settings_cache
    from app.services.video_service import get_pexels_video

    monkeypatch.setenv("PEXELS_API_KEY", "pexels-test-key")
    reset_settings_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.pexels.com"
        assert request.url.path == "/v1/videos/videos/15909403"
        assert request.headers["Authorization"] == "pexels-test-key"
        return httpx.Response(200, json={**_pexels_video(), "id": 15909403})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidate = get_pexels_video("15909403", get_settings(), client=client)

    assert candidate.provider == "pexels"
    assert candidate.provider_id == "15909403"
    assert candidate.download_url.endswith("hd.mp4")


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


def test_elevenlabs_returns_audio_and_word_level_timestamps(monkeypatch):
    from app.config import get_settings, reset_settings_cache
    from app.services import video_service

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-br")
    reset_settings_cache()

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "audio_base64": "SUQz",
                "alignment": {
                    "characters": ["O", "i", " ", "D", "o", "o", "v", "a", "!"],
                    "character_start_times_seconds": [0.0, 0.15, 0.3, 0.5, 0.58, 0.66, 0.74, 0.82, 0.9],
                    "character_end_times_seconds": [0.15, 0.3, 0.4, 0.58, 0.66, 0.74, 0.82, 0.9, 1.0],
                },
            }

    calls = {}

    def fake_post(url, **kwargs):
        calls.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(video_service.httpx, "post", fake_post)
    audio, words = video_service._elevenlabs_audio_with_timestamps(
        "Oi Doova!", get_settings(), 5,
    )

    assert audio == b"ID3"
    assert words == [
        {"text": "Oi", "start": 0.0, "end": 0.3},
        {"text": "Doova!", "start": 0.5, "end": 1.0},
    ]
    assert calls["url"].endswith("/voice-br/with-timestamps")
    assert calls["headers"]["xi-api-key"] == "eleven-test-key"


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


def test_elevenlabs_narration_prompt_adds_tags_only_when_supported(monkeypatch):
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
    monkeypatch.setenv("ELEVENLABS_MODEL", "eleven_v4")
    reset_settings_cache()
    prompt = _elevenlabs_narration_prompt(texts, get_settings())

    assert prompt.startswith("[curious] Gancho. [thoughtful] Ajuda")
    assert prompt != clean
    assert all(text in prompt for text in texts)

    monkeypatch.setenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    reset_settings_cache()
    assert _elevenlabs_narration_prompt(texts, get_settings()) == clean


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


def test_generated_image_omits_script_text_when_subtitles_are_rendered(db, monkeypatch, tmp_path):
    from PIL import ImageDraw

    from app.config import get_settings
    from app.services import video_service

    candidate = video_service.VideoCandidate(
        provider="pexels",
        provider_id="test-video",
        title="Test video",
        creator="Pexels",
        landing_url="https://www.pexels.com/video/1/",
        download_url="https://cdn.example/video.mp4",
        width=1080,
        height=1920,
        duration=12,
        query="chuva",
    )
    monkeypatch.setattr(video_service, "search_videos", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(video_service, "_download", lambda _candidate, _settings, _client, path: path.write_bytes(b"video"))
    monkeypatch.setattr(
        ImageDraw.ImageDraw,
        "multiline_text",
        lambda *args, **kwargs: pytest.fail("generated image repeated text already present in subtitles"),
    )

    asset = video_service.create_video(
        db,
        "chuva",
        "chuva",
        "chuva",
        get_settings(),
        render=False,
        variant="clean_center",
        transition="fade",
        generated_image_count=1,
        script_override=("Gancho", [
            "Gancho",
            "Tem alguém que sabe resolver.",
            "Encontre serviços no Doova.",
            "Instale o app Doova e encontre ajuda ou ofereça seu serviço.",
        ]),
    )

    assert asset.metadata_json["generated_image_count"] == 1
    assert asset.metadata_json["texts"][1]["text"] == "Tem alguém que sabe resolver."


def test_ffmpeg_uses_image_loop_for_generated_cards(tmp_path):
    from app.services.video_service import _ffmpeg_loop_input

    assert _ffmpeg_loop_input(tmp_path / "generated.png") == [
        "-loop", "1", "-framerate", "30", "-i", str(tmp_path / "generated.png")
    ]


def test_ass_karaoke_marks_each_word_with_timing():
    from app.config import get_settings
    from app.services.video_service import _ass

    result = _ass([{"start": 0, "end": 2, "text": "Chuva chegou"}], get_settings(), "Inter")

    assert "Style: Default,Inter,64,&H0000FFFF,&H00FFFFFF" in result
    assert "{\\k" in result
    assert "Chuva" in result and "chegou" in result


def test_script_ends_with_doova_install_cta():
    from app.services.video_service import _script_texts

    _, texts = _script_texts("chuva", "chuva")

    assert texts[-1] == "Instale o app Doova e encontre ajuda ou ofereça seu serviço."
    assert texts[1] != "Tem alguém que sabe resolver."
    assert "calhas obstruídas" in texts[1]


def test_trend_script_uses_observed_context_and_three_second_hook():
    from app.services.video_service import _script_texts, subtitle_plan

    class ScriptGroq:
        def __init__(self):
            self.system = ""
            self.user = ""

        def chat_json(self, system, user, max_tokens):
            self.system, self.user = system, user
            return {"hook": "A chuva revelou esse problema?", "beats": [
                "Um telhado bem cuidado evita dor de cabeça.",
                "Encontre profissionais da sua região no Doova.",
            ]}

    groq = ScriptGroq()
    hook, rows = subtitle_plan(
        "chuva", "chuva forte em Iporá", 12,
        ["Previsão de chuva na região"], groq,
        [{"hook": "A goteira tem solução?", "theme_match": True,
          "avg_watch_time_over_duration": 0.72, "shares_over_reach": 0.03, "reach": 500}],
    )

    assert hook == rows[0]["text"] == "A chuva revelou esse problema?"
    assert rows[0]["start"] == 0 and rows[0]["end"] == 3
    assert "Previsão de chuva na região" in groq.user
    assert "A goteira tem solução?" in groq.user
    assert "observacionais" in groq.user
    assert "até 3 segundos" in groq.system
    assert "audiência ampla" in groq.system
    assert "emoção principal" in groq.system
    assert "lacuna de curiosidade" in groq.system
    assert "Demonstre autoridade" in groq.system
    assert rows[-1]["text"] == "Instale o app Doova e encontre ajuda ou ofereça seu serviço."
    assert rows[-1]["end"] == 12


def test_fallback_hook_varies_by_trend_for_same_theme():
    from app.services.video_service import _script_texts

    chuva, _ = _script_texts("chuva", "chuva em Iporá")
    calor, _ = _script_texts("chuva", "alerta de temporal no Centro-Oeste")

    assert chuva != calor


def test_recent_video_performance_context_ignores_metrics_before_seven_days(db):
    from app.models import VideoAsset, VideoMetricSnapshot
    from app.services.video_service import _recent_video_performance_context

    for index in range(3):
        asset = VideoAsset(
            provider="pexels", provider_id=f"history-{index}", query="roof", title="t", creator="c",
            source="Pexels", license="Pexels License", landing_url="https://pexels.com/x",
            download_url="https://pexels.com/x.mp4", width=1080, height=1920, duration_seconds=10,
            video_url="https://example.com/video.mp4", subtitle_text="", hook=f"gancho-{index}",
            trend_term="chuva", theme_key="chuva", metadata_json={"instagram_media_id": f"ig-{index}"},
        )
        db.add(asset)
        db.flush()
        db.add(VideoMetricSnapshot(video_id=asset.id, age_hours=120, reach=100, shares=5,
                                   avg_watch_time_ms=5000))
    db.commit()

    assert _recent_video_performance_context(db, "chuva") == []


def test_recent_video_performance_context_returns_best_and_worst_reels(db):
    from app.models import VideoAsset, VideoMetricSnapshot
    from app.services.video_service import _recent_video_performance_context

    for index, watch_time in enumerate((2000, 4000, 6000)):
        asset = VideoAsset(
            provider="pexels", provider_id=f"history-{index}", query="roof", title="t", creator="c",
            source="Pexels", license="Pexels License", landing_url="https://pexels.com/x",
            download_url="https://pexels.com/x.mp4", width=1080, height=1920, duration_seconds=10,
            video_url="https://example.com/video.mp4", subtitle_text="", hook=f"gancho-{index}",
            trend_term="chuva", theme_key="chuva", metadata_json={"instagram_media_id": f"ig-{index}"},
        )
        db.add(asset)
        db.flush()
        db.add(VideoMetricSnapshot(video_id=asset.id, age_hours=200, reach=100, shares=index,
                                   avg_watch_time_ms=watch_time))
    db.commit()

    context = _recent_video_performance_context(db, "chuva")

    assert {item["hook"] for item in context} == {"gancho-0", "gancho-1", "gancho-2"}
    assert context[0]["avg_watch_time_over_duration"] == 0.6
    assert context[-1]["avg_watch_time_over_duration"] == 0.2


def test_render_video_sources_builds_cuts_between_placeholders(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services import video_service

    sources = [tmp_path / "source-0.mp4", tmp_path / "source-1.mp4"]
    for source in sources:
        source.write_bytes(b"fake")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
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

    assert len(calls) == 3  # duas normalizações + uma transição final
    command = calls[-1]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "xfade=transition=fade" in filter_graph
    assert "duration=0.350" in filter_graph
    assert "setpts=PTS-STARTPTS,fps=30,format=yuv420p,settb=AVTB,setsar=1" in filter_graph
    assert command[command.index("-filter_complex_threads") + 1] == "1"
    assert command[command.index("-threads") + 1] == "1"
    assert any(str(item).endswith("__doova-normalized-0.mp4") for item in command)
    assert any(str(item).endswith("__doova-normalized-1.mp4") for item in command)
    assert sum(item == "-i" for item in command) == 2
    assert output.read_bytes() == b"fake output"


def test_render_video_sources_keeps_two_inputs_per_transition(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services import video_service

    sources = [tmp_path / f"source-{index}.mp4" for index in range(3)]
    for source in sources:
        source.write_bytes(b"fake")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
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
        6,
    )

    assert len(calls) == 5  # três normalizações + duas transições
    transition_commands = [cmd for cmd in calls if "-filter_complex" in cmd]
    assert len(transition_commands) == 2
    assert all(sum(item == "-i" for item in cmd) == 2 for cmd in transition_commands)
    assert output.read_bytes() == b"fake output"


def test_video_request_supports_forcing_a_previous_stock_video():
    from app.routes.videos import VideoTrendRequest

    request = VideoTrendRequest(trend_term="chuva", theme_key="chuva", force=True)

    assert request.force is True
