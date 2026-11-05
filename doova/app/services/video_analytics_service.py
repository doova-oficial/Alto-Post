"""Coleta de métricas e análise de resultados dos Reels (vídeo).

Espelha ``analytics_service.py``, mas para ``VideoAsset``/``VideoMetricSnapshot`` em vez
de ``Post``/``MetricSnapshot`` — os dois pipelines de publicação (imagem e Reel) são
independentes (ver ``video_service.publish_reel``), então as métricas também são.

Reaproveita de ``analytics_service`` tudo que já é genérico (taxas, teste de permutação,
ajuste por bloco) para não duplicar a parte estatística.

Dimensões analisadas: transição, faixa de duração, variante criativa, quantidade de
imagens geradas no vídeo, tema, dia da semana, faixa de horário e quantos Reels foram
publicados no mesmo dia (para ver se publicar mais de um por dia atrapalha o alcance
de cada um).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import VideoAsset, VideoMetricSnapshot
from app.services.analytics_service import (
    RATE_DEFS,
    WEEKDAYS_PT,
    adjust_by_block,
    hour_bucket,
    interactions_of,
    permutation_test,
)
from app.services.meta_service import MetaError, MetaService, REEL_METRICS
from app.timeutil import iso_utc, utc_to_local

VIDEO_COUNT_FIELDS = ["reach", "views", "likes", "comments", "shares", "saved", "total_interactions",
                      "profile_visits", "profile_activity", "follows", "avg_watch_time_ms", "total_watch_time_ms"]

VIDEO_DIMENSIONS: dict[str, str] = {
    "hook": "Gancho",
    "transition": "Transição entre cenas",
    "duration_bucket": "Duração do vídeo",
    "creative_variant": "Variante criativa",
    "generated_image_count": "Quantidade de imagens geradas no vídeo",
    "segment_count": "Quantidade de segmentos",
    "segment_mix": "Composição dos segmentos",
    "has_generated_card": "Usa card gerado",
    "render_mode": "Renderizador",
    "theme_key": "Tema",
    "weekday": "Dia da semana",
    "hour_bucket": "Faixa de horário",
    "daily_video_count_bucket": "Quantos Reels saíram no mesmo dia",
}

BASE_LIMITATIONS = [
    "As métricas vêm da API oficial da Meta; 'views', 'ig_reels_avg_watch_time' e "
    "'ig_reels_video_view_total_time' são específicas de Reels e podem não existir para mídias "
    "antigas ou contas sem esse recurso habilitado.",
    "O alcance de um Reel também depende do algoritmo de distribuição, do tema e do horário; "
    "diferenças entre transições ou durações podem ter outras causas além do próprio criativo.",
    "A Meta informa que os dados podem atrasar até 48 h; métricas indisponíveis são tratadas como "
    "ausentes, não como zero.",
    "Os testes de permutação indicam se uma diferença é improvável sob acaso, considerando estas "
    "amostras; não provam causalidade. Resultados devem ser replicados antes de virar regra.",
]


# ------------------------------------------------------------ coleta
def collect_video_metrics(db: Session, meta: MetaService | None = None, now: datetime | None = None,
                          settings: Settings | None = None, min_interval_hours: float = 1.0) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or utcnow()
    meta = meta or MetaService(settings)
    assets = db.scalars(select(VideoAsset).where(
        VideoAsset.created_at >= now - timedelta(days=settings.metrics_max_age_days))).all()
    collected, skipped, errors = 0, 0, []
    for asset in assets:
        media_id = (asset.metadata_json or {}).get("instagram_media_id")
        if not media_id:
            continue
        last = db.scalar(select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == asset.id)
                         .order_by(VideoMetricSnapshot.collected_at.desc()).limit(1))
        if last and (now - last.collected_at) < timedelta(hours=min_interval_hours):
            skipped += 1
            continue
        try:
            values, unavailable = meta.media_insights(media_id, metrics=REEL_METRICS)
        except MetaError as e:
            errors.append({"video_id": asset.id, "error": str(e)[:300]})
            if e.retryable:
                break
            continue
        record_video_snapshot(db, asset, values, unavailable, now)
        collected += 1
    db.commit()
    return {"collected": collected, "skipped_recent": skipped, "errors": errors, "eligible": len(assets)}


def record_video_snapshot(db: Session, asset: VideoAsset, values: dict[str, int], unavailable: list[str],
                          now: datetime | None = None) -> VideoMetricSnapshot:
    now = now or utcnow()
    published_at_raw = (asset.metadata_json or {}).get("instagram_published_at")
    published_at = _parse_meta_timestamp(published_at_raw) or asset.created_at
    age = max(0.0, (now - published_at).total_seconds() / 3600)
    snap = VideoMetricSnapshot(
        video_id=asset.id, collected_at=now, age_hours=round(age, 2), unavailable=list(unavailable), raw=dict(values),
        reach=values.get("reach"), views=values.get("views"), likes=values.get("likes"),
        comments=values.get("comments"), shares=values.get("shares"), saved=values.get("saved"),
        total_interactions=values.get("total_interactions"), profile_visits=values.get("profile_visits"),
        profile_activity=values.get("profile_activity"), follows=values.get("follows"),
        avg_watch_time_ms=values.get("ig_reels_avg_watch_time"),
        total_watch_time_ms=values.get("ig_reels_video_view_total_time"),
    )
    db.add(snap)
    db.flush()
    return snap


def _parse_meta_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def video_snapshot_dict(s: VideoMetricSnapshot) -> dict[str, Any]:
    d = {k: getattr(s, k) for k in VIDEO_COUNT_FIELDS}
    d.update(age_hours=s.age_hours, collected_at=iso_utc(s.collected_at), unavailable=s.unavailable or [])
    return d


def compute_video_rates(snap: dict[str, Any]) -> dict[str, float | None]:
    data = dict(snap)
    data["interactions"], _ = interactions_of(snap)
    out: dict[str, float | None] = {}
    for name, (num, den, _) in RATE_DEFS.items():
        if den not in data and den != "reach":
            continue
        n, d = data.get(num), data.get(den)
        out[name] = (n / d) if (n is not None and d) else None
    watch_ms, duration_s = data.get("avg_watch_time_ms"), snap.get("duration_seconds")
    out["watch_through_rate"] = (watch_ms / 1000 / duration_s) if (watch_ms is not None and duration_s) else None
    return out


def analysis_snapshot(snaps: list[VideoMetricSnapshot], target_hours: float) -> VideoMetricSnapshot | None:
    eligible = [s for s in snaps if s.age_hours >= 0.8 * target_hours]
    if not eligible:
        return None
    return min(eligible, key=lambda s: abs(s.age_hours - target_hours))


def _duration_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "desconhecida"
    if seconds <= 10:
        return "curto (≤10s)"
    if seconds <= 20:
        return "médio (11-20s)"
    return "longo (>20s)"


# ------------------------------------------------------------ dataset
def build_video_dataset(db: Session, settings: Settings | None = None, target_hours: float | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    target = float(target_hours or settings.analysis_target_age_hours)
    assets = db.scalars(select(VideoAsset).order_by(VideoAsset.created_at)).all()
    published = [a for a in assets if (a.metadata_json or {}).get("instagram_media_id")]

    # quantos Reels foram publicados em cada dia local, para a dimensão "daily_video_count_bucket"
    per_day: dict[Any, int] = {}
    for a in published:
        pub_at = _parse_meta_timestamp((a.metadata_json or {}).get("instagram_published_at")) or a.created_at
        local = utc_to_local(pub_at)
        if local:
            per_day[local.date()] = per_day.get(local.date(), 0) + 1

    rows, too_young, no_metrics = [], [], []
    for asset in published:
        snaps = db.scalars(select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == asset.id)).all()
        if not snaps:
            no_metrics.append(asset.id)
            continue
        snap = analysis_snapshot(snaps, target)
        if snap is None:
            too_young.append(asset.id)
            continue
        metadata = asset.metadata_json or {}
        segments = metadata.get("segments") or []
        segment_counts: dict[str, int] = {}
        for segment in segments:
            kind = str(segment.get("kind") or "unknown")
            segment_counts[kind] = segment_counts.get(kind, 0) + 1
        segment_mix = " + ".join(f"{kind}: {count}" for kind, count in sorted(segment_counts.items()))
        generated_count = metadata.get("generated_image_count")
        has_generated_card = (
            "sim" if any(item.get("kind") == "generated_image" for item in segments)
            else "não" if segments
            else "sim" if isinstance(generated_count, (int, float)) and generated_count > 0
            else "não" if isinstance(generated_count, (int, float))
            else None
        )
        pub_at = _parse_meta_timestamp(metadata.get("instagram_published_at")) or asset.created_at
        local = utc_to_local(pub_at)
        day_count = per_day.get(local.date(), 1) if local else 1
        sd = video_snapshot_dict(snap)
        sd["duration_seconds"] = metadata.get("duration_seconds")
        inter, inter_src = interactions_of(sd)
        row = {
            "video_id": asset.id, "ig_media_id": metadata.get("instagram_media_id"),
            "permalink": metadata.get("instagram_permalink"),
            "published_at_local": local.isoformat() if local else None,
            "weekday": WEEKDAYS_PT[local.weekday()] if local else None,
            "hour_bucket": hour_bucket(local.hour) if local else None,
            "theme_key": asset.theme_key, "trend_term": asset.trend_term, "hook": asset.hook,
            "subtitle_text": asset.subtitle_text,
            "transition": metadata.get("transition"), "creative_variant": metadata.get("creative_variant"),
            "duration_seconds": metadata.get("duration_seconds"),
            "duration_bucket": _duration_bucket(metadata.get("duration_seconds")),
            "generated_image_count": metadata.get("generated_image_count"),
            "segment_count": len(segments) if segments else None,
            "segment_mix": segment_mix or None,
            "has_generated_card": has_generated_card,
            "render_mode": "Android (Media3)" if metadata.get("android_renderer") == "media3" else "Servidor",
            "daily_video_count_bucket": "1" if day_count <= 1 else ("2" if day_count == 2 else "3+"),
            "snapshot_age_hours": snap.age_hours, "interactions": inter, "interactions_source": inter_src,
            **{k: sd[k] for k in VIDEO_COUNT_FIELDS},
            "unavailable_metrics": ",".join(sd["unavailable"]),
        }
        row.update({k: (round(v, 6) if v is not None else None) for k, v in compute_video_rates(sd | {"duration_seconds": metadata.get("duration_seconds")}).items()})
        if snap.age_hours > 2 * target:
            row["age_warning"] = f"primeira medição disponível com {snap.age_hours:.0f} h (alvo {target:.0f} h)"
        rows.append(row)
    return {"target_age_hours": target, "rows": rows, "excluded_too_young": too_young,
            "excluded_no_metrics": no_metrics, "published_total": len(published), "generated_total": len(assets)}


def dashboard_data(db: Session, settings: Settings | None = None) -> dict[str, Any]:
    """Reels publicados com as características do criativo e métricas disponíveis."""
    settings = settings or get_settings()
    assets = db.scalars(select(VideoAsset).order_by(VideoAsset.created_at.desc())).all()
    reels = []
    for asset in assets:
        metadata = asset.metadata_json or {}
        media_id = metadata.get("instagram_media_id")
        if not media_id:
            continue
        snapshots = db.scalars(select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == asset.id)
                               .order_by(VideoMetricSnapshot.collected_at)).all()
        latest = snapshots[-1] if snapshots else None
        comparable = analysis_snapshot(snapshots, settings.analysis_target_age_hours)

        def metric_values(snapshot: VideoMetricSnapshot | None) -> tuple[dict, dict, list]:
            if snapshot is None:
                return {}, {}, []
            values = video_snapshot_dict(snapshot)
            values["duration_seconds"] = metadata.get("duration_seconds") or asset.duration_seconds
            return ({key: values.get(key) for key in VIDEO_COUNT_FIELDS},
                    compute_video_rates(values), values["unavailable"])

        metrics, rates, unavailable = metric_values(latest)
        analysis_metrics, analysis_rates, analysis_unavailable = metric_values(comparable)
        segments = metadata.get("segments") or []
        segment_counts: dict[str, int] = {}
        for segment in segments:
            kind = str(segment.get("kind") or "unknown")
            segment_counts[kind] = segment_counts.get(kind, 0) + 1
        segment_mix = " + ".join(f"{kind}: {count}" for kind, count in sorted(segment_counts.items()))
        published_at = _parse_meta_timestamp(metadata.get("instagram_published_at")) or asset.created_at
        local = utc_to_local(published_at)
        reels.append({
            "video_id": asset.id,
            "permalink": metadata.get("instagram_permalink"),
            "published_at_local": local.isoformat() if local else None,
            "theme_key": asset.theme_key,
            "trend_term": asset.trend_term,
            "hook": asset.hook,
            "duration_seconds": metadata.get("duration_seconds") or asset.duration_seconds,
            "transition": metadata.get("transition"),
            "creative_variant": metadata.get("creative_variant"),
            "generated_image_count": metadata.get("generated_image_count"),
            "segment_count": len(segments) if segments else None,
            "segment_mix": segment_mix or None,
            "segments": segments,
            "render_mode": "Android (Media3)" if metadata.get("android_renderer") == "media3" else "Servidor",
            "metrics": metrics,
            "rates": rates,
            "analysis_metrics": analysis_metrics,
            "analysis_rates": analysis_rates,
            "snapshot_age_hours": latest.age_hours if latest is not None else None,
            "analysis_snapshot_age_hours": comparable.age_hours if comparable is not None else None,
            "metric_basis": "idade-alvo" if comparable is not None else "sem amostra na idade-alvo",
            "comparable": comparable is not None,
            "unavailable_metrics": unavailable,
            "analysis_unavailable_metrics": analysis_unavailable,
        })
    return {
        "generated_at": iso_utc(utcnow()),
        "target_age_hours": settings.analysis_target_age_hours,
        "reels": reels,
        "summary": summary(db, settings),
        "dimensions": VIDEO_DIMENSIONS,
    }


# ------------------------------------------------------------- comparação
def compare_video(rows: list[dict], dimension: str, metric: str = "engagement_rate",
                  settings: Settings | None = None, adjust_blocks: bool = False) -> dict[str, Any]:
    import statistics

    settings = settings or get_settings()
    if metric not in RATE_DEFS and metric not in VIDEO_COUNT_FIELDS and metric != "watch_through_rate":
        raise ValueError(f"métrica desconhecida: {metric}")
    min_n = settings.analysis_min_samples_per_group
    value_key = metric
    if adjust_blocks:
        rows = adjust_by_block(rows, metric, block_key="theme_key")
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
    den_desc = RATE_DEFS[metric][2] if metric in RATE_DEFS else (
        "tempo médio assistido / duração do vídeo" if metric == "watch_through_rate" else f"contagem absoluta de {metric}")
    return {
        "dimension": dimension, "dimension_label": VIDEO_DIMENSIONS.get(dimension, dimension), "metric": metric,
        "metric_definition": den_desc, "block_adjusted": adjust_blocks, "observations": sum(g["n"] for g in out_groups),
        "min_samples_per_group": min_n, "groups": out_groups, "test": test,
        "interpretation": _interpret(out_groups, test, min_n, metric),
    }


def _interpret(groups: list[dict], test: dict, min_n: int, metric: str) -> str:
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
        lim.insert(0, f"Apenas {n} Reel(s) com métricas na idade padronizada; comparações são exploratórias.")
    if dataset["excluded_too_young"]:
        lim.append(f"{len(dataset['excluded_too_young'])} Reel(s) ainda não atingiram "
                   f"{dataset['target_age_hours']:.0f} h e foram excluídos das comparações.")
    if dataset["excluded_no_metrics"]:
        lim.append(f"{len(dataset['excluded_no_metrics'])} Reel(s) publicado(s) ainda sem métricas coletadas.")
    missing = {m for r in dataset["rows"] for m in (r.get("unavailable_metrics") or "").split(",") if m}
    if missing:
        lim.append("Métricas indisponíveis em pelo menos um Reel: " + ", ".join(sorted(missing)) + ".")
    return lim


# ------------------------------------------------------------- resumos
def summary(db: Session, settings: Settings | None = None, metric: str = "engagement_rate") -> dict[str, Any]:
    settings = settings or get_settings()
    ds = build_video_dataset(db, settings)
    rows = ds["rows"]
    totals = {k: sum(r[k] for r in rows if r.get(k) is not None) for k in ("reach", "likes", "comments", "shares", "saved")}
    pooled = None
    inter = [r for r in rows if r.get("interactions") is not None and r.get("reach")]
    if inter:
        pooled = sum(r["interactions"] for r in inter) / sum(r["reach"] for r in inter)
    comps = {d: compare_video(rows, d, metric, settings) for d in VIDEO_DIMENSIONS}
    return {
        "generated_at": iso_utc(utcnow()), "target_age_hours": ds["target_age_hours"],
        "videos_generated": ds["generated_total"], "videos_published": ds["published_total"],
        "videos_analyzed": len(rows), "totals_at_target_age": totals,
        "pooled_engagement_rate": round(pooled, 6) if pooled is not None else None,
        "pooled_engagement_rate_definition": "soma das interações / soma do alcance, nos Reels analisados",
        "metric": metric, "metric_definition": RATE_DEFS.get(metric, ("", "", metric))[2],
        "comparisons": comps, "recommendations": _recommendations(comps), "limitations": limitations_for(ds, settings),
    }


def _recommendations(comparisons: dict[str, dict]) -> list[str]:
    recs = []
    for dim, c in comparisons.items():
        if c["test"].get("performed") and c["test"]["significant"]:
            best = max((g for g in c["groups"] if g["sufficient_sample"]), key=lambda g: g["mean"])
            recs.append(f"{c['dimension_label']}: '{best['group']}' teve desempenho superior com significância; "
                        "replique em novos temas antes de padronizar.")
    if not recs:
        recs.append("Ainda não há evidência suficiente para mudar transição, duração ou cadência de vídeos. "
                    "Priorize coletar mais Reels publicados antes de comparar.")
    return recs


def write_reports(db: Session, out_dir: Path | None = None, settings: Settings | None = None,
                  metric: str = "engagement_rate") -> dict[str, str]:
    settings = settings or get_settings()
    stamp = utc_to_local(utcnow()).strftime("%Y-%m-%d_%H%M%S")
    out = out_dir or (settings.generated_dir / "relatorios" / f"videos_{stamp}")
    out.mkdir(parents=True, exist_ok=True)
    ds = build_video_dataset(db, settings)
    summ = summary(db, settings, metric)
    videos_csv = out / "videos.csv"
    fields = sorted({k for r in ds["rows"] for k in r}) or ["video_id"]
    with open(videos_csv, "w", newline="", encoding="utf-8") as f:
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
    return {"dir": str(out), "videos_csv": str(videos_csv), "comparisons_csv": str(comp_csv), "summary_json": str(js)}
