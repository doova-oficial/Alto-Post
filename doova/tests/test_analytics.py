import json
from datetime import timedelta

import httpx
import pytest

from app.database import utcnow
from app.models import MetricSnapshot, PostStatus
from app.services import analytics_service as an
from app.services.meta_service import MetaError, MetaService
from tests.conftest import FakeMeta, new_post, reload_settings


def test_rates_have_explicit_denominators():
    snap = {"reach": 200, "likes": 10, "comments": 2, "shares": 3, "saved": 5, "total_interactions": None,
            "views": 400, "profile_visits": 4, "follows": 1}
    r = an.compute_rates(snap)
    assert r["engagement_rate"] == pytest.approx(20 / 200)  # fallback: soma das interações
    assert r["save_rate"] == pytest.approx(5 / 200)
    assert r["views_per_reach"] == pytest.approx(2.0)
    assert "/ alcance" in an.RATE_DEFS["engagement_rate"][2]


def test_missing_metrics_are_none_not_zero():
    r = an.compute_rates({"reach": 0, "likes": 3})
    assert r["like_rate"] is None and r["engagement_rate"] is None
    r = an.compute_rates({"reach": 100, "likes": None})
    assert r["like_rate"] is None


def test_total_interactions_preferred():
    assert an.interactions_of({"total_interactions": 9, "likes": 1, "comments": 1, "shares": 1, "saved": 1}) == (
        9, "total_interactions")


def test_age_normalized_snapshot():
    mk = lambda h: MetricSnapshot(age_hours=h)  # noqa: E731
    assert an.analysis_snapshot([mk(5), mk(30)], 72) is None  # jovem demais
    assert an.analysis_snapshot([mk(50), mk(70), mk(120)], 72).age_hours == 70


def _rows(groups):
    rows = []
    for g, vals in groups.items():
        for v in vals:
            rows.append({"template": g, "engagement_rate": v, "reach": 100, "snapshot_age_hours": 72})
    return rows


def test_compare_refuses_test_with_small_sample():
    out = an.compare(_rows({"a": [0.1, 0.2], "b": [0.05, 0.04]}), "template")
    assert out["test"]["performed"] is False
    assert "insuficiente" in out["interpretation"]


def test_compare_detects_clear_difference():
    out = an.compare(_rows({"a": [0.10, 0.11, 0.12, 0.10, 0.13, 0.11],
                            "b": [0.02, 0.03, 0.02, 0.01, 0.03, 0.02]}), "template")
    assert out["test"]["performed"] and out["test"]["significant"]
    assert out["test"]["p_value"] < 0.01
    assert "não prova de causalidade" in out["interpretation"] or "causalidade" in out["interpretation"]


def test_compare_no_difference():
    vals = [0.1, 0.2, 0.15, 0.12, 0.18, 0.14]
    out = an.compare(_rows({"a": vals, "b": list(reversed(vals))}), "template")
    assert out["test"]["performed"] and not out["test"]["significant"]


def test_block_adjustment_removes_theme_effect():
    rows = []
    for block, base in (("tema1", 0.20), ("tema2", 0.02)):
        rows.append({"block": block, "factor_x": "a", "engagement_rate": base + 0.01, "reach": 1, "snapshot_age_hours": 72})
        rows.append({"block": block, "factor_x": "b", "engagement_rate": base - 0.01, "reach": 1, "snapshot_age_hours": 72})
    adj = an.adjust_by_block(rows, "engagement_rate")
    assert {round(r["engagement_rate_adj"], 6) for r in adj} == {0.01, -0.01}


def _publish(db, post, hours_ago):
    post.status = PostStatus.PUBLISHED.value
    post.published_at = utcnow() - timedelta(hours=hours_ago)
    post.ig_media_id = f"m_{post.id}"
    db.commit()


def test_collect_and_dataset_and_reports(db, monkeypatch, tmp_path):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "x" * 30)
    old = new_post(db)
    young = new_post(db, overrides={"title": "Outro"})
    _publish(db, old, 80)
    _publish(db, young, 5)
    meta = FakeMeta()
    meta.insights = {old.ig_media_id: ({"reach": 300, "likes": 15, "comments": 3, "shares": 2, "saved": 4,
                                        "total_interactions": 24, "views": 500}, ["profile_activity"]),
                     young.ig_media_id: ({"reach": 50}, [])}
    res = an.collect_metrics(db, meta=meta, settings=s)
    assert res["collected"] == 2
    assert an.collect_metrics(db, meta=meta, settings=s)["skipped_recent"] == 2  # sem coleta duplicada
    ds = an.build_dataset(db, s)
    assert [r["post_id"] for r in ds["rows"]] == [old.id]
    assert ds["excluded_too_young"] == [young.id]
    row = ds["rows"][0]
    assert row["engagement_rate"] == pytest.approx(24 / 300)
    assert row["profile_activity"] is None and "profile_activity" in row["unavailable_metrics"]
    assert row["variant_id"] == old.variant_id and row["artwork_id"] == old.artwork_id
    files = an.write_reports(db, tmp_path / "rel", s)
    summary = json.loads(open(files["summary_json"], encoding="utf-8").read())
    assert summary["posts_analyzed"] == 1 and summary["limitations"]
    assert open(files["posts_csv"], encoding="utf-8").read().startswith(sorted(row)[0])


def test_meta_insights_parsing_with_unsupported_metric(monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAAsecreto" + "z" * 25)
    token = s.meta_access_token.get_secret_value()

    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == f"Bearer {token}"
        metrics = req.url.params["metric"].split(",")
        if "profile_activity" in metrics:
            return httpx.Response(400, json={"error": {"code": 100, "message": f"invalid metric token={token}"}})
        data = [{"name": m, "period": "lifetime", "values": [{"value": 7}]} for m in metrics if m != "follows"]
        return httpx.Response(200, json={"data": data})

    m = MetaService(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    values, unavailable = m.media_insights("123")
    assert values["reach"] == 7 and "profile_activity" not in values
    assert set(unavailable) == {"profile_activity", "follows"}
    with pytest.raises(MetaError) as e:
        m._insights("123", ["profile_activity"])
    assert token not in str(e.value)


def test_container_requires_https(monkeypatch):
    s = reload_settings(monkeypatch, META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAA" + "y" * 30)
    with pytest.raises(MetaError):
        MetaService(s, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))) \
            .create_image_container("http://inseguro/img.jpg", "legenda")