from datetime import timedelta

import pytest

from app.database import SessionLocal, init_engine, utcnow
from app.models import Post, PostStatus
from app.services.content_service import ContentError, PostInput
from app.services.meta_service import MetaError
from app.services.publisher import Publisher
from app.services.state import acquire_lock, set_paused
from tests.conftest import (
    MANUAL_CAPTION,
    ExplodingMeta,
    FakeMeta,
    ig_timestamp,
    make_content,
    new_post,
    reload_settings,
    scheduled_post,
)


def _status(pid):
    with SessionLocal() as s:
        return s.get(Post, pid)


@pytest.fixture
def live(monkeypatch):
    return reload_settings(monkeypatch, DRY_RUN="false", META_IG_USER_ID="17841400000000000",
                           META_ACCESS_TOKEN="IGAAtesteFalso1234567890abcdef", PUBLIC_BASE_URL="https://doova.example.com")


# ----------------------------------------------------------- estados
def test_manual_caption_requires_approval(db):
    p = new_post(db)
    assert p.status == PostStatus.PENDING_APPROVAL.value
    assert p.variant.params["template"] == "autonomos" and p.artwork.jpg_bytes


def test_schedule_requires_future(db):
    cs = make_content()
    p = new_post(db, cs)
    cs.approve(db, p)
    from app.timeutil import utc_to_local

    with pytest.raises(ContentError):  # horário local sem fuso, 1 h no passado
        cs.schedule(db, p, utc_to_local(utcnow() - timedelta(hours=1)).replace(tzinfo=None))


def test_queue_survives_restart(db):
    from app.config import get_settings

    cs = make_content()
    p = new_post(db, cs)
    cs.approve(db, p)
    from app.timeutil import utc_to_local

    cs.schedule(db, p, utc_to_local(utcnow() + timedelta(hours=2)).replace(tzinfo=None))
    pid = p.id
    db.close()
    init_engine(get_settings().database_url)  # simula reinício do processo
    with SessionLocal() as s:
        assert s.get(Post, pid).status == PostStatus.SCHEDULED.value


# ----------------------------------------------------------- simulação
def test_dry_run_never_calls_meta(db):
    p = scheduled_post(db)
    res = Publisher(meta=ExplodingMeta()).run_cycle()
    assert res["results"][0]["outcome"] == "simulated"
    post = _status(p.id)
    assert post.status == PostStatus.SIMULATED.value and post.ig_media_id is None and post.dry_run


def test_one_post_per_cycle_and_min_interval(db):
    a = scheduled_post(db)
    b = scheduled_post(db, overrides={"title": "Outro título"})
    pub = Publisher(meta=ExplodingMeta())
    pub.run_cycle()
    pub.run_cycle()
    statuses = sorted([_status(a.id).status, _status(b.id).status])
    assert statuses == [PostStatus.SCHEDULED.value, PostStatus.SIMULATED.value]
    waiting = a if _status(a.id).status == PostStatus.SCHEDULED.value else b
    assert _status(waiting.id).next_attempt_at > utcnow() + timedelta(minutes=170)


def test_pause_blocks_publishing(db):
    p = scheduled_post(db)
    set_paused(db, True)
    db.commit()
    assert Publisher(meta=ExplodingMeta()).run_cycle() == {"paused": True}
    assert Publisher(meta=ExplodingMeta()).publish_now(p.id)["outcome"] == "paused"
    assert _status(p.id).status == PostStatus.SCHEDULED.value


def test_lock_held_by_other_instance(db):
    scheduled_post(db)
    assert acquire_lock("publisher", 600, owner="outra-instancia")
    assert Publisher(meta=ExplodingMeta()).run_cycle() == {"busy": True}


def test_stale_publishing_is_recovered(db):
    p = scheduled_post(db)
    p.status = PostStatus.PUBLISHING.value
    p.locked_at = utcnow() - timedelta(hours=1)
    db.commit()
    res = Publisher(meta=ExplodingMeta()).run_cycle()
    assert res["recovered"] == 1 and _status(p.id).status == PostStatus.SIMULATED.value


def test_preflight_blocks_invalid_caption(db):
    p = scheduled_post(db)
    p.caption.text = "Ganhe renda garantida com o Doova agora mesmo em Iporá."
    db.commit()
    res = Publisher(meta=ExplodingMeta()).run_cycle()
    assert res["results"][0]["outcome"] == "blocked"
    assert _status(p.id).status == PostStatus.NEEDS_REVIEW.value


def test_unapproved_post_cannot_publish_now(db):
    p = new_post(db)
    assert Publisher(meta=ExplodingMeta()).publish_now(p.id)["outcome"] == "skipped"


# ----------------------------------------------------------- duplicidade
def test_duplicate_content_blocked(db):
    cs = make_content()
    a = new_post(db, cs)
    b = new_post(db, cs)
    assert a.content_hash == b.content_hash
    cs.approve(db, a)
    cs.approve(db, b)
    assert a.status == PostStatus.APPROVED.value
    assert b.status == PostStatus.DUPLICATE_BLOCKED.value


