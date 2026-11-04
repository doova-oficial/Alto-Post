import json
from datetime import datetime, timedelta, timezone

import pytest

from app.database import utcnow
from app.models import VideoAsset, VideoMetricSnapshot
from app.services import video_analytics_service as van
from tests.conftest import FakeMeta, ig_timestamp, reload_settings


def _video_asset(db, *, provider_id="pv1", theme_key="autonomos", trend_term="termo em alta",
                 transition="fade", duration_seconds=12.0, creative_variant="clean_center",
                 generated_image_count=1, published=False, media_id=None, published_at: datetime | None = None,
                 cloudinary_public_id="doova/videos/x"):
    metadata = {
        "transition": transition, "duration_seconds": duration_seconds, "duration_requested": duration_seconds,
        "creative_variant": creative_variant, "generated_image_count": generated_image_count,
        "cloudinary_public_id": cloudinary_public_id, "segments": [], "texts": [],
        "context_image": {"provider": "doova", "kind": "brand_placeholder"},
    }
    if published:
        metadata["instagram_media_id"] = media_id or f"media_{provider_id}"
        metadata["instagram_permalink"] = f"https://www.instagram.com/p/{provider_id}/"
        if published_at is not None:
            metadata["instagram_published_at"] = ig_timestamp(published_at)
    asset = VideoAsset(provider="pexels", provider_id=provider_id, query="consulta", title="titulo", creator="autor",
                       source="Pexels", license="Pexels License", landing_url="https://pexels.com/x",
                       download_url="https://pexels.com/x.mp4", width=1080, height=1920,
                       duration_seconds=duration_seconds,
                       video_url="https://res.cloudinary.com/demo/video/upload/x.mp4",
                       subtitle_text="legenda de exemplo", hook="gancho de exemplo", trend_term=trend_term,
                       theme_key=theme_key, metadata_json=metadata)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


# --------------------------------------------------------------- unidades
def test_duration_bucket_thresholds():
    assert van._duration_bucket(8) == "curto (≤10s)"
    assert van._duration_bucket(15) == "médio (11-20s)"
    assert van._duration_bucket(45) == "longo (>20s)"
    assert van._duration_bucket(None) == "desconhecida"


def test_compute_video_rates_includes_watch_through():
    snap = {"reach": 200, "likes": 10, "comments": 2, "shares": 1, "saved": 3, "total_interactions": None,
            "views": 300, "avg_watch_time_ms": 6000, "duration_seconds": 12.0}
    r = van.compute_video_rates(snap)
    assert r["engagement_rate"] == pytest.approx(16 / 200)
    assert r["watch_through_rate"] == pytest.approx(6.0 / 12.0)


def test_compute_video_rates_missing_watch_time_is_none():
    r = van.compute_video_rates({"reach": 100, "avg_watch_time_ms": None, "duration_seconds": 12.0})
    assert r["watch_through_rate"] is None


