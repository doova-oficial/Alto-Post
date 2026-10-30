"""Sugestões de temas a partir de fontes legítimas.

Fontes:
1. **Google Trends (RSS público "Trending now")** — https://trends.google.com/trending/rss?geo=BR-GO.
   O Google não oferece API oficial estável e gratuita para o Trends; o RSS é público, mas
   pode mudar sem aviso, traz apenas buscas em alta (não volume histórico) e raramente
   coincide com o tema "serviços". Por isso: timeout curto, cache, filtro por palavras-chave
   e o sistema funciona normalmente sem ele.
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
from typing import Any

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


def fetch_trends(settings: Settings | None = None, client: httpx.Client | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.google_trends_rss_enabled:
        return {"items": [], "source": "desativado", "error": None}
    geos = [settings.google_trends_geo] if settings.google_trends_geo in ("BR",) else [settings.google_trends_geo, "BR"]
    if "BR-GO" not in geos:
        geos.insert(0, "BR-GO")
    c = client or httpx.Client(timeout=10, headers={"User-Agent": "DoovaMarketingBot/1.0"})
    items: list[dict] = []
    errors = []
    for geo in geos:
        try:
            r = c.get(TRENDS_URL, params={"geo": geo})
            r.raise_for_status()
            for it in parse_trends_rss(r.text):
                it["geo"] = geo
                items.append(it)
        except Exception as e:  # rede, mudança de formato, bloqueio
            errors.append(f"{geo}: {type(e).__name__}")
    return {"items": items, "source": "Google Trends RSS", "error": "; ".join(errors) or None}


def cached_trends(db: Session, settings: Settings | None = None, max_age_hours: int = 6,
                  client: httpx.Client | None = None) -> dict[str, Any]:
    cache = get_state(db, CACHE_KEY)
    now = utcnow()
    if cache and cache.get("fetched_at") and (now.timestamp() - cache["fetched_at"]) < max_age_hours * 3600:
        return cache
    data = fetch_trends(settings, client)
    data["fetched_at"] = now.timestamp()
    if data["items"] or not cache:
        set_state(db, CACHE_KEY, data)
        db.commit()
        return data
    return {**cache, "error": data.get("error")}


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
        data = cached_trends(db, settings, client=trends_client)
        rel = relevant_trends(data.get("items", []), library.trend_keywords())
        trends_info = {"source": data.get("source"), "error": data.get("error"), "relevant": rel[:5],
                       "total_items": len(data.get("items", []))}
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