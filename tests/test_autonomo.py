"""Modo automático: aprovação sem humano, checagens, horário adaptativo e vigilância (só simuladores)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import httpx

from app.database import utcnow
from app.models import Post, PostStatus
from app.services.content_service import ContentService, set_status
from tests.conftest import CAPTION_OK, ExplodingMeta, make_content, new_post, reload_settings


def _auto(monkeypatch, **kw):
    return reload_settings(monkeypatch, REQUIRE_MANUAL_APPROVAL="false", AUTO_SCHEDULE="true", **kw)


def test_autonomous_post_is_approved_scheduled_and_published(db, monkeypatch):
    from app.services.publisher import Publisher

    s = _auto(monkeypatch)
    cs = make_content(settings=s)
    p = new_post(db, cs)
    assert p.status == PostStatus.APPROVED.value and p.approved_by == "automático"
    assert cs.auto_schedule(db) == 1
    db.refresh(p)
    assert p.status == PostStatus.SCHEDULED.value and p.scheduled_at > utcnow()
    p.scheduled_at = utcnow() - timedelta(minutes=1)
    db.commit()
    res = Publisher(meta=ExplodingMeta()).run_cycle()
    assert res["results"][0]["outcome"] == "simulated"


def test_invalid_caption_is_regenerated_instead_of_waiting(db, monkeypatch):
    s = _auto(monkeypatch)
    cs = make_content(["Curta.", CAPTION_OK], settings=s)
    p = new_post(db, cs, caption=None)
    assert p.status == PostStatus.APPROVED.value and p.caption.text.startswith("Encontrar")
    assert p.caption_attempts == 2
    retry_req = cs.captions.stub.requests[1]
    assert "recusada pela validação" in json.dumps(retry_req, ensure_ascii=False)


def test_caption_still_bad_after_retries_is_held(db, monkeypatch):
    s = _auto(monkeypatch, CAPTION_MAX_ATTEMPTS="2")
    p = new_post(db, make_content(["Curta."], settings=s), caption=None)
    assert p.status == PostStatus.NEEDS_REVIEW.value


def test_manual_mode_unchanged(db):
    assert new_post(db).status == PostStatus.PENDING_APPROVAL.value


def test_approve_pending_and_fill_missing_captions(db, monkeypatch):
    held = new_post(db)  # criada com aprovação manual ligada
    from app.services.content_service import PostInput

    draft = make_content().create_post(db, PostInput(template_key="autonomos", overrides={"title": "Sem legenda"},
                                                     generate_caption=False))
    assert draft.status == PostStatus.DRAFT.value
    s = _auto(monkeypatch)
    cs = make_content(settings=s)
    assert cs.approve_pending(db) == 1
    assert cs.fill_missing_captions(db) == 1
    db.refresh(held), db.refresh(draft)
    assert held.status == PostStatus.APPROVED.value and draft.status == PostStatus.APPROVED.value


def test_photo_flags():
    from app.services.image_search import photo_flags

    assert photo_flags("Woman fixing a roof", ["repair"]) == ["woman"]
    assert photo_flags("Telhado", ["Logotipo", "casa"]) == ["logotipo"]
    assert photo_flags("Roof repair work", ["roof"]) == []


def test_find_photo_skips_people(db, monkeypatch):
    from app.services.image_search import find_photo
    from tests.test_photos_trends import OV_RESULT, _transport

    s = _auto(monkeypatch)
    people = dict(OV_RESULT, id="ov-p", title="Man repairing roof")
    clean = dict(OV_RESULT, id="ov-c", title="Roof repair tiles")
    photo, _ = find_photo(db, "roof repair", None, s, _transport([], ov_results=[people, clean]))
    assert photo is not None and photo.provider_id == "ov-c"


def test_trend_post_autonomous_and_recheck_cancels(db, monkeypatch):
    from app.services import trend_content as tc
    from app.services.publisher import Publisher
    from app.services.state import set_state
    from app.services.trends_service import CACHE_KEY
    from tests.test_photos_trends import CAP, _content, _transport

    s = _auto(monkeypatch, TREND_USE_GROQ="false", POSTING_CANDIDATE_SLOTS="09:00,18:00")
    items = [{"term": "Chuva em Iporá", "news": ["Previsão indica chuva"], "geo": "BR-GO"}]
    res = tc.run_trend_auto(db, s, force=True, client=_transport([]), content=_content(CAP), items=items)
    post = db.get(Post, res["created"][0])
    assert post.status == PostStatus.APPROVED.value, post.review_notes
    from datetime import datetime as dt

    from app.timeutil import local_to_utc, utc_to_local

    day = utc_to_local(utcnow()).date() + timedelta(days=1)
    afternoon = local_to_utc(dt(day.year, day.month, day.day, 13, 0))  # 13:00 local: próximo candidato só às 15:00
    ContentService(s).auto_schedule(db, now=afternoon + timedelta(minutes=40))
    db.refresh(post)
    assert post.status == PostStatus.SCHEDULED.value
    assert post.scheduled_at <= afternoon + timedelta(minutes=50)  # assunto do momento sai logo
    set_state(db, CACHE_KEY, {"items": [{"term": "Chuva em Iporá", "news": ["Acidente grave durante a chuva"]}],
                              "fetched_at": utcnow().timestamp()})
    post.scheduled_at = utcnow() - timedelta(minutes=1)
    db.commit()
    out = Publisher(meta=ExplodingMeta()).run_cycle()
    assert out["results"][0]["outcome"] == "cancelled"
    db.refresh(post)
    assert post.status == PostStatus.CANCELLED.value


def test_ranked_slots_learning_then_exploit(monkeypatch):
    from app.services import timing

    s = _auto(monkeypatch, POSTING_CANDIDATE_SLOTS="09:00,12:30,19:30", TIMING_MIN_SAMPLES="2",
              TIMING_EXPLORE_RATE="0", MAX_POSTS_PER_DAY="1")
    st = {(9, 0): {"tries": 3, "measured": 2, "mean": 0.05}, (12, 30): {"tries": 0, "measured": 0, "mean": None},
          (19, 30): {"tries": 1, "measured": 1, "mean": 0.2}}
    assert timing.ranked_slots(st, s, date(2026, 11, 2))[0] == (12, 30)  # aprendizado: menos testado primeiro
    st[(12, 30)].update(tries=2, measured=2, mean=0.10)
    st[(19, 30)].update(tries=2, measured=2, mean=0.20)
    assert timing.ranked_slots(st, s, date(2026, 11, 2)) == [(19, 30), (12, 30), (9, 0)]


def test_next_free_slot_rotates_candidates(db, monkeypatch):
    from app.timeutil import utc_to_local

    s = _auto(monkeypatch, POSTING_CANDIDATE_SLOTS="09:00,19:30", MIN_MINUTES_BETWEEN_POSTS="60")
    cs = make_content(settings=s)
    for i in range(3):
        new_post(db, cs, overrides={"title": f"Título {i}"})
    assert cs.auto_schedule(db) == 3
    times = sorted(utc_to_local(p.scheduled_at) for p in db.query(Post).all())
    assert {t.strftime("%H:%M") for t in times} == {"09:00", "19:30"}  # usou os dois horários


def _hook(calls):
    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(204)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_watchdog_auto_pauses_after_failures(db, monkeypatch):
    from app.services.alerts import watchdog
    from app.services.state import is_paused

    s = _auto(monkeypatch, ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/1/abc", QUEUE_MIN_DAYS="0")
    cs = make_content(settings=s)
    for i in range(3):
        p = new_post(db, cs, overrides={"title": f"Falha {i}"})
        p.last_error = "code=190 token inválido"
        set_status(db, p, PostStatus.FAILED, "falha definitiva")
    db.commit()
    calls: list = []
    out = watchdog(db, s, client=_hook(calls))
    assert out.get("auto_paused") and is_paused(db)
    assert any("pausada automaticamente" in c["content"] for c in calls)
    n = len(calls)
    watchdog(db, s, client=_hook(calls))  # pausado: não repete avisos
    assert len(calls) == n


def test_watchdog_queue_and_review_alerts_are_deduplicated(db, monkeypatch):
    from app.services.alerts import watchdog

    s = _auto(monkeypatch, ALERT_WEBHOOK_URL="https://ntfy.sh/doova-teste")
    p = new_post(db, make_content(settings=s))
    set_status(db, p, PostStatus.NEEDS_REVIEW, "teste")
    p.review_notes = ["texto da arte: promessa de renda"]
    db.commit()
    calls: list = []

    def handler(req):
        calls.append(req.headers.get("Title"))
        return httpx.Response(200)
    cl = httpx.Client(transport=httpx.MockTransport(handler))
    assert watchdog(db, s, client=cl)["alerts"] == 2  # barrada + fila acabando
    assert watchdog(db, s, client=cl)["alerts"] == 0
    assert all(t and t.isascii() for t in calls)


def test_timing_and_alerts_endpoints(client, auth):
    r = client.get("/api/timing", headers=auth)
    assert r.status_code == 200 and r.json()["strategy"] == "adaptive" and r.json()["slots"]
    r = client.get("/api/alerts", headers=auth)
    assert r.status_code == 200 and r.json()["webhook_configured"] is False