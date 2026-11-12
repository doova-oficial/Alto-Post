import pytest

from app.database import utcnow
from app.models import Post, PostStatus, VideoAsset, VideoMetricSnapshot
from app.services import video_service as vs
from tests.conftest import FakeMeta, reload_settings, scheduled_post


def _asset(db, *, provider_id="pv1", video_url="https://res.cloudinary.com/demo/video/upload/x.mp4",
          cloudinary_public_id="doova/videos/x", published=False):
    metadata = {"transition": "fade", "duration_seconds": 12.0, "creative_variant": "clean_center",
               "generated_image_count": 1, "cloudinary_public_id": cloudinary_public_id,
               "context_image": {"provider": "doova", "kind": "brand_placeholder"}}
    if published:
        metadata["instagram_media_id"] = f"media_{provider_id}"
        metadata["instagram_permalink"] = f"https://www.instagram.com/p/{provider_id}/"
    asset = VideoAsset(provider="pexels", provider_id=provider_id, query="q", title="t", creator="c",
                       source="Pexels", license="Pexels License", landing_url="https://pexels.com/x",
                       download_url="https://pexels.com/x.mp4", width=1080, height=1920, duration_seconds=12.0,
                       video_url=video_url, subtitle_text="legenda", hook="gancho", trend_term="termo",
                       theme_key="autonomos", metadata_json=metadata)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


# ------------------------------------------------------------- publicação
def test_publish_reel_happy_path(db, monkeypatch):
    s = reload_settings(monkeypatch, DRY_RUN="false", META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db)
    meta = FakeMeta()
    result = vs.publish_reel(db, asset, s, meta=meta)
    assert result["status"] == "published"
    assert "#doova" in meta.last_caption
    assert "#trabalhoautonomo" in meta.last_caption
    assert "#empreendedorismo" in meta.last_caption
    db.refresh(asset)
    assert asset.metadata_json["instagram_media_id"] == result["media_id"]
    assert ("create_reel", asset.video_url) in meta.calls


def test_publish_reel_adds_hashtags_to_caption_override_without_duplicates(db, monkeypatch):
    s = reload_settings(monkeypatch, DRY_RUN="false", META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db)
    meta = FakeMeta()

    vs.publish_reel(db, asset, s, meta=meta, caption_override="Legenda existente #Doova")

    assert meta.last_caption == "Legenda existente #Doova\n\n#trabalhoautonomo #empreendedorismo"


def test_publish_reel_already_published_is_idempotent(db, monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db, published=True)
    meta = FakeMeta()
    result = vs.publish_reel(db, asset, s, meta=meta)
    assert result["status"] == "already_published"
    assert meta.calls == []  # não chama a Meta de novo


def test_publish_reel_without_render_raises(db, monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db, video_url=None)
    with pytest.raises(vs.VideoPublishError):
        vs.publish_reel(db, asset, s, meta=FakeMeta())


def test_publish_reel_without_meta_configured_raises(db, monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="", META_ACCESS_TOKEN="")
    asset = _asset(db)
    with pytest.raises(vs.VideoPublishError):
        vs.publish_reel(db, asset, s, meta=FakeMeta())


def test_publish_reel_dry_run_simulates(db, monkeypatch):
    s = reload_settings(monkeypatch, DRY_RUN="true", META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db)
    result = vs.publish_reel(db, asset, s, meta=FakeMeta())
    assert result["status"] == "simulated"
    db.refresh(asset)
    assert "instagram_media_id" not in (asset.metadata_json or {})


# -------------------------------------------------------------- exclusão
def test_delete_video_unpublished_removes_row_and_cloudinary(db, monkeypatch):
    s = reload_settings(monkeypatch, CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="k",
                        CLOUDINARY_API_SECRET="s")
    asset = _asset(db)
    destroyed = {}
    monkeypatch.setattr(vs, "_destroy_cloudinary", lambda public_id, settings: destroyed.update(id=public_id))
    video_id = asset.id
    result = vs.delete_video(db, asset, s)
    assert result["deleted"] is True and result["cloudinary_deleted"] is True
    assert destroyed["id"] == "doova/videos/x"
    assert db.get(VideoAsset, video_id) is None


def test_delete_video_published_requires_force(db, monkeypatch):
    s = reload_settings(monkeypatch)
    asset = _asset(db, published=True)
    with pytest.raises(vs.VideoDeleteError):
        vs.delete_video(db, asset, s)
    assert db.get(VideoAsset, asset.id) is not None  # nada foi apagado


def test_delete_video_published_with_force_deletes_locally(db, monkeypatch):
    s = reload_settings(monkeypatch, CLOUDINARY_CLOUD_NAME="demo", CLOUDINARY_API_KEY="k",
                        CLOUDINARY_API_SECRET="s")
    asset = _asset(db, published=True)
    monkeypatch.setattr(vs, "_destroy_cloudinary", lambda public_id, settings: None)
    result = vs.delete_video(db, asset, s, force=True)
    assert result["deleted"] is True and result["was_published_on_instagram"] is True
    assert db.get(VideoAsset, asset.id) is None


def test_delete_video_also_removes_its_metric_snapshots(db, monkeypatch):
    s = reload_settings(monkeypatch)
    asset = _asset(db, cloudinary_public_id=None)
    db.add(VideoMetricSnapshot(video_id=asset.id, collected_at=utcnow(), age_hours=10, reach=100))
    db.commit()
    vs.delete_video(db, asset, s)
    assert db.query(VideoMetricSnapshot).filter_by(video_id=asset.id).count() == 0


# ---------------------------------------------------------------- job automático
def test_unpublished_rendered_videos_filters_correctly(db):
    rendered = _asset(db, provider_id="r1")
    _asset(db, provider_id="r2", published=True)
    _asset(db, provider_id="r3", video_url=None)
    pending = vs.unpublished_rendered_videos(db)
    assert [a.id for a in pending] == [rendered.id]


