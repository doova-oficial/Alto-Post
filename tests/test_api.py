from app.models import PostStatus
from app.routes import deps
from app.security import limiter
from tests.conftest import ADMIN_TOKEN, MANUAL_CAPTION, make_content, reload_settings


def _use_fake_groq():
    cs = make_content()
    deps.set_factories(content=lambda: cs)
    return cs


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["dry_run"] is True


def test_every_admin_route_requires_token(client):
    from app.main import app

    checked = 0
    # usa o esquema OpenAPI (independe da estrutura interna de rotas da versão do FastAPI)
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        url = path.replace("{post_id}", "post_x").replace("{artwork_id}", "art_x").replace("{exp_id}", "1") \
            .replace("{opp_id}", "1").replace("{job}", "publish")
        for method in [m.upper() for m in ops]:
            limiter.reset()
            r = client.request(method, url)
            assert r.status_code == 401, (method, url, r.status_code)
            r = client.request(method, url, headers={"Authorization": "Bearer token-errado-mas-longo-0000000"})
            assert r.status_code == 401, (method, url)
            checked += 1
    assert checked > 30


def test_admin_disabled_without_configured_token(client, monkeypatch):
    reload_settings(monkeypatch, ADMIN_API_TOKEN="")
    assert client.get("/api/posts", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 503


def test_x_admin_token_header(client):
    assert client.get("/api/templates", headers={"X-Admin-Token": ADMIN_TOKEN}).status_code == 200


def test_auth_bruteforce_is_rate_limited(client):
    codes = [client.get("/api/posts", headers={"X-Admin-Token": "x"}).status_code for _ in range(12)]
    assert codes[-1] == 429


def test_full_flow_via_api(client, auth):
    _use_fake_groq()
    r = client.post("/api/posts", headers=auth, json={"theme_key": "chegada_ipora", "caption": MANUAL_CAPTION})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["status"] == PostStatus.PENDING_APPROVAL.value and post["template"] == "aquisicao"
    assert [p["id"] for p in client.get("/api/posts/pending", headers=auth).json()] == [post["id"]]
    assert client.post(f"/api/posts/{post['id']}/approve", headers=auth, json={}).json()["status"] == "approved"
    r = client.post(f"/api/posts/{post['id']}/publish-now", headers=auth)
    assert r.status_code == 200 and r.json()["outcome"] == "simulated"
    detail = client.get(f"/api/posts/{post['id']}", headers=auth).json()
    assert detail["status"] == "simulated" and len(detail["events"]) >= 4
    hist = client.get("/api/posts/history", headers=auth).json()
    assert hist["posts"][0]["id"] == post["id"]


def test_generated_caption_via_api(client, auth):
    _use_fake_groq()
    r = client.post("/api/posts", headers=auth, json={"template": "pergunta", "audience": "contratantes",
                                                       "topic": "Serviços difíceis de encontrar"})
    assert r.status_code == 201 and r.json()["caption"]
    assert "#" in r.json()["caption"]


def test_reject_and_pause(client, auth):
    _use_fake_groq()
    pid = client.post("/api/posts", headers=auth, json={"theme_key": "apresentacao", "caption": MANUAL_CAPTION}).json()["id"]
    assert client.post(f"/api/posts/{pid}/reject", headers=auth, json={"reason": "não gostei"}).json()["status"] == "rejected"
    assert client.post(f"/api/posts/{pid}/approve", headers=auth, json={}).status_code == 409
    assert client.post("/api/scheduler/pause", headers=auth, json={"reason": "teste"}).json()["paused"] is True
    assert client.get("/health").json()["paused"] is True
    assert client.post("/api/scheduler/resume", headers=auth).json()["paused"] is False


def test_media_endpoint_serves_jpeg_by_unguessable_token(client, auth, db):
    _use_fake_groq()
    pid = client.post("/api/posts", headers=auth, json={"theme_key": "apresentacao", "caption": MANUAL_CAPTION}).json()["id"]
    from app.models import Post

    token = db.get(Post, pid).artwork.public_token
    r = client.get(f"/media/{token}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert client.get("/media/" + "a" * 43 + ".jpg").status_code == 404


def test_validation_rejects_unknown_fields_and_bad_values(client, auth):
    _use_fake_groq()
    assert client.post("/api/art", headers=auth, json={"template": "pergunta", "params": {"inventado": 1}}).status_code == 422
    assert client.post("/api/art", headers=auth, json={"template": "pergunta", "params": {"title_position": "lado"}}).status_code == 422
    assert client.post("/api/art", headers=auth, json={"template": "naoexiste"}).status_code == 422
    r = client.post("/api/posts", headers=auth, json={"template": "autonomos", "content_kind": "opportunity",
                                                       "caption": MANUAL_CAPTION})
    assert r.status_code == 422


def test_generation_rate_limit(client, auth, monkeypatch):
    reload_settings(monkeypatch, RATE_LIMIT_GENERATION_PER_MINUTE=2)
    codes = [client.post("/api/art", headers=auth, json={"template": "institucional"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_config_does_not_leak_secrets(client, auth):
    body = client.get("/api/config", headers=auth).text
    assert ADMIN_TOKEN not in body and "gsk_" not in body


def test_suggestions_and_library(client, auth):
    r = client.get("/api/suggestions?include_trends=false", headers=auth)
    assert r.status_code == 200 and r.json()["suggestions"]
    assert all(s["evidence_level"] for s in r.json()["suggestions"])
    assert len(client.get("/api/library", headers=auth).json()["launch_plan"]) >= 10


def test_metrics_summary_empty(client, auth):
    r = client.get("/api/metrics/summary", headers=auth)
    assert r.status_code == 200 and r.json()["posts_analyzed"] == 0 and r.json()["limitations"]