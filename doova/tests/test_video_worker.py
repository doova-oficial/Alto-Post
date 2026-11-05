import hashlib

from app.models import VideoAsset, VideoRenderJob
from app.services.video_service import VideoCandidate
from app.services.video_worker import enqueue_video_render
from tests.conftest import reload_settings

WORKER_TOKEN = "video-worker-test-token-0123456789abcdef"


def _asset(db):
    asset = VideoAsset(
        provider="pexels",
        provider_id="video-worker-test",
        query="chuva",
        title="Chuva",
        creator="Criador",
        source="Pexels",
        license="Pexels License",
        landing_url="https://www.pexels.com/video/1/",
        download_url="https://cdn.example/video.mp4",
        width=720,
        height=1280,
        duration_seconds=10,
        hook="Vai chover?",
        trend_term="chuva",
        theme_key="chuva",
        metadata_json={"texts": [{"start": 0, "end": 10, "text": "Encontre ajuda no Doova."}]},
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _worker_headers():
    return {"X-Video-Worker-Token": WORKER_TOKEN}


def _reaction_candidate():
    return VideoCandidate(
        provider="pexels",
        provider_id="reaction-clip-1",
        title="Person reacting to phone",
        creator="Pexels creator",
        landing_url="https://www.pexels.com/video/reaction/",
        download_url="https://cdn.example/reaction.mp4",
        width=1080,
        height=1920,
        duration=12,
        query="person holding smartphone surprised reaction",
    )


def test_video_worker_authentication_and_empty_queue(client, monkeypatch):
    reload_settings(monkeypatch, VIDEO_RENDER_MODE="android", VIDEO_WORKER_TOKEN=WORKER_TOKEN)

    unauthorized = client.post("/api/video-worker/jobs/claim", json={"worker_id": "phone-1"})
    assert unauthorized.status_code == 401
    empty = client.post("/api/video-worker/jobs/claim", headers=_worker_headers(), json={"worker_id": "phone-1"})
    assert empty.status_code == 200 and empty.json()["job"] is None


def test_video_worker_claim_heartbeat_and_complete(client, db, monkeypatch):
    from app.routes import video_worker

    reload_settings(monkeypatch, VIDEO_RENDER_MODE="android", VIDEO_WORKER_TOKEN=WORKER_TOKEN)
    monkeypatch.setattr(
        video_worker, "_elevenlabs_audio_with_timestamps",
        lambda text, settings, timeout: (b"ID3 audio", [{"text": "Encontre", "start": 0.0, "end": 0.5}]),
    )
    monkeypatch.setattr(video_worker, "search_videos", lambda query, settings: [_reaction_candidate()])
    asset = _asset(db)
    queued = enqueue_video_render(db, asset)
    claimed = client.post("/api/video-worker/jobs/claim", headers=_worker_headers(),
                          json={"worker_id": "phone-1"})
    assert claimed.status_code == 200
    job = claimed.json()["job"]
    assert job["id"] == queued.id and job["video_id"] == asset.id
    assert job["source"]["url"] == asset.download_url
    assert job["render"]["subtitles"] == asset.metadata_json["texts"]
    assert job["render"]["narration_audio_base64"] == "SUQzIGF1ZGlv"
    assert job["render"]["narration_words"] == [{"text": "Encontre", "start": 0.0, "end": 0.5}]
    assert job["reaction_source"]["url"] == "https://cdn.example/reaction.mp4"
    assert job["reaction_source"]["creator"] == "Pexels creator"

    heartbeat = client.post(f"/api/video-worker/jobs/{job['id']}/heartbeat",
                            headers=_worker_headers(), json={"worker_id": "phone-1"})
    assert heartbeat.status_code == 200 and heartbeat.json()["status"] == "processing"

    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
    digest = hashlib.sha256(mp4).hexdigest()

    def upload(path, asset_key, settings, overwrite=False):
        assert path.read_bytes() == mp4 and overwrite is True
        assert asset_key == f"android-{job['id']}"
        return "https://res.cloudinary.com/test/video/upload/output.mp4", "doova/videos/output"

    monkeypatch.setattr(video_worker, "_upload_to_cloudinary", upload)
    completed = client.post(
        f"/api/video-worker/jobs/{job['id']}/complete",
        headers={**_worker_headers(), "X-Worker-Id": "phone-1", "X-Content-SHA256": digest,
                 "Content-Type": "video/mp4"},
        content=mp4,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "ready"
    db.refresh(asset)
    saved_job = db.get(VideoRenderJob, job["id"])
    db.refresh(saved_job)
    assert asset.video_url == completed.json()["video_url"]
    assert asset.metadata_json["android_renderer"] == "media3"
    assert saved_job.status == "ready" and saved_job.lease_owner is None


def test_video_worker_rejects_stale_owner_and_bad_mp4(client, db, monkeypatch):
    from app.routes import video_worker

    reload_settings(monkeypatch, VIDEO_RENDER_MODE="android", VIDEO_WORKER_TOKEN=WORKER_TOKEN)
    monkeypatch.setattr(
        video_worker, "_elevenlabs_audio_with_timestamps",
        lambda text, settings, timeout: (b"ID3 audio", [{"text": "Encontre", "start": 0.0, "end": 0.5}]),
    )
    monkeypatch.setattr(video_worker, "search_videos", lambda query, settings: [_reaction_candidate()])
    asset = _asset(db)
    queued = enqueue_video_render(db, asset)
    claim = client.post("/api/video-worker/jobs/claim", headers=_worker_headers(),
                        json={"worker_id": "phone-1"}).json()["job"]
    assert claim["id"] == queued.id

    stale = client.post(f"/api/video-worker/jobs/{claim['id']}/heartbeat", headers=_worker_headers(),
                        json={"worker_id": "phone-2"})
    assert stale.status_code == 409
    bad = b"not an mp4"
    rejected = client.post(
        f"/api/video-worker/jobs/{claim['id']}/complete",
        headers={**_worker_headers(), "X-Worker-Id": "phone-1",
                 "X-Content-SHA256": hashlib.sha256(bad).hexdigest(), "Content-Type": "video/mp4"},
        content=bad,
    )
    assert rejected.status_code == 422
