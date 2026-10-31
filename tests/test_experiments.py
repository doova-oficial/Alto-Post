from datetime import date, timedelta

import pytest

from app.database import utcnow
from app.models import Post, PostStatus
from app.services import analytics_service as an
from app.services.content_service import ContentError
from app.services.experiment_service import ExperimentService
from tests.conftest import make_content


def _svc():
    return ExperimentService(make_content())


def test_ab_experiment_holds_other_params_constant(db):
    svc = _svc()
    exp = svc.create(db, name="posicao_titulo", factors={"title_position": ["top", "bottom"]},
                     theme_keys=["convite_prestadores", "convite_contratantes"])
    assert exp.design == "ab"
    posts = svc.generate_posts(db, exp)
    assert len(posts) == 4
    for block in exp.theme_keys:
        bp = [p for p in posts if p.theme_key == block]
        a, b = (p.variant.params for p in bp)
        diff = {k for k in a if a[k] != b[k]}
        assert diff == {"title_position"}
        assert {p.variant.factor_values["title_position"] for p in bp} == {"top", "bottom"}
        assert bp[0].caption_id == bp[1].caption_id  # mesma legenda dentro do bloco
        assert bp[0].content_hash != bp[1].content_hash
    for p in posts:  # rastreabilidade arte ↔ legenda ↔ campanha ↔ experimento
        assert p.experiment_id == exp.id and p.variant.experiment_id == exp.id
        assert p.artwork.variant_id == p.variant_id and p.campaign.slug == "exp-posicao-titulo"


def test_factorial_and_limits(db):
    svc = _svc()
    exp = svc.create(db, name="fatorial", factors={"title_position": ["top", "bottom"], "shapes": [True, False]},
                     theme_keys=["beneficio_perto"])
    assert exp.design == "factorial"
    plan = svc.plan(exp)
    assert plan["posts_total"] == 4 and plan["notes"]  # alerta de amostra pequena
    with pytest.raises(ContentError):
        svc.create(db, name="grande", factors={"title_position": ["top", "center", "bottom"],
                                               "background": ["gradient", "solid", "light"]},
                   theme_keys=["beneficio_perto"])
    with pytest.raises(ContentError):
        svc.create(db, name="invalido", factors={"cor_magica": ["a", "b"]}, theme_keys=["beneficio_perto"])
    with pytest.raises(ContentError):
        svc.create(db, name="umnivel", factors={"shapes": [True]}, theme_keys=["beneficio_perto"])


def test_text_amount_factor(db):
    svc = _svc()
    exp = svc.create(db, name="texto", factors={"text_amount": ["curto", "longo"]}, theme_keys=["beneficio_perto"])
    posts = svc.generate_posts(db, exp)
    by = {p.variant.factor_values["text_amount"]: p.variant.params for p in posts}
    assert by["curto"]["items"] == [] and by["curto"]["subtitle"] is None
    assert len(by["longo"]["items"]) == 3


def test_schedule_and_results(db):
    svc = _svc()
    exp = svc.create(db, name="agenda", factors={"title_position": ["top", "bottom"]}, theme_keys=["dois_lados"])
    posts = svc.generate_posts(db, exp)
    with pytest.raises(ContentError):
        svc.schedule(db, exp, date.today() + timedelta(days=2))  # ainda não aprovadas
    for p in posts:
        svc.content.approve(db, p)
    plan = svc.schedule(db, exp, date.today() + timedelta(days=2), slot="19:00")["scheduled"]
    assert len(plan) == 2 and all(x["scheduled_local"][11:16] == "19:00" for x in plan)
    assert len({x["scheduled_local"][:10] for x in plan}) == 2
    for i, p in enumerate(posts):
        p.status = PostStatus.PUBLISHED.value
        p.published_at = utcnow() - timedelta(hours=80)
        p.ig_media_id = f"m{i}"
        db.commit()
        an.record_snapshot(db, p, {"reach": 100, "total_interactions": 5 + i}, [])
    db.commit()
    res = svc.results(db, exp)
    assert res["observations"] == 2
    fr = res["factors"]["title_position"]["raw"]
    assert {g["group"] for g in fr["groups"]} == {"top", "bottom"}
    assert fr["test"]["performed"] is False  # amostra insuficiente → sem conclusão
    assert db.query(Post).filter_by(experiment_id=exp.id).count() == 2