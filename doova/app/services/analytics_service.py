"""Coleta de métricas e análise de resultados.

Princípios:
* toda taxa tem numerador e denominador explícitos (ex.: interações / alcance);
* métricas indisponíveis são ``None`` (nunca zero) e ficam registradas;
* comparações usam um *snapshot* de idade padronizada (``ANALYSIS_TARGET_AGE_HOURS``)
  para não comparar uma publicação de poucas horas com outra de vários dias;
* testes estatísticos (permutação) só rodam quando cada grupo tem amostra mínima;
* resultados são descritos como associações observadas, nunca como causalidade.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import random
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Campaign, MetricSnapshot, Post, PostStatus
from app.services.meta_service import MetaError, MetaService
from app.timeutil import iso_utc, utc_to_local

logger = logging.getLogger(__name__)

WEEKDAYS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
COUNT_FIELDS = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions",
                "profile_visits", "profile_activity", "follows"]

RATE_DEFS: dict[str, tuple[str, str, str]] = {
    "engagement_rate": ("interactions", "reach",
                        "interações (total_interactions; ou curtidas+comentários+compartilhamentos+salvamentos) / alcance"),
    "like_rate": ("likes", "reach", "curtidas / alcance"),
    "comment_rate": ("comments", "reach", "comentários / alcance"),
    "share_rate": ("shares", "reach", "compartilhamentos / alcance"),
    "save_rate": ("saved", "reach", "salvamentos / alcance"),
    "profile_visit_rate": ("profile_visits", "reach", "visitas ao perfil / alcance"),
    "follow_rate": ("follows", "reach", "novos seguidores atribuídos / alcance"),
    "views_per_reach": ("views", "reach", "visualizações / alcance (média de vezes que cada conta viu)"),
}

DIMENSIONS: dict[str, str] = {
    "template": "Template (estilo visual)",
    "title_position": "Posição do título",
    "subtitle_position": "Posição do subtítulo",
    "title_size": "Tamanho do título",
    "text_amount": "Quantidade de texto",
    "info_quantity": "Quantidade de itens/informações",
    "primary_color": "Cor predominante",
    "accent_color": "Cor de destaque",
    "background": "Tipo de fundo",
    "shapes": "Formas geométricas",
    "has_photo": "Com ou sem foto",
    "photo_provider": "Banco de imagens da foto",
    "message_type": "Tipo de mensagem",
    "content_kind": "Tipo de conteúdo",
    "cta": "Chamada para ação",
    "theme": "Tema",
    "audience": "Público",
    "campaign": "Campanha",
    "weekday": "Dia da semana",
    "hour_bucket": "Faixa de horário",
}

BASE_LIMITATIONS = [
    "As métricas vêm da API oficial da Meta e medem distribuição (alcance, visualizações) e ações "
    "(curtidas, comentários, compartilhamentos, salvamentos, visitas ao perfil). Elas não medem atenção "
    "visual: não é possível saber para onde as pessoas olharam nem qual posição 'atrai mais o olhar'.",
    "O alcance depende do algoritmo de distribuição, do horário, do tema e do número de seguidores; "
    "diferenças entre layouts podem ser causadas por esses fatores e não pelo layout.",
    "A Meta informa que os dados podem atrasar até 48 h; métricas indisponíveis são tratadas como ausentes, não como zero.",
    "A métrica 'impressions' foi descontinuada para mídias criadas após 02/07/2024; usa-se 'views' quando disponível.",
    "Cliques e conversões no aplicativo não são medidos pela API de publicações de feed; exigem link rastreável "
    "(por exemplo, UTM no link da bio) e integração com a análise do aplicativo, que não estão configuradas.",
    "Os testes de permutação indicam se uma diferença é improvável sob acaso, considerando estas amostras; "
    "não provam causalidade. Resultados devem ser replicados antes de virar regra.",
]


# ------------------------------------------------------------ coleta
def collect_metrics(db: Session, meta: MetaService | None = None, now: datetime | None = None,
                    settings: Settings | None = None, min_interval_hours: float = 1.0) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or utcnow()
    meta = meta or MetaService(settings)
    posts = db.scalars(select(Post).where(
        Post.status == PostStatus.PUBLISHED.value, Post.ig_media_id.is_not(None),
        Post.published_at >= now - timedelta(days=settings.metrics_max_age_days))).all()
    collected, skipped, errors = 0, 0, []
    for post in posts:
        last = db.scalar(select(MetricSnapshot).where(MetricSnapshot.post_id == post.id)
                         .order_by(MetricSnapshot.collected_at.desc()).limit(1))
        if last and (now - last.collected_at) < timedelta(hours=min_interval_hours):
            skipped += 1
            continue
        try:
            values, unavailable = meta.media_insights(post.ig_media_id)
        except MetaError as e:
            errors.append({"post_id": post.id, "error": str(e)[:300]})
            if e.retryable:
                break
            continue
        record_snapshot(db, post, values, unavailable, now)
        collected += 1
    db.commit()
    return {"collected": collected, "skipped_recent": skipped, "errors": errors, "eligible": len(posts)}


def record_snapshot(db: Session, post: Post, values: dict[str, int], unavailable: list[str],
                    now: datetime | None = None) -> MetricSnapshot:
    now = now or utcnow()
    age = max(0.0, (now - (post.published_at or now)).total_seconds() / 3600)
    snap = MetricSnapshot(post_id=post.id, collected_at=now, age_hours=round(age, 2), unavailable=list(unavailable),
                          raw=dict(values), **{k: values.get(k) for k in COUNT_FIELDS})
    db.add(snap)
    db.flush()
    return snap


# ------------------------------------------------------------- taxas
def interactions_of(snap: dict[str, Any]) -> tuple[int | None, str]:
    if snap.get("total_interactions") is not None:
        return snap["total_interactions"], "total_interactions"
    parts = [snap.get(k) for k in ("likes", "comments", "shares", "saved")]
    if all(p is not None for p in parts):
        return sum(parts), "likes+comments+shares+saved"
    return None, "indisponível"


def compute_rates(snap: dict[str, Any]) -> dict[str, float | None]:
    data = dict(snap)
    data["interactions"], _ = interactions_of(snap)
    out: dict[str, float | None] = {}
    for name, (num, den, _) in RATE_DEFS.items():
        n, d = data.get(num), data.get(den)
        out[name] = (n / d) if (n is not None and d) else None
    return out


def snapshot_dict(s: MetricSnapshot) -> dict[str, Any]:
    d = {k: getattr(s, k) for k in COUNT_FIELDS}
    d.update(age_hours=s.age_hours, collected_at=iso_utc(s.collected_at), unavailable=s.unavailable or [])
    return d


def analysis_snapshot(snaps: list[MetricSnapshot], target_hours: float) -> MetricSnapshot | None:
    """Snapshot mais próximo da idade-alvo, desde que a publicação tenha pelo menos 80% dela."""
    eligible = [s for s in snaps if s.age_hours >= 0.8 * target_hours]
    if not eligible:
        return None
    return min(eligible, key=lambda s: abs(s.age_hours - target_hours))


def hour_bucket(h: int) -> str:
    if 5 <= h < 12:
        return "manhã (05-12h)"
    if 12 <= h < 18:
        return "tarde (12-18h)"
    if 18 <= h < 23:
        return "noite (18-23h)"
    return "madrugada (23-05h)"


# ------------------------------------------------------------ dataset
def build_dataset(db: Session, settings: Settings | None = None, experiment_id: int | None = None,
                  target_hours: float | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    target = float(target_hours or settings.analysis_target_age_hours)
    q = select(Post).where(Post.status == PostStatus.PUBLISHED.value)
    if experiment_id is not None:
        q = q.where(Post.experiment_id == experiment_id)
    posts = db.scalars(q.order_by(Post.published_at)).all()
    campaigns = {c.id: c.slug for c in db.scalars(select(Campaign)).all()}
    from app.models import StockPhoto

    photo_prov = {pid: prov for pid, prov in db.execute(select(StockPhoto.id, StockPhoto.provider)).all()}
    rows, too_young, no_metrics = [], [], []
    for p in posts:
        snaps = db.scalars(select(MetricSnapshot).where(MetricSnapshot.post_id == p.id)).all()
        if not snaps:
            no_metrics.append(p.id)
            continue
        snap = analysis_snapshot(snaps, target)
        if snap is None:
            too_young.append(p.id)
            continue
        params = p.variant.params if p.variant else {}
        local = utc_to_local(p.published_at)
        sd = snapshot_dict(snap)
        inter, inter_src = interactions_of(sd)
        row = {
            "post_id": p.id, "variant_id": p.variant_id, "artwork_id": p.artwork_id,
            "experiment_id": p.experiment_id, "block": p.variant.block_key if p.variant else None,
            "ig_media_id": p.ig_media_id, "permalink": p.permalink,
            "published_at_local": local.isoformat() if local else None,
            "weekday": WEEKDAYS_PT[local.weekday()] if local else None,
            "hour_bucket": hour_bucket(local.hour) if local else None,
            "campaign": campaigns.get(p.campaign_id), "theme": p.theme_key, "content_kind": p.content_kind,
            "message_type": p.message_type, "audience": p.audience,
            "template": params.get("template"), "title_position": params.get("title_position"),
            "subtitle_position": params.get("subtitle_position"), "title_size": params.get("title_size"),
            "text_amount": (p.variant.factor_values or {}).get("text_amount") or _text_amount(params),
            "info_quantity": len(params.get("items") or []), "primary_color": params.get("primary_color"),
            "accent_color": params.get("accent_color"), "background": params.get("background"),
            "shapes": params.get("shapes"),
            "has_photo": "com foto" if p.photo_id else "sem foto",
            "photo_provider": photo_prov.get(p.photo_id, "(sem foto)"), "cta": params.get("cta") or "(sem CTA)",
            "factor_values": json.dumps((p.variant.factor_values or {}) if p.variant else {}, ensure_ascii=False),
            "snapshot_age_hours": snap.age_hours, "interactions": inter, "interactions_source": inter_src,
            **{k: sd[k] for k in COUNT_FIELDS},
            "unavailable_metrics": ",".join(sd["unavailable"]),
        }
        row.update({k: (round(v, 6) if v is not None else None) for k, v in compute_rates(sd).items()})
        if snap.age_hours > 2 * target:
            row["age_warning"] = f"primeira medição disponível com {snap.age_hours:.0f} h (alvo {target:.0f} h)"
        rows.append(row)
    return {"target_age_hours": target, "rows": rows, "excluded_too_young": too_young,
            "excluded_no_metrics": no_metrics, "published_total": len(posts)}


def _text_amount(params: dict) -> str:
    n = sum(len(params.get(k) or "") for k in ("title", "subtitle", "body")) + sum(len(i) for i in params.get("items") or [])
    return "curto" if n <= 90 else ("medio" if n <= 220 else "longo")


# ------------------------------------------------------------ estatística
def permutation_test(groups: dict[str, list[float]], permutations: int = 5000, seed: int = 20260101) -> float:
    """Teste de permutação para diferença entre médias de grupos (estatística: variância entre grupos ponderada)."""
    labels, values = [], []
    for g, vals in groups.items():
        labels += [g] * len(vals)
        values += vals
    if len(groups) < 2 or len(values) < 3:
        return 1.0

    def stat(vals: list[float]) -> float:
        grand = sum(vals) / len(vals)
        acc: dict[str, list[float]] = {}
        for lab, v in zip(labels, vals):
            acc.setdefault(lab, []).append(v)
        return sum(len(a) * (sum(a) / len(a) - grand) ** 2 for a in acc.values())

    observed = stat(values)
    rng = random.Random(seed)
    shuffled = list(values)
    extreme = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        if stat(shuffled) >= observed - 1e-15:
            extreme += 1
    return (extreme + 1) / (permutations + 1)


def adjust_by_block(rows: list[dict], metric: str, block_key: str = "block") -> list[dict]:
    """Subtrai a média do bloco (tema), removendo diferenças entre temas da comparação."""
    by_block: dict[Any, list[float]] = {}
    for r in rows:
        if r.get(metric) is not None:
            by_block.setdefault(r.get(block_key), []).append(r[metric])
    out = []
    for r in rows:
        if r.get(metric) is None:
            continue
        vals = by_block[r.get(block_key)]
        rr = dict(r)
        rr[metric + "_adj"] = r[metric] - sum(vals) / len(vals) if len(vals) > 1 else None
        out.append(rr)
    return out


def compare(rows: list[dict], dimension: str, metric: str = "engagement_rate", settings: Settings | None = None,
            adjust_blocks: bool = False) -> dict[str, Any]:
    settings = settings or get_settings()
    if metric not in RATE_DEFS and metric not in COUNT_FIELDS:
        raise ValueError(f"métrica desconhecida: {metric}")
    min_n = settings.analysis_min_samples_per_group
    value_key = metric
    if adjust_blocks:
        rows = adjust_by_block(rows, metric)
        value_key = metric + "_adj"
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get(value_key) is None:
            continue
        groups.setdefault(str(r.get(dimension)), []).append(r)
    out_groups = []
    for g, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        vals = [i[value_key] for i in items]
        reach = [i["reach"] for i in items if i.get("reach") is not None]
        out_groups.append({
            "group": g, "n": len(vals), "mean": round(statistics.fmean(vals), 6),
            "median": round(statistics.median(vals), 6),
            "stdev": round(statistics.stdev(vals), 6) if len(vals) > 1 else None,
            "min": round(min(vals), 6), "max": round(max(vals), 6),
            "total_reach": sum(reach) if reach else None,
            "mean_snapshot_age_hours": round(statistics.fmean(i["snapshot_age_hours"] for i in items), 1),
            "sufficient_sample": len(vals) >= min_n,
        })
    eligible = {g["group"]: [i[value_key] for i in groups[g["group"]]] for g in out_groups if g["sufficient_sample"]}
    test: dict[str, Any] = {"performed": False}
    if len(eligible) >= 2:
        p = permutation_test(eligible, settings.analysis_permutations)
        test = {"performed": True, "method": "teste de permutação (diferença entre médias)", "groups": list(eligible),
                "p_value": round(p, 4), "alpha": settings.analysis_alpha, "significant": p < settings.analysis_alpha}
    den_desc = RATE_DEFS[metric][2] if metric in RATE_DEFS else f"contagem absoluta de {metric}"
    return {
        "dimension": dimension, "dimension_label": DIMENSIONS.get(dimension, dimension), "metric": metric,
        "metric_definition": den_desc, "block_adjusted": adjust_blocks, "observations": sum(g["n"] for g in out_groups),
        "min_samples_per_group": min_n, "groups": out_groups, "test": test,
        "interpretation": interpret(out_groups, test, min_n, metric),
    }


def interpret(groups: list[dict], test: dict, min_n: int, metric: str) -> str:
    if not groups:
        return "Sem observações com esta métrica."
    if len(groups) < 2:
        return "Apenas um grupo observado; não há comparação possível."
    missing = [f"{g['group']} (faltam {min_n - g['n']})" for g in groups if not g["sufficient_sample"]]
    if not test.get("performed"):
        return ("Amostra insuficiente para qualquer conclusão. Continue coletando: " + ", ".join(missing) + ". "
                "As médias acima são apenas descritivas.")
    best = max((g for g in groups if g["sufficient_sample"]), key=lambda g: g["mean"])
    if test["significant"]:
        return (f"O grupo '{best['group']}' apresentou a maior média de {metric} e a diferença entre grupos é "
                f"improvável sob acaso (p={test['p_value']}). Trate como hipótese apoiada: replique em novos temas "
                f"e horários antes de adotar como padrão. Isto é associação, não prova de causalidade.")
    return (f"Não foi detectada diferença consistente entre os grupos (p={test['p_value']}). "
            "Pode não haver efeito ou a amostra ainda ser pequena para detectá-lo.")


def limitations_for(dataset: dict[str, Any], settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    lim = list(BASE_LIMITATIONS)
    n = len(dataset["rows"])
    if n < 2 * settings.analysis_min_samples_per_group:
        lim.insert(0, f"Apenas {n} publicação(ões) com métricas na idade padronizada; comparações são exploratórias.")
    if dataset["excluded_too_young"]:
        lim.append(f"{len(dataset['excluded_too_young'])} publicação(ões) ainda não atingiram "
                   f"{dataset['target_age_hours']:.0f} h e foram excluídas das comparações.")
    if dataset["excluded_no_metrics"]:
        lim.append(f"{len(dataset['excluded_no_metrics'])} publicação(ões) ainda sem métricas coletadas.")
    missing = {m for r in dataset["rows"] for m in (r.get("unavailable_metrics") or "").split(",") if m}
    if missing:
        lim.append("Métricas indisponíveis em pelo menos uma publicação: " + ", ".join(sorted(missing)) + ".")
    if any(r.get("age_warning") for r in dataset["rows"]):
        lim.append("Algumas publicações só têm medições bem depois da idade-alvo; isso infla suas contagens.")
    return lim


# ------------------------------------------------------------- resumos
def summary(db: Session, settings: Settings | None = None, metric: str = "engagement_rate") -> dict[str, Any]:
    settings = settings or get_settings()
    ds = build_dataset(db, settings)
    rows = ds["rows"]
    counts: dict[str, int] = {}
    for (st,) in db.execute(select(Post.status)):
        counts[st] = counts.get(st, 0) + 1
    totals = {k: sum(r[k] for r in rows if r.get(k) is not None) for k in ("reach", "likes", "comments", "shares", "saved")}
    pooled = None
    inter = [r for r in rows if r.get("interactions") is not None and r.get("reach")]
    if inter:
        pooled = sum(r["interactions"] for r in inter) / sum(r["reach"] for r in inter)
    comps = {d: compare(rows, d, metric, settings) for d in
             ("template", "title_position", "text_amount", "message_type", "theme", "weekday", "hour_bucket", "cta",
              "campaign")}
    return {
        "generated_at": iso_utc(utcnow()), "target_age_hours": ds["target_age_hours"], "posts_by_status": counts,
        "posts_analyzed": len(rows), "totals_at_target_age": totals,
        "pooled_engagement_rate": round(pooled, 6) if pooled is not None else None,
        "pooled_engagement_rate_definition": "soma das interações / soma do alcance, nas publicações analisadas",
        "metric": metric, "metric_definition": RATE_DEFS.get(metric, ("", "", metric))[2],
        "comparisons": comps, "recommendations": recommendations(comps), "limitations": limitations_for(ds, settings),
    }


def recommendations(comparisons: dict[str, dict]) -> list[str]:
    recs = []
    for dim, c in comparisons.items():
        if c["test"].get("performed") and c["test"]["significant"]:
            best = max((g for g in c["groups"] if g["sufficient_sample"]), key=lambda g: g["mean"])
            recs.append(f"{c['dimension_label']}: '{best['group']}' teve desempenho superior com significância; "
                        "planeje um experimento de replicação controlado antes de padronizar.")
    if not recs:
        recs.append("Ainda não há evidência suficiente para mudar a estratégia. Priorize experimentos com uma "
                    "variável por vez (ex.: posição do título), mesmo tema por bloco e mesmo horário de publicação.")
    return recs


# --------------------------------------------------------------- relatórios
def write_reports(db: Session, out_dir: Path | None = None, settings: Settings | None = None,
                  metric: str = "engagement_rate") -> dict[str, str]:
    settings = settings or get_settings()
    stamp = utc_to_local(utcnow()).strftime("%Y-%m-%d_%H%M%S")
    out = out_dir or (settings.generated_dir / "relatorios" / stamp)
    out.mkdir(parents=True, exist_ok=True)
    ds = build_dataset(db, settings)
    summ = summary(db, settings, metric)
    posts_csv = out / "publicacoes.csv"
    fields = sorted({k for r in ds["rows"] for k in r}) or ["post_id"]
    with open(posts_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in ds["rows"]:
            w.writerow(r)
    comp_csv = out / "comparacoes.csv"
    with open(comp_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dimensao", "grupo", "metrica", "n", "media", "mediana", "desvio", "alcance_total",
                    "idade_media_h", "amostra_suficiente", "p_valor"])
        for dim, c in summ["comparisons"].items():
            for g in c["groups"]:
                w.writerow([dim, g["group"], metric, g["n"], g["mean"], g["median"], g["stdev"], g["total_reach"],
                            g["mean_snapshot_age_hours"], g["sufficient_sample"], c["test"].get("p_value")])
    js = out / "resumo.json"
    js.write_text(json.dumps(summ, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"dir": str(out), "posts_csv": str(posts_csv), "comparisons_csv": str(comp_csv), "summary_json": str(js)}


def nan_safe(v):  # pragma: no cover - utilitário para serialização
    return None if isinstance(v, float) and math.isnan(v) else v