def test_published_post_is_not_published_again(db, live):
    p = scheduled_post(db)
    meta = FakeMeta()
    pub = Publisher(live, meta=meta)
    assert pub.run_cycle()["results"][0]["outcome"] == "published"
    pub.run_cycle()
    assert pub.publish_now(p.id)["outcome"] == "skipped"
    assert [c for c in meta.calls if c[0] == "publish"] == [("publish", "cont1")]


# ----------------------------------------------------------- publicação real (Meta simulada)
def test_real_flow_container_then_publish(db, live):
    p = scheduled_post(db)
    meta = FakeMeta()
    res = Publisher(live, meta=meta).run_cycle()["results"][0]
    assert res["outcome"] == "published"
    post = _status(p.id)
    assert post.status == PostStatus.PUBLISHED.value and post.ig_media_id == "media_cont1"
    assert post.permalink.startswith("https://www.instagram.com/")
    create = next(c for c in meta.calls if c[0] == "create")
    db.expire_all()
    token = db.get(Post, p.id).artwork.public_token
    assert create[1] == f"https://doova.example.com/media/{token}.jpg"
    assert meta.last_caption == MANUAL_CAPTION


def test_container_in_progress_continues_next_cycle(db, live):
    p = scheduled_post(db)
    meta = FakeMeta()
    meta.container_states = ["IN_PROGRESS", "FINISHED"]
    pub = Publisher(live, meta=meta)
    assert pub.run_cycle()["results"][0]["outcome"] == "processing"
    assert _status(p.id).status == PostStatus.PROCESSING.value
    assert pub.run_cycle()["results"][0]["outcome"] == "published"


def test_ambiguous_publish_is_verified_not_repeated(db, live):
    p = scheduled_post(db)
    meta = FakeMeta()
    meta.publish_errors = [MetaError("timeout em media_publish", retryable=True, ambiguous=True)]
    pub = Publisher(live, meta=meta)
    assert pub.run_cycle()["results"][0]["outcome"] == "verifying"
    assert _status(p.id).status == PostStatus.VERIFYING.value
    # a publicação de fato aconteceu: aparece na conta
    meta.recent = [{"id": "m_real", "caption": meta.last_caption, "timestamp": ig_timestamp(utcnow())}]
    meta.container_states = ["PUBLISHED"]
    assert pub.run_cycle()["results"][0]["outcome"] == "published"
    assert _status(p.id).ig_media_id == "m_real"
    assert len([c for c in meta.calls if c[0] == "publish"]) == 1


def test_container_error_retries_then_fails(db, monkeypatch):
    s = reload_settings(monkeypatch, DRY_RUN="false", META_IG_USER_ID="1", META_ACCESS_TOKEN="IGAAx" * 6,
                        PUBLIC_BASE_URL="https://doova.example.com", PUBLISH_MAX_ATTEMPTS=2)
    p = scheduled_post(db)
    meta = FakeMeta()
    meta.container_states = ["ERROR"]
    pub = Publisher(s, meta=meta)
    assert pub.run_cycle()["results"][0]["outcome"] == "retry"
    post = _status(p.id)
    assert post.status == PostStatus.SCHEDULED.value and post.attempts == 1
    later = post.next_attempt_at + timedelta(seconds=1)
    assert pub.run_cycle(now=later)["results"][0]["outcome"] == "failed"
    assert _status(p.id).status == PostStatus.FAILED.value


def test_non_retryable_create_error_fails(db, live):
    p = scheduled_post(db)
    meta = FakeMeta()
    meta.create_errors = [MetaError("Graph API 400 (code=9004): imagem inválida", status=400, code=9004)]
    assert Publisher(live, meta=meta).run_cycle()["results"][0]["outcome"] == "failed"
    assert _status(p.id).status == PostStatus.FAILED.value


def test_real_mode_without_credentials_does_not_publish(db, monkeypatch):
    s = reload_settings(monkeypatch, DRY_RUN="false")
    p = scheduled_post(db)
    res = Publisher(s, meta=ExplodingMeta()).run_cycle()["results"][0]
    assert res["outcome"] == "not_configured"
    assert _status(p.id).status == PostStatus.SCHEDULED.value


# ----------------------------------------------------------- oportunidades
def test_opportunity_requires_verification(db):
    with pytest.raises(ContentError):
        make_content().create_post(db, PostInput(template_key="autonomos", content_kind="opportunity",
                                                 caption=MANUAL_CAPTION))


def test_unverified_job_text_in_art_requires_review(db):
    p = new_post(db, overrides={"title": "Vagas abertas para pedreiro"})
    assert p.status == PostStatus.NEEDS_REVIEW.value
    assert any("vaga" in n for n in p.review_notes)


def test_verified_opportunity_allowed(db):
    from app.models import VerifiedOpportunity

    opp = VerifiedOpportunity(title="Pintura de muro", description="Cliente real solicitou pintura de muro.",
                              source="contato telefônico registrado", verified_by="equipe")
    db.add(opp)
    db.commit()
    p = make_content().create_post(db, PostInput(
        template_key="contratantes", content_kind="opportunity", verified_opportunity_id=opp.id,
        overrides={"title": "Vaga aberta: pintura de muro em Iporá"},
        caption="Oportunidade verificada pela equipe do Doova: pintura de muro em Iporá. Detalhes no perfil."))
    assert p.status == PostStatus.PENDING_APPROVAL.value