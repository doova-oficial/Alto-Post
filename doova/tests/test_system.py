from datetime import date

import httpx

from app.logging_config import redact
from app.services import scheduler
from app.services.backup import run_backup
from app.services.trend_content import match_theme, screen_trend
from app.services.trends_service import cached_trends, fetch_trends, parse_trends_rss, relevant_trends, suggestions
from tests.conftest import reload_settings

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>preço do pedreiro</title><ht:approx_traffic>200+</ht:approx_traffic><pubDate>x</pubDate>
<ht:news_item><ht:news_item_title>Quanto cobra um pedreiro em Goiás</ht:news_item_title></ht:news_item></item>
<item><title>futebol hoje</title><ht:approx_traffic>500+</ht:approx_traffic></item>
</channel></rss>"""


def test_trends_parsing_and_filter():
    items = parse_trends_rss(RSS)
    assert [i["term"] for i in items] == ["preço do pedreiro", "futebol hoje"]
    rel = relevant_trends(items, ["pedreiro", "goiás"])
    assert len(rel) == 1 and set(rel[0]["matched"]) == {"pedreiro", "goias"}


def test_trends_fetch_uses_brazil_only_when_goias_has_no_trends(monkeypatch):
    s = reload_settings(monkeypatch, GOOGLE_TRENDS_RSS_ENABLED="true")
    requests = []

    def handler(request):
        requests.append(request.url.params["geo"])
        if request.url.params["geo"] == "BR-GO":
            return httpx.Response(200, text="<rss><channel /></rss>")
        return httpx.Response(200, text=RSS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_trends(s, client)

    assert requests == ["BR-GO", "BR"]
    assert result["items"] and all(item["geo"] == "BR" for item in result["items"])


def test_trends_fetch_does_not_query_brazil_when_goias_has_trends(monkeypatch):
    s = reload_settings(monkeypatch, GOOGLE_TRENDS_RSS_ENABLED="true")
    requests = []

    def handler(request):
        requests.append(request.url.params["geo"])
        return httpx.Response(200, text=RSS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_trends(s, client)

    assert requests == ["BR-GO"]
    assert result["items"] and all(item["geo"] == "BR-GO" for item in result["items"])


def test_trends_fetch_falls_back_to_international_feed_when_brazil_is_irrelevant(monkeypatch):
    s = reload_settings(monkeypatch, GOOGLE_TRENDS_RSS_ENABLED="true")
    requests = []
    international_rss = RSS.replace("preço do pedreiro", "roof repair")

    def handler(request):
        geo = request.url.params["geo"]
        requests.append(geo)
        if geo == "US":
            return httpx.Response(200, text=international_rss)
        return httpx.Response(200, text=RSS)

    def is_relevant(items):
        return any(item["term"] == "roof repair" for item in items)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_trends(s, client, is_relevant=is_relevant)

    assert requests == ["BR-GO", "BR", "US"]
    assert result["geo"] == "US" and result["items"][0]["geo"] == "US"


def test_cached_trends_falls_through_irrelevant_feeds(db, monkeypatch):
    s = reload_settings(monkeypatch, GOOGLE_TRENDS_RSS_ENABLED="true")
    requests = []
    international_rss = RSS.replace("preço do pedreiro", "roof repair")

    def handler(request):
        geo = request.url.params["geo"]
        requests.append(geo)
        if geo == "US":
            return httpx.Response(200, text=international_rss)
        return httpx.Response(200, text=RSS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = cached_trends(
        db, s, client=client,
        is_relevant=lambda items: any(item["term"] == "roof repair" for item in items),
    )

    assert requests == ["BR-GO", "BR", "US"]
    assert result["geo"] == "US"
    assert result["searched_geos"] == ["BR-GO", "BR", "US"]


def test_international_trend_keywords_and_antifraud_headlines():
    assert match_theme("Black Friday") == "autonomos"
    assert screen_trend("Black Friday", ["Guia para não cair em golpes"]) is None
    assert screen_trend("golpe do Pix", []) is not None


def test_suggestions_with_trends_and_fallback(db, monkeypatch):
    s = reload_settings(monkeypatch, GOOGLE_TRENDS_RSS_ENABLED="true")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=RSS)))
    out = suggestions(db, s, today=date(2026, 11, 1), trends_client=client, limit=30)
    kinds = {x["evidence"] for x in out["suggestions"]}
    assert "Google Trends RSS" in kinds and "calendário" in kinds
    assert any(x["theme"].startswith("Black Friday") for x in out["suggestions"])
    # sem rede: o sistema continua sugerindo
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    from app.services.state import set_state

    set_state(db, "google_trends_cache", None)
    db.commit()
    out = suggestions(db, s, trends_client=bad)
    assert out["suggestions"] and out["trends"]["error"]
    assert all(x["evidence_level"] in ("hipótese", "dados históricos", "tendência de busca (indicativa)")
               for x in out["suggestions"])


def test_redaction():
    t = redact("GET /x?access_token=EAAsecret123&a=1 Authorization: Bearer abc.def gsk_ABCDEFGHIJKLMNOP", ["meusegredo"])
    assert "EAAsecret123" not in t and "abc.def" not in t and "gsk_ABCDEFGHIJKLMNOP" not in t
    assert redact("valor meusegredo aqui", ["meusegredo"]) == "valor *** aqui"


def test_tick_runs_due_jobs_once():
    first = scheduler.tick()
    assert first["publish"]["results"] == [] and first["metrics"] == {"skipped": "Meta não configurada"}
    assert first["backup"]["done"] is True
    second = scheduler.tick()
    assert second["backup"] == {"not_due": True} and second["maintenance"] == {"not_due": True}


def test_job_lock_prevents_concurrent_runs():
    from app.services.state import acquire_lock

    assert acquire_lock("job:backup", 600, owner="outro")
    assert scheduler.run_job("backup") == {"skipped": "em execução em outro processo"}


def test_backup_keeps_limit(monkeypatch):
    s = reload_settings(monkeypatch, BACKUP_KEEP=2)
    import time

    for _ in range(3):
        assert run_backup(s)["done"]
        time.sleep(1.05)
    assert len(list(s.backup_dir.glob("doova-*.db"))) == 2


def test_token_refresh_stored_encrypted(monkeypatch):
    from cryptography.fernet import Fernet

    from app.models import StoredToken
    from app.database import SessionLocal
    from app.services.token_store import current_access_token, refresh_token

    s = reload_settings(monkeypatch, META_LOGIN_TYPE="instagram", META_IG_USER_ID="1",
                        META_ACCESS_TOKEN="IGAAantigo" + "a" * 30,
                        TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode())

    class M:
        def refresh_long_lived_token(self, tok):
            assert tok.startswith("IGAAantigo")
            return "IGAAnovo" + "b" * 30, 5184000

    res = refresh_token(s, meta=M())
    assert res["refreshed"] is True
    assert current_access_token(s).startswith("IGAAnovo")
    with SessionLocal() as db:
        assert "IGAAnovo" not in db.query(StoredToken).one().ciphertext

def test_unified_login_uses_facebook_graph_and_disables_refresh(monkeypatch):
    from app.config import Settings
    from app.services.meta_service import MetaError, MetaService
    from app.services.token_store import refresh_token

    s = reload_settings(monkeypatch, META_LOGIN_TYPE="unified", META_IG_USER_ID="17840000000000000",
                        META_ACCESS_TOKEN="SUAT-test-token")
    assert s.meta_host == "https://graph.facebook.com"
    assert s.meta_configured is True

    class MustNotRefresh:
        def refresh_long_lived_token(self, token):
            raise AssertionError("Unified Login não deve tentar renovar SUAT")

    result = refresh_token(s, meta=MustNotRefresh())
    assert result["refreshed"] is False
    assert "SUAT" in result["reason"]

    try:
        MetaService(s).refresh_long_lived_token("SUAT-test-token")
    except MetaError as exc:
        assert "SUAT" in str(exc)
    else:
        raise AssertionError("a renovação do SUAT deveria ser bloqueada")
