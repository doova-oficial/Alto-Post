"""Escolha automática do horário de publicação, aprendida com os resultados.

Fase de aprendizado: enquanto algum horário candidato (POSTING_CANDIDATE_SLOTS) tiver
menos de TIMING_MIN_SAMPLES publicações medidas, as publicações são distribuídas em
rodízio, começando pelos horários menos testados.

Fase de aproveitamento: os horários com maior taxa de engajamento média recebem as
publicações. Uma fração TIMING_EXPLORE_RATE dos dias continua testando outro horário,
para perceber se o comportamento do público mudou.

A medida é a mesma das análises (engajamento / alcance na medição de ~72 h), então só
publicações reais com métricas da Meta ensinam o sistema. É uma heurística de
otimização, não uma prova estatística de que um horário causa mais engajamento.
"""
from __future__ import annotations

import random
import statistics
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import utcnow
from app.models import Post, PostStatus
from app.timeutil import utc_to_local

Slot = tuple[int, int]
TRY_STATES = [PostStatus.PUBLISHED.value, PostStatus.SIMULATED.value, PostStatus.SCHEDULED.value,
              PostStatus.PUBLISHING.value, PostStatus.PROCESSING.value, PostStatus.VERIFYING.value]


def fmt(slot: Slot) -> str:
    return f"{slot[0]:02d}:{slot[1]:02d}"


def candidate_slots(s: Settings) -> list[Slot]:
    return s.candidate_slot_times() if s.posting_strategy == "adaptive" else s.slot_times()


def slot_of(local_dt: datetime, slots: list[Slot], window_minutes: int) -> Slot | None:
    if not slots:
        return None
    mins = local_dt.hour * 60 + local_dt.minute
    best = min(slots, key=lambda sl: abs(sl[0] * 60 + sl[1] - mins))
    return best if abs(best[0] * 60 + best[1] - mins) <= window_minutes else None


def slot_stats(db: Session, s: Settings | None = None, metric: str = "engagement_rate") -> dict[Slot, dict[str, Any]]:
    s = s or get_settings()
    from app.services.analytics_service import build_dataset

    slots = candidate_slots(s)
    stats: dict[Slot, dict[str, Any]] = {sl: {"slot": fmt(sl), "tries": 0, "values": []} for sl in slots}
    for row in build_dataset(db, s)["rows"]:
        v, ts = row.get(metric), row.get("published_at_local")
        if v is None or not ts:
            continue
        sl = slot_of(datetime.fromisoformat(ts), slots, s.timing_window_minutes)
        if sl is not None:
            stats[sl]["values"].append(float(v))
    stamp = func.coalesce(Post.published_at, Post.scheduled_at)
    for (t,) in db.execute(select(stamp).where(Post.status.in_(TRY_STATES))).all():
        if t is None:
            continue
        sl = slot_of(utc_to_local(t), slots, s.timing_window_minutes)
        if sl is not None:
            stats[sl]["tries"] += 1
    for st in stats.values():
        st["measured"] = len(st["values"])
        st["mean"] = statistics.fmean(st["values"]) if st["values"] else None
    return stats


def learning(stats: dict[Slot, dict[str, Any]], s: Settings) -> bool:
    return any(st["measured"] < s.timing_min_samples for st in stats.values())


def ranked_slots(stats: dict[Slot, dict[str, Any]], s: Settings, day: date) -> list[Slot]:
    """Horários do dia em ordem de prioridade."""
    slots = list(stats)
    if not slots:
        return []
    rng = random.Random(day.toordinal())  # estável no mesmo dia
    if learning(stats, s):
        jitter = {sl: rng.random() for sl in slots}
        return sorted(slots, key=lambda sl: (stats[sl]["measured"] >= s.timing_min_samples,
                                             stats[sl]["tries"], jitter[sl]))
    ranked = sorted(slots, key=lambda sl: -(stats[sl]["mean"] or 0.0))
    k = max(1, min(s.max_posts_per_day, len(ranked)))
    if len(ranked) > k and rng.random() < s.timing_explore_rate:
        pick = rng.choice(ranked[k:])
        ranked.remove(pick)
        ranked.insert(k - 1, pick)  # um dos horários do dia vai para teste
    return ranked


def timing_report(db: Session, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    out: dict[str, Any] = {"strategy": s.posting_strategy, "metric": "engagement_rate",
                           "min_samples_per_slot": s.timing_min_samples, "explore_rate": s.timing_explore_rate}
    stats = slot_stats(db, s)
    out["phase"] = ("aprendizado" if learning(stats, s) else "aproveitamento") if s.posting_strategy == "adaptive" \
        else "horários fixos"
    order = ranked_slots(stats, s, utc_to_local(utcnow()).date()) if s.posting_strategy == "adaptive" \
        else list(stats)
    out["slots"] = [{"slot": stats[sl]["slot"], "tries": stats[sl]["tries"], "measured": stats[sl]["measured"],
                     "mean_engagement_rate": round(stats[sl]["mean"], 4) if stats[sl]["mean"] is not None else None,
                     "priority_today": i + 1} for i, sl in enumerate(order)]
    queue = db.scalars(select(Post).where(Post.status == PostStatus.SCHEDULED.value)
                       .order_by(Post.scheduled_at).limit(20)).all()
    out["queue"] = [{"post_id": p.id, "at_local": utc_to_local(p.scheduled_at).isoformat() if p.scheduled_at else None,
                     "kind": p.content_kind, "topic": p.topic} for p in queue]
    from app.services.content_service import ContentService

    nxt = ContentService(s).next_free_slot(db)
    out["next_free_slot_local"] = utc_to_local(nxt).isoformat() if nxt else None
    return out