# -------------------------------------------------------------- coleta
def test_collect_video_metrics_skips_unpublished_and_recent(db, monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    draft = _video_asset(db, provider_id="draft1", published=False)
    published = _video_asset(db, provider_id="pub1", published=True,
                             published_at=utcnow() - timedelta(hours=80))
    meta = FakeMeta()
    meta.insights = {published.metadata_json["instagram_media_id"]:
                     ({"reach": 500, "likes": 20, "comments": 4, "shares": 2, "saved": 6,
                       "total_interactions": 32, "views": 900, "ig_reels_avg_watch_time": 7000}, [])}
    res = van.collect_video_metrics(db, meta=meta, settings=s)
    assert res["collected"] == 1  # o rascunho (draft) não tem instagram_media_id; não entra na coleta
    snaps = db.query(VideoMetricSnapshot).all()
    assert len(snaps) == 1 and snaps[0].video_id == published.id
    assert snaps[0].avg_watch_time_ms == 7000
    # uma segunda coleta imediata não duplica (intervalo mínimo)
    res2 = van.collect_video_metrics(db, meta=meta, settings=s)
    assert res2["skipped_recent"] == 1


# ------------------------------------------------------- dataset e dimensões
def test_build_dataset_excludes_too_young_and_unpublished(db, monkeypatch):
    s = reload_settings(monkeypatch)
    _video_asset(db, provider_id="draft2", published=False)
    old = _video_asset(db, provider_id="old1", published=True, published_at=utcnow() - timedelta(hours=80))
    young = _video_asset(db, provider_id="young1", published=True, published_at=utcnow() - timedelta(hours=2))
    for asset, hours in ((old, 80), (young, 2)):
        snap = VideoMetricSnapshot(video_id=asset.id, collected_at=utcnow(), age_hours=hours,
                                   reach=400, likes=20, comments=2, shares=1, saved=3, total_interactions=26)
        db.add(snap)
    db.commit()
    ds = van.build_video_dataset(db, s)
    assert [r["video_id"] for r in ds["rows"]] == [old.id]
    assert ds["excluded_too_young"] == [young.id]
    assert ds["generated_total"] == 3  # inclui o rascunho não publicado
    assert ds["published_total"] == 2
    row = ds["rows"][0]
    assert row["transition"] == "fade"
    assert row["duration_bucket"] == "médio (11-20s)"  # duração padrão do helper de teste é 12s
    assert row["engagement_rate"] == pytest.approx(26 / 400)


def test_daily_video_count_bucket_counts_same_local_day(db, monkeypatch):
    s = reload_settings(monkeypatch)
    # 10:00 e 13:00 UTC caem no mesmo dia em America/Sao_Paulo (UTC-3); o terceiro, no dia seguinte.
    day1a = _video_asset(db, provider_id="day1a", published=True,
                         published_at=datetime(2026, 1, 10, 13, 0, tzinfo=timezone.utc).replace(tzinfo=None))
    day1b = _video_asset(db, provider_id="day1b", published=True,
                         published_at=datetime(2026, 1, 10, 18, 0, tzinfo=timezone.utc).replace(tzinfo=None))
    day2 = _video_asset(db, provider_id="day2a", published=True,
                        published_at=datetime(2026, 1, 11, 18, 0, tzinfo=timezone.utc).replace(tzinfo=None))
    for asset in (day1a, day1b, day2):
        db.add(VideoMetricSnapshot(video_id=asset.id, collected_at=utcnow(), age_hours=80,
                                   reach=300, likes=10, comments=1, shares=1, saved=1, total_interactions=13))
    db.commit()
    ds = van.build_video_dataset(db, s)
    by_id = {r["video_id"]: r for r in ds["rows"]}
    assert by_id[day1a.id]["daily_video_count_bucket"] == "2"
    assert by_id[day1b.id]["daily_video_count_bucket"] == "2"
    assert by_id[day2.id]["daily_video_count_bucket"] == "1"


def test_compare_video_by_transition(db, monkeypatch):
    s = reload_settings(monkeypatch)
    rows = []
    for transition, vals in (("fade", [0.20, 0.22, 0.21, 0.19, 0.23, 0.20]),
                             ("wipeleft", [0.03, 0.02, 0.04, 0.03, 0.02, 0.03])):
        for v in vals:
            rows.append({"transition": transition, "engagement_rate": v, "reach": 100, "snapshot_age_hours": 80})
    out = van.compare_video(rows, "transition", settings=s)
    assert out["test"]["performed"] and out["test"]["significant"]
    assert out["dimension_label"] == "Transição entre cenas"


def test_write_video_reports(db, monkeypatch, tmp_path):
    s = reload_settings(monkeypatch)
    asset = _video_asset(db, provider_id="rep1", published=True, published_at=utcnow() - timedelta(hours=80))
    db.add(VideoMetricSnapshot(video_id=asset.id, collected_at=utcnow(), age_hours=80, reach=400, likes=20,
                               comments=2, shares=1, saved=3, total_interactions=26))
    db.commit()
    files = van.write_reports(db, tmp_path / "rel", s)
    summ = json.loads(open(files["summary_json"], encoding="utf-8").read())
    assert summ["videos_analyzed"] == 1
    assert summ["limitations"]
    assert open(files["videos_csv"], encoding="utf-8").read().strip()
