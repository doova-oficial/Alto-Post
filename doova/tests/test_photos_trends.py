"""Fotos de bancos gratuitos e assuntos do momento — somente simuladores, nenhuma rede real."""
from __future__ import annotations

import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image

from tests.conftest import ADMIN_TOKEN, reload_settings


def _jpeg(w=1400, h=1000, color=(90, 120, 150)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


OV_RESULT = {
    "id": "ov-1", "title": "Roof repair work", "foreign_landing_url": "https://www.flickr.com/photos/x/1",
    "url": "https://img.example/roof.jpg", "creator": "Ana Foto", "creator_url": "https://www.flickr.com/photos/x",
    "license": "by", "license_version": "2.0", "license_url": "https://creativecommons.org/licenses/by/2.0/",
    "provider": "flickr", "source": "flickr", "category": "photograph", "tags": [{"name": "roof"}, {"name": "repair"}],
    "attribution": '"Roof repair work" by Ana Foto is licensed under CC BY 2.0.', "mature": False,
    "width": 1600, "height": 1200, "thumbnail": "https://api.openverse.org/v1/images/ov-1/thumb/",
}


def _transport(calls: list, ov_results=None, pix_hits=None, image_bytes=None, ov_status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        host = req.url.host
        if host == "api.openverse.org":
            if ov_status != 200:
                return httpx.Response(ov_status, json={"detail": "x"})
            if req.url.path.rstrip("/").endswith("/images"):
                res = ov_results if ov_results is not None else [OV_RESULT]
                return httpx.Response(200, json={"result_count": len(res), "results": res})
            return httpx.Response(200, json=OV_RESULT)
        if host == "pixabay.com":
            return httpx.Response(200, json={"total": 1, "hits": pix_hits or []})
        if host == "img.example":
            return httpx.Response(200, content=image_bytes or _jpeg(), headers={"content-type": "image/jpeg"})
        if host == "trends.google.com":
            return httpx.Response(404)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def db():
    from app.database import SessionLocal

    s = SessionLocal()
    yield s
    s.close()


# ---------------------------------------------------------------- busca
def test_openverse_filters_license_and_caches(db):
    from app.services import image_search as isr

    bad = dict(OV_RESULT, id="ov-nc", license="by-nc")
    sa = dict(OV_RESULT, id="ov-sa", license="by-sa")
    small = dict(OV_RESULT, id="ov-small", width=500)
    calls = []
    c = _transport(calls, ov_results=[OV_RESULT, bad, sa, small])
    items = isr.search(db, "roof repair", "openverse", client=c)
    assert [i.provider_id for i in items] == ["ov-1"]
    q = httpx.URL(calls[0]).params
    assert q["license"] == "cc0,pdm,by" and q["category"] == "photograph" and q["mature"] == "false"
    isr.search(db, "roof repair", "openverse", client=c)
    assert len(calls) == 1  # segunda busca veio do cache de 24 h


def test_pixabay_requires_key_and_uses_portuguese(db, monkeypatch):
    from app.services import image_search as isr

    with pytest.raises(isr.ImageSearchError):
        isr.search(db, "telhado", "pixabay", client=_transport([]))
    s = reload_settings(monkeypatch, PIXABAY_API_KEY="pix-teste-123")
    calls = []
    hit = {"id": 7, "pageURL": "https://pixabay.com/photos/telhado-7/", "tags": "telhado, casa",
           "largeImageURL": "https://img.example/7.jpg", "imageWidth": 4000, "imageHeight": 3000,
           "user": "joao", "user_id": 9}
    items = isr.search(db, "telhado", "pixabay", settings=s, client=_transport(calls, pix_hits=[hit]))
    assert items[0].license == "pixabay" and items[0].width == 1280
    params = httpx.URL(calls[0]).params
    assert params["lang"] == "pt" and params["safesearch"] == "true" and params["image_type"] == "photo"


def test_import_photo_and_credits(db):
    from app.services import image_search as isr

    c = _transport([])
    cand = isr.search(db, "roof", "openverse", client=c)[0]
    photo = isr.import_photo(db, cand, client=c)
    assert photo.jpg_bytes[:3] == b"\xff\xd8\xff" and photo.width == 1400
    assert isr.import_photo(db, cand, client=c).id == photo.id  # sem duplicar
    credit = isr.caption_credit(photo)
    assert "Ana Foto" in credit and "CC BY 2.0" in credit and "Flickr via Openverse" in credit
    assert "Recortada" in credit and "#" not in credit
    assert isr.short_credit(photo) == "Foto: Ana Foto (CC BY 2.0)"


def test_import_rejects_non_image_and_small(db):
    from app.services import image_search as isr

    cand = isr.search(db, "roof", "openverse", client=_transport([]))[0]
    with pytest.raises(isr.ImageSearchError):
        isr.import_photo(db, cand, client=_transport([], image_bytes=_jpeg(600, 400)))


def test_upstream_failure_is_reported(db, monkeypatch):
    from app.services import image_search as isr

    monkeypatch.setattr(isr.time, "sleep", lambda *_: None)
    with pytest.raises(isr.ImageSearchError) as e:
        isr.search(db, "roof", "openverse", client=_transport([], ov_status=503))
    assert e.value.upstream


# ---------------------------------------------------------------- triagem
@pytest.mark.parametrize("term,news", [
    ("acidente na BR-060", []), ("Eleições 2026", []), ("Lula", []), ("Mega-Sena", []),
    ("chuva em Goiânia", ["Temporal deixa vítimas"]), ("Corinthians x Flamengo", []),
    ("5 mil para MEI", []), ("auxílio para autônomos", []), ("MEI", ["Governo libera R$ 5 mil"]),
])
def test_sensitive_or_unrelated_trends_are_dropped(term, news):
    from app.services.trend_content import match_theme, screen_trend

    assert screen_trend(term, news) is not None or match_theme(term) is None


def test_service_trends_are_matched():
    from app.services.trend_content import match_theme, screen_trend

    assert screen_trend("onda de calor", []) is None and match_theme("Onda de calor") == "calor"
    assert match_theme("Chuva em Iporá") == "chuva"
    assert match_theme("Enem 2026") == "estudos"


def test_extra_blocklist(monkeypatch):
    from app.services.trend_content import screen_trend

    s = reload_settings(monkeypatch, TREND_BLOCKLIST_EXTRA="calor")
    assert screen_trend("onda de calor", [], s) is not None


# -------------------------------------------------------------- ponta a ponta
def _content(groq_text: str):
    from app.services.content_service import ContentService
    from app.services.groq_service import GroqCaptionService
    from tests.conftest import groq_response

    def handler(req):
        return httpx.Response(200, json=groq_response(groq_text))

    return ContentService(caption_service=GroqCaptionService(client=httpx.Client(transport=httpx.MockTransport(handler)),
                                                             sleep=lambda *_: None))


CAP = ("Choveu e a goteira apareceu? Quando o telhado precisa de conserto, o Doova ajuda a encontrar quem faz "
       "esse tipo de serviço. Acompanhe o perfil. #Iporá #Doova")


def test_trend_run_creates_photo_post_for_review(db, monkeypatch):
    from app.models import Post
    from app.services import trend_content as tc

    s = reload_settings(monkeypatch, TREND_USE_GROQ="false")
    items = [{"term": "Acidente na GO-060", "news": [], "geo": "BR-GO"},
             {"term": "Chuva em Iporá", "news": ["Previsão indica chuva"], "geo": "BR-GO"},
             {"term": "Onda de calor", "news": [], "geo": "BR"}]
    res = tc.run_trend_auto(db, s, force=True, client=_transport([]), content=_content(CAP), items=items)
    assert len(res["created"]) == 1  # limite padrão: 1 por dia
    decisions = {r["term"]: r["decision"] for r in res["report"]}
    assert decisions["Acidente na GO-060"] == "descartado"
    post = db.get(Post, res["created"][0])
    assert post.content_kind == "trend" and post.photo_id and post.trend_term == "Chuva em Iporá"
    assert post.variant.template_key in tc.PHOTO_TEMPLATES
    assert post.status == "needs_review"  # foto externa e assunto do momento sempre passam por revisão
    assert "Ana Foto" in post.caption.text and post.caption.text.count("#") <= 3
    assert post.expires_at is not None
    assert any(e["name"] == "photo_credit" for e in post.artwork.layout_report["elements"])
    again = tc.run_trend_auto(db, s, force=True, client=_transport([]), content=_content(CAP), items=items)
    assert again["created"] == [] and again["skipped"]


def test_trend_run_disabled_by_default(db):
    from app.services import trend_content as tc

    res = tc.run_trend_auto(db, items=[{"term": "Onda de calor"}])
    assert res["created"] == [] and "TREND_AUTO_ENABLED" in res["skipped"]


def test_trend_expiry_and_preflight(db, monkeypatch):
    from app.database import utcnow
    from app.services import trend_content as tc
    from app.services.publisher import Publisher

    s = reload_settings(monkeypatch, TREND_USE_GROQ="false")
    res = tc.run_trend_auto(db, s, force=True, client=_transport([]), content=_content(CAP),
                            items=[{"term": "Chuva em Iporá", "geo": "BR-GO"}])
    from app.models import Post

    post = db.get(Post, res["created"][0])
    post.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert "assunto do momento expirado" in Publisher(s).preflight(db, post)
    assert tc.expire_trend_posts(db) == 1
    db.refresh(post)
    assert post.status == "cancelled"


def test_manual_caption_keeps_photo_credit(db, monkeypatch):
    from app.services import trend_content as tc

    s = reload_settings(monkeypatch, TREND_USE_GROQ="false")
    cs = _content(CAP)
    res = tc.run_trend_auto(db, s, force=True, client=_transport([]), content=cs,
                            items=[{"term": "Chuva em Iporá", "geo": "BR-GO"}])
    from app.models import Post

    post = db.get(Post, res["created"][0])
    cap = cs.set_manual_caption(db, post, "Telhado com goteira? O Doova ajuda a achar quem conserta. Siga o perfil.")
    assert "Ana Foto" in cap.text


def test_groq_can_veto_or_pick_theme(monkeypatch):
    from app.services import trend_content as tc

    class FakeGroq:
        def __init__(self, out):
            self.out = out

        def chat_json(self, *a, **k):
            return self.out

    assert tc.groq_theme(FakeGroq({"sensitive": True, "reason": "celebridade"}), "x", [])[0] is None
    assert tc.groq_theme(FakeGroq({"sensitive": False, "theme": "festas"}), "Halloween", [])[0] == "festas"
    assert tc.groq_theme(FakeGroq({"sensitive": False, "theme": "inventado"}), "x", [])[0] is None


def test_photo_template_requires_photo(db):
    from app.services.content_service import ContentError, ContentService

    with pytest.raises(ContentError):
        ContentService().create_art(db, "foto_destaque", {})


def test_photo_fundo_keeps_contrast_on_white_photo():
    from app.services.image_generator import render
    from app.services.template_config import get_template_config

    cfg = get_template_config()
    white = Image.new("RGB", (1400, 1000), (255, 255, 255))
    rep = render(cfg.resolve("foto_fundo", {"photo_credit": "Foto: X"}), photo=white).report
    assert rep["min_contrast_text_vs_background"] >= 4.5


# ------------------------------------------------------------------- API
def test_photo_api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.routes import deps

    calls = []
    deps.set_factories(http=lambda: _transport(calls))
    client = TestClient(create_app())
    h = {"X-Admin-Token": ADMIN_TOKEN}
    assert client.get("/api/photos/search", params={"q": "roof"}).status_code == 401
    r = client.get("/api/photos/search", params={"q": "roof"}, headers=h)
    assert r.status_code == 200 and r.json()["results"][0]["provider_id"] == "ov-1"
    r = client.post("/api/photos/import", json={"provider": "openverse", "provider_id": "ov-1"}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.get(f"/api/photos/{pid}/image", headers=h).content[:3] == b"\xff\xd8\xff"
    r = client.post("/api/art", json={"template": "foto_destaque", "params": {"photo_id": pid}}, headers=h)
    assert r.status_code in (200, 201), r.text
    assert any(t["key"] == "foto_fundo" and t["requires_photo"] for t in client.get("/api/templates", headers=h).json())
    r = client.get("/api/trends/evaluate", headers=h)
    assert r.status_code == 200 and "report" in r.json()

def test_find_photo_falls_back_to_shorter_query(db):
    from app.services import image_search as isr

    assert isr.fallback_queries("air conditioner repair") == ["air conditioner repair", "air conditioner", "air"]
    calls = []
    res = dict(OV_RESULT, title="Air conditioner", tags=[{"name": "air conditioner"}])

    def handler(req):
        calls.append(req.url.params.get("q"))
        if req.url.host == "img.example":
            return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})
        results = [res] if req.url.params.get("q") == "air conditioner" else []
        return httpx.Response(200, json={"result_count": len(results), "results": results})

    photo, log = isr.find_photo(db, "air conditioner repair", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert photo is not None and calls[:2] == ["air conditioner repair", "air conditioner"]
