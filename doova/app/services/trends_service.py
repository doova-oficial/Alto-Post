"""Sugestões de temas a partir de fontes legítimas.

Fontes:
1. **Google Trends (RSS público "Trending now")** — Goiás, Brasil e, se necessário, feeds
   de uma amostra internacional (EUA, México, Reino Unido, Índia, Japão e Austrália).
   O Google não oferece feed mundial agregado nem API oficial estável e gratuita para o Trends;
   os RSSs são públicos, podem mudar sem aviso e trazem buscas em alta, não volume histórico.
   Por isso: timeout curto, cache, filtro por relevância e o sistema funciona normalmente sem ele.
2. Calendário de datas comemorativas (app/content/dates.yaml).
3. Biblioteca de temas e perguntas frequentes de autônomos (app/content/themes.yaml).
4. Resultados históricos das próprias publicações (analytics_service).

Sem dados suficientes, toda sugestão é rotulada como **hipótese**.
"""
from __future__ import annotations

import logging
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Post
from app.services import analytics_service as an
from app.services import library
from app.services.state import get_state, set_state
from app.timeutil import utc_to_local

logger = logging.getLogger(__name__)
TRENDS_URL = "https://trends.google.com/trending/rss"
NS = {"ht": "https://trends.google.com/trending/rss"}
CACHE_KEY = "google_trends_cache"
WORLD_TREND_GEOS = ("US", "MX", "GB", "IN", "JP", "AU")
TREND_GEOS = ("BR-GO", "BR", *WORLD_TREND_GEOS)


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def parse_trends_rss(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        news = [n.findtext("ht:news_item_title", default="", namespaces=NS) for n in item.findall("ht:news_item", NS)]
        out.append({"term": (item.findtext("title") or "").strip(),
                    "approx_traffic": item.findtext("ht:approx_traffic", default=None, namespaces=NS),
                    "published": item.findtext("pubDate"), "news": [n for n in news if n][:3]})
    return out


def _fetch_trends_geo(geo: str, client: httpx.Client) -> dict[str, Any]:
    try:
        response = client.get(TRENDS_URL, params={"geo": geo})
        response.raise_for_status()
        items = parse_trends_rss(response.text)
        for item in items:
            item["geo"] = geo
        return {"items": items, "source": "Google Trends RSS", "geo": geo, "error": None}
    except Exception as exc:  # rede, mudança de formato, bloqueio
        return {"items": [], "source": "Google Trends RSS", "geo": geo,
                "error": f"{geo}: {type(exc).__name__}"}


def fetch_trends(settings: Settings | None = None, client: httpx.Client | None = None,
                 is_relevant: Callable[[list[dict[str, Any]]], bool] | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.google_trends_rss_enabled:
        return {"items": [], "source": "desativado", "error": None}
    owns_client = client is None
    c = client or httpx.Client(timeout=10, headers={"User-Agent": "DoovaMarketingBot/1.0"})
    errors = []
    last_result: dict[str, Any] = {"items": [], "source": "Google Trends RSS", "geo": None, "error": None}
    searched = []
    try:
        for geo in TREND_GEOS:
            result = _fetch_trends_geo(geo, c)
            searched.append(geo)
            if result["error"]:
                errors.append(result["error"])
            if result["items"]:
                last_result = result
                if is_relevant is None or is_relevant(result["items"]):
                    return {**result, "searched_geos": searched, "error": "; ".join(errors) or None}
        return {**last_result, "searched_geos": searched, "error": "; ".join(errors) or None}
    finally:
        if owns_client:
            c.close()


def cached_trends(db: Session, settings: Settings | None = None, max_age_hours: int = 6,
                  client: httpx.Client | None = None,
                  is_relevant: Callable[[list[dict[str, Any]]], bool] | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.google_trends_rss_enabled:
        return {"items": [], "source": "desativado", "error": None}
    now = utcnow()
    cache = get_state(db, CACHE_KEY) or {}
    regions = dict(cache.get("regions") or {})
    owns_client = client is None
    c = client or httpx.Client(timeout=10, headers={"User-Agent": "DoovaMarketingBot/1.0"})
    errors = []
    searched = []
    last_result: dict[str, Any] = {"items": [], "source": "Google Trends RSS", "geo": None, "error": None}
    try:
        for geo in TREND_GEOS:
            region = regions.get(geo)
            fresh = (region and region.get("fetched_at")
                     and now.timestamp() - region["fetched_at"] < max_age_hours * 3600)
            if fresh:
                result = region
            else:
                result = _fetch_trends_geo(geo, c)
                regions[geo] = {**result, "fetched_at": now.timestamp()}
                cache["regions"] = regions
                set_state(db, CACHE_KEY, cache)
                db.commit()
            searched.append(geo)
            if result.get("error"):
                errors.append(result["error"])
            if result.get("items"):
                last_result = result
                if is_relevant is None or is_relevant(result["items"]):
                    return {**result, "searched_geos": searched, "error": "; ".join(errors) or None}
        return {**last_result, "searched_geos": searched, "error": "; ".join(errors) or None}
    finally:
        if owns_client:
            c.close()


def relevant_trends(items: list[dict], keywords: list[str]) -> list[dict]:
    kws = [_norm(k) for k in keywords]
    out = []
    for it in items:
        text = _norm(" ".join([it["term"]] + it.get("news", [])))
        hits = [k for k in kws if k in text]
        if hits:
            out.append({**it, "matched": hits})
    return out


def theme_usage(db: Session) -> dict[str, dict]:
    rows = db.execute(select(Post.theme_key, func.count(Post.id), func.max(Post.created_at))
                      .group_by(Post.theme_key)).all()
    return {k: {"count": n, "last": last} for k, n, last in rows if k}


def suggestions(db: Session, settings: Settings | None = None, today: date | None = None, limit: int = 10,
                trends_client: httpx.Client | None = None, include_trends: bool = True) -> dict[str, Any]:
    settings = settings or get_settings()
    today = today or utc_to_local(utcnow()).date()
    out: list[dict[str, Any]] = []

    # 1) datas comemorativas próximas
    for d in library.upcoming_dates(today, 45):
        out.append({
            "theme": f"{d['name']} ({d['date']})", "theme_key": None, "justification":
                f"Data comemorativa em {d['days_until']} dia(s). {d['idea']}.",
            "audience": d["audience"], "format": d["template"], "cta": "Salve para lembrar" if d["template"] == "educativo" else "Conheça o Doova",
            "evidence": "calendário", "evidence_level": "hipótese", "priority": 1 if d["days_until"] <= 14 else 2,
        })

    # 2) desempenho histórico por tema
    ds = an.build_dataset(db, settings)
    comp = an.compare(ds["rows"], "theme", "engagement_rate", settings)
    hist = {g["group"]: g for g in comp["groups"]}
    usage = theme_usage(db)
    themes = library.themes()

    def score(item):
        key, t = item
        u = usage.get(key, {"count": 0, "last": None})
        g = hist.get(key)
        perf = g["mean"] if g and g["sufficient_sample"] else 0
        return (-perf, u["count"], u["last"] or utcnow() - timedelta(days=3650))

    for key, t in sorted(themes.items(), key=score)[:6]:
        g = hist.get(key)
        if g and g["sufficient_sample"]:
            level = "dados históricos"
            just = (f"Taxa média de engajamento {g['mean']:.2%} em {g['n']} publicações (idade padronizada de "
                    f"{ds['target_age_hours']:.0f} h). Associação observada; não garante o mesmo resultado.")
        elif g:
            level = "hipótese"
            just = f"Só {g['n']} publicação(ões) com métricas; dados insuficientes para afirmar desempenho."
        else:
            level = "hipótese"
            used = usage.get(key, {}).get("count", 0)
            just = ("Tema da biblioteca ainda não publicado; ajuda a cobrir o público " + t["audience"]
                    if not used else f"Tema usado {used} vez(es); sem métricas suficientes.")
        out.append({"theme": t["topic"], "theme_key": key, "justification": just, "audience": t["audience"],
                    "format": t["template"], "cta": t.get("cta"), "evidence": "biblioteca + histórico",
                    "evidence_level": level, "priority": 2 if level == "dados históricos" else 3})

    # 3) perguntas frequentes de autônomos
    for q in library.faq()[:3]:
        out.append({"theme": q, "theme_key": None, "justification": "Pergunta frequente de trabalhadores autônomos; "
                    "conteúdo educativo útil independentemente de tendências.", "audience": "prestadores",
                    "format": "educativo", "cta": "Salve para consultar depois", "evidence": "perguntas frequentes",
                    "evidence_level": "hipótese", "priority": 3})

    # 4) tendências de busca relevantes
    trends_info = {"source": "Google Trends RSS", "error": None, "relevant": []}
    if include_trends:
        keywords = library.trend_keywords()
        data = cached_trends(
            db, settings, client=trends_client,
            is_relevant=lambda items: bool(relevant_trends(items, keywords)),
        )
        rel = relevant_trends(data.get("items", []), keywords)
        trends_info = {"source": data.get("source"), "error": data.get("error"), "relevant": rel[:5],
                       "total_items": len(data.get("items", [])), "geo": data.get("geo"),
                       "searched_geos": data.get("searched_geos", [])}
        for it in rel[:3]:
            out.append({"theme": f"Busca em alta: {it['term']}", "theme_key": None,
                        "justification": f"Termo em alta no Google ({it['geo']}, tráfego aprox. {it['approx_traffic']}) "
                                         f"relacionado a: {', '.join(it['matched'])}. Verifique se o assunto tem relação "
                                         "real com serviços antes de usar; não associe o Doova a notícias sensíveis.",
                        "audience": "ambos", "format": "educativo", "cta": None, "evidence": "Google Trends RSS",
                        "evidence_level": "tendência de busca (indicativa)", "priority": 2})
    out.sort(key=lambda s: s["priority"])
    return {
        "generated_for": today.isoformat(), "suggestions": out[:limit], "trends": trends_info,
        "notes": [
            "Sugestões marcadas como 'hipótese' não são tendências comprovadas.",
            "O RSS do Google Trends mostra apenas buscas em alta recentes; não há API oficial estável. "
            "Para pesquisas de volume por termo, consulte trends.google.com manualmente.",
            "Informações regionais de Iporá devem ser adicionadas ao calendário (dates.yaml) somente após "
            "conferência em fontes oficiais.",
        ],
    }