def test_job_video_publish_respects_auto_publish_flag(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    from app.services.scheduler import job_video_publish

    assert job_video_publish()["skipped"] == "VIDEO_AUTO_PUBLISH=false"


def test_job_video_publish_publishes_pending_reels(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="true", DRY_RUN="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    asset = _asset(db, provider_id="auto1")
    meta = FakeMeta()
    monkeypatch.setattr("app.services.meta_service.MetaService", lambda *a, **kw: meta)
    from app.services.scheduler import job_video_publish

    res = job_video_publish()
    assert asset.id in res["published"]
    db.refresh(asset)
    assert asset.metadata_json["instagram_media_id"]


def test_job_video_publish_spaces_reels_and_defers_the_next_video(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="true", DRY_RUN="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    first = _asset(db, provider_id="spaced-1")
    second = _asset(db, provider_id="spaced-2")
    meta = FakeMeta()
    monkeypatch.setattr("app.services.meta_service.MetaService", lambda *a, **kw: meta)
    from app.services.scheduler import job_video_publish

    result = job_video_publish()

    assert result["published"] == [first.id]
    assert second.id in result["deferred"]


def test_job_video_publish_works_with_facebook_without_instagram_credentials(db, monkeypatch):
    reload_settings(
        monkeypatch,
        VIDEO_AUTO_PUBLISH="true",
        DRY_RUN="false",
        META_IG_USER_ID="",
        META_ACCESS_TOKEN="",
        FACEBOOK_PAGE_ID="page-123",
        FACEBOOK_PAGE_ACCESS_TOKEN="page-token",
    )
    asset = _asset(db, provider_id="facebook-only")

    class FacebookPublisher:
        def __init__(self, settings):
            assert settings.facebook_configured

        def publish(self, video_url, caption, metadata):
            assert video_url == asset.video_url
            assert "#doova" in caption
            return {"status": "published", "video_id": "fb-video-1",
                    "permalink": "https://facebook.com/reel/fb-video-1"}

        def close(self):
            pass

    monkeypatch.setattr(
        "app.services.social_video_publishers.FacebookReelPublisher", FacebookPublisher,
    )
    from app.services.scheduler import job_video_publish

    result = job_video_publish()

    assert result["targets"] == ["facebook"]
    assert result["published"] == [asset.id]
    db.refresh(asset)
    assert asset.metadata_json["facebook_status"] == "published"
    assert asset.metadata_json["facebook_video_id"] == "fb-video-1"
    assert not asset.metadata_json.get("instagram_media_id")


def test_video_publish_endpoint_allows_selecting_a_configured_destination(client, db, monkeypatch, auth):
    reload_settings(
        monkeypatch,
        DRY_RUN="true",
        META_IG_USER_ID="",
        META_ACCESS_TOKEN="",
        FACEBOOK_PAGE_ID="page-123",
        FACEBOOK_PAGE_ACCESS_TOKEN="page-token",
    )
    asset = _asset(db, provider_id="manual-facebook")

    response = client.post(
        f"/api/videos/{asset.id}/publish",
        headers=auth,
        json={"platforms": ["facebook"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "simulated",
        "platforms": {"facebook": {"status": "simulated", "message": "DRY_RUN=true"}},
    }


def test_linked_reel_waits_for_post_schedule_and_uses_post_caption(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="true", DRY_RUN="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    post = scheduled_post(db)
    asset = _asset(db, provider_id="linked-auto")
    post.video_asset_id = asset.id
    post.scheduled_at = utcnow().replace(year=utcnow().year + 1)
    db.commit()
    meta = FakeMeta()
    monkeypatch.setattr("app.services.meta_service.MetaService", lambda *a, **kw: meta)

    from app.services.scheduler import job_video_publish

    result = job_video_publish()
    assert asset.id in result["deferred"]
    assert meta.calls == []
    assert db.get(Post, post.id).status == PostStatus.SCHEDULED.value


def test_linked_reel_waits_while_android_worker_is_offline(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="true", DRY_RUN="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    post = scheduled_post(db)
    asset = _asset(db, provider_id="linked-offline", video_url=None)
    post.video_asset_id = asset.id
    db.commit()
    from app.services.scheduler import job_video_publish
    from app.services.publisher import Publisher

    result = job_video_publish()
    image_result = Publisher(meta=FakeMeta()).run_cycle()

    assert result["eligible"] == 0 and result["published"] == [] and result["errors"] == []
    assert image_result["results"] == []
    assert db.get(Post, post.id).status == PostStatus.SCHEDULED.value


def test_linked_reel_publishes_as_post_not_photo_and_syncs_status(db, monkeypatch):
    reload_settings(monkeypatch, VIDEO_AUTO_PUBLISH="true", DRY_RUN="false", META_IG_USER_ID="1",
                    META_ACCESS_TOKEN="IGAA" + "x" * 30)
    post = scheduled_post(db)
    asset = _asset(db, provider_id="linked-ready")
    post.video_asset_id = asset.id
    db.commit()
    meta = FakeMeta()
    monkeypatch.setattr("app.services.meta_service.MetaService", lambda *a, **kw: meta)

    from app.services.scheduler import job_video_publish
    from app.services.publisher import Publisher

    image_cycle = Publisher(meta=meta).run_cycle()
    assert image_cycle["results"] == []
    result = job_video_publish()

    assert asset.id in result["published"]
    assert meta.last_caption == f"{post.caption.text}\n\n#doova #trabalhoautonomo #empreendedorismo"
    db.refresh(post)
    assert post.status == PostStatus.PUBLISHED.value
    assert post.ig_media_id and post.permalink
