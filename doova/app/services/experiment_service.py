"""Experimentos A/B e fatoriais controlados.

Desenho:
* cada **bloco** é um tema da biblioteca: dentro do bloco, todas as variantes têm o
  mesmo tema, os mesmos textos (exceto quando o fator é texto) e, por padrão, a
  mesma legenda — apenas os fatores testados mudam;
* a ordem de publicação dentro de cada bloco é aleatorizada e todas saem no mesmo
  horário do dia, reduzindo a confusão com horário/dia;
* a análise compara variantes descontando a média de cada bloco (ajuste por tema).
"""
from __future__ import annotations

import itertools
import random
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Experiment, Post, PostStatus
from app.services import analytics_service as an
from app.services.content_service import ContentError, ContentService, PostInput, get_or_create_campaign
from app.services.library import get_theme, theme_overrides
from app.timeutil import tz

MAX_COMBINATIONS = 8

FACTORS: dict[str, dict[str, Any]] = {
    "title_position": {"param": "title_position", "allowed": ["top", "center", "bottom"]},
    "subtitle_position": {"param": "subtitle_position", "allowed": ["above_title", "below_title"]},
    "title_size": {"param": "title_size", "type": int},
    "primary_color": {"param": "primary_color", "type": str},
    "accent_color": {"param": "accent_color", "type": str},
    "background": {"param": "background", "allowed": ["gradient", "solid", "light", "dark"]},
    "shapes": {"param": "shapes", "allowed": [True, False]},
    "cta": {"param": "cta", "type": str},
    "template": {"param": None, "type": str},
    "text_amount": {"param": None, "allowed": ["curto", "medio", "longo"]},
    "info_quantity": {"param": None, "type": int},
    "message_type": {"param": None, "allowed": ["beneficio", "pergunta", "informacao", "chamada"]},
}


def validate_factors(factors: dict[str, list], template_keys: list[str]) -> None:
    if not factors:
        raise ContentError("informe ao menos um fator")
    for name, levels in factors.items():
        spec = FACTORS.get(name)
        if spec is None:
            raise ContentError(f"fator desconhecido: {name}; use um de {sorted(FACTORS)}")
        if not isinstance(levels, list) or len(levels) < 2 or len(set(map(str, levels))) != len(levels):
            raise ContentError(f"o fator {name} precisa de pelo menos dois níveis distintos")
        if "allowed" in spec and any(l not in spec["allowed"] for l in levels):
            raise ContentError(f"níveis inválidos para {name}: use {spec['allowed']}")
        if spec.get("type") is int and any(not isinstance(l, int) for l in levels):
            raise ContentError(f"o fator {name} aceita somente números inteiros")
        if name == "template" and any(l not in template_keys for l in levels):
            raise ContentError(f"template inexistente em {levels}")
        if name == "info_quantity" and any(not 0 <= l <= 4 for l in levels):
            raise ContentError("info_quantity deve estar entre 0 e 4")
    combos = 1
    for levels in factors.values():
        combos *= len(levels)
    if combos > MAX_COMBINATIONS:
        raise ContentError(f"{combos} combinações; o máximo é {MAX_COMBINATIONS}. Teste menos fatores por vez.")


def combinations(factors: dict[str, list]) -> list[dict[str, Any]]:
    names = sorted(factors)
    return [dict(zip(names, vals)) for vals in itertools.product(*(factors[n] for n in names))]


def apply_text_amount(texts: dict[str, Any], amount: str) -> dict[str, Any]:
    t = dict(texts)
    if amount == "curto":
        t.update(subtitle=None, body=None, items=[])
    elif amount == "medio":
        t.update(body=None, items=[])
    elif amount == "longo":
        if not (t.get("items") or t.get("body")):
            raise ContentError("o tema não tem itens/corpo para a versão 'longo'")
    return t


def variant_overrides(theme: dict[str, Any], base_params: dict[str, Any], combo: dict[str, Any]) -> tuple[str, dict]:
    template = combo.get("template") or base_params.get("template") or theme.get("template")
    texts = theme_overrides(theme)
    if "message_type" in combo:
        mv = (theme.get("message_variants") or {}).get(combo["message_type"])
        if not mv:
            raise ContentError(f"o tema não define 'message_variants.{combo['message_type']}'")
        texts.update(mv)
    if "text_amount" in combo:
        texts = apply_text_amount(texts, combo["text_amount"])
    if "info_quantity" in combo:
        items = list(texts.get("items") or [])
        if len(items) < combo["info_quantity"]:
            raise ContentError(f"o tema tem {len(items)} itens; não dá para testar {combo['info_quantity']}")
        texts["items"] = items[: combo["info_quantity"]]
    overrides = {**{k: v for k, v in base_params.items() if k != "template"}, **texts}
    for name, val in combo.items():
        param = FACTORS[name]["param"]
        if param:
            overrides[param] = val
    return template, overrides


class ExperimentService:
    def __init__(self, content: ContentService | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.content = content or ContentService(self.settings)

    def create(self, db: Session, *, name: str, factors: dict[str, list], theme_keys: list[str],
               template_key: str | None = None, base_params: dict | None = None, hypothesis: str = "",
               primary_metric: str = "engagement_rate", caption_strategy: str = "shared",
               campaign_slug: str | None = None, min_days_between_block_posts: int = 1) -> Experiment:
        validate_factors(factors, self.content.cfg.keys())
        if primary_metric not in an.RATE_DEFS:
            raise ContentError(f"métrica principal inválida; use uma de {sorted(an.RATE_DEFS)}")
        if caption_strategy not in ("shared", "per_post"):
            raise ContentError("caption_strategy deve ser 'shared' ou 'per_post'")
        if "message_type" in factors:
            caption_strategy = "per_post"  # a legenda precisa acompanhar a mensagem
        if not theme_keys:
            raise ContentError("informe ao menos um tema (bloco)")
        for k in theme_keys:
            try:
                get_theme(k)
            except KeyError as e:
                raise ContentError(str(e)) from e
        if template_key and template_key not in self.content.cfg.templates:
            raise ContentError(f"template desconhecido: {template_key}")
        if db.scalar(select(Experiment).where(Experiment.name == name)):
            raise ContentError("já existe um experimento com esse nome")
        campaign = get_or_create_campaign(db, campaign_slug or f"exp-{name}")
        design = "ab" if len(factors) == 1 and len(next(iter(factors.values()))) == 2 else "factorial"
        exp = Experiment(name=name, campaign_id=campaign.id, design=design, factors=factors,
                         template_key=template_key or "", base_params=base_params or {}, theme_keys=theme_keys,
                         hypothesis=hypothesis, primary_metric=primary_metric, caption_strategy=caption_strategy,
                         min_days_between_block_posts=min_days_between_block_posts)
        db.add(exp)
        db.commit()
        return exp

    def plan(self, exp: Experiment) -> dict[str, Any]:
        combos = combinations(exp.factors)
        n_blocks = len(exp.theme_keys)
        per_level = {f: n_blocks * len(combos) // len(levels) for f, levels in exp.factors.items()}
        min_n = self.settings.analysis_min_samples_per_group
        notes = []
        if any(v < min_n for v in per_level.values()):
            need = max(-(-min_n * len(lv) // len(combos)) for lv in exp.factors.values())
            notes.append(f"Com {n_blocks} bloco(s) haverá menos de {min_n} publicações por nível; "
                         f"use pelo menos {need} temas (blocos) para permitir teste estatístico.")
        return {"combinations": combos, "blocks": exp.theme_keys, "posts_total": len(combos) * n_blocks,
                "posts_per_level": per_level, "notes": notes}

    def generate_posts(self, db: Session, exp: Experiment) -> list[Post]:
        existing = db.scalars(select(Post).where(Post.experiment_id == exp.id)).all()
        if existing:
            raise ContentError("este experimento já gerou publicações")
        base = dict(exp.base_params or {})
        if exp.template_key:
            base["template"] = exp.template_key
        created: list[Post] = []
        campaign_slug = None
        if exp.campaign_id:
            from app.models import Campaign
            c = db.get(Campaign, exp.campaign_id)
            campaign_slug = c.slug if c else None
        # valida todas as combinações antes de criar qualquer publicação
        planned = []
        for block in exp.theme_keys:
            theme = get_theme(block)
            for combo in combinations(exp.factors):
                planned.append((block, theme, combo, *variant_overrides(theme, base, combo)))
        for block in exp.theme_keys:
            shared_caption = None
            for b, theme, combo, template, overrides in planned:
                if b != block:
                    continue
                data = PostInput(
                    template_key=template, overrides=overrides, topic=theme["topic"], audience=theme["audience"],
                    objective=theme.get("objective", ""), theme_key=block, campaign_slug=campaign_slug,
                    content_kind=theme.get("content_kind", "institutional"),
                    message_type=combo.get("message_type") or theme.get("message_type"),
                    experiment_id=exp.id, block_key=block,
                    factor_values=combo, shared_caption_id=shared_caption,
                    generate_caption=True,
                )
                post = self.content.create_post(db, data)
                created.append(post)
                if exp.caption_strategy == "shared" and shared_caption is None and post.caption_id:
                    shared_caption = post.caption_id
        return created

    def schedule(self, db: Session, exp: Experiment, start: date, slot: str | None = None,
                 seed: int | None = None) -> dict[str, Any]:
        posts = db.scalars(select(Post).where(Post.experiment_id == exp.id)).all()
        pending = [p.id for p in posts if p.status != PostStatus.APPROVED.value]
        if pending:
            raise ContentError(f"aprove todas as publicações antes de agendar; pendentes: {pending}")
        h, m = (slot.split(":") if slot else (str(self.settings.slot_times()[0][0]), str(self.settings.slot_times()[0][1])))
        rng = random.Random(seed if seed is not None else exp.id)
        weekdays = self.settings.weekdays()
        day = start
        plan = []
        for block in exp.theme_keys:
            block_posts = [p for p in posts if p.theme_key == block]
            rng.shuffle(block_posts)  # ordem aleatória dentro do bloco
            for p in block_posts:
                while day.weekday() not in weekdays:
                    day += timedelta(days=1)
                when = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=tz())
                self.content.schedule(db, p, when)
                plan.append({"post_id": p.id, "block": block, "factors": p.variant.factor_values,
                             "scheduled_local": when.isoformat()})
                day += timedelta(days=max(1, exp.min_days_between_block_posts))
        return {"scheduled": plan}

    def results(self, db: Session, exp: Experiment) -> dict[str, Any]:
        ds = an.build_dataset(db, self.settings, experiment_id=exp.id)
        rows = ds["rows"]
        for r in rows:
            fv = __import__("json").loads(r["factor_values"])
            for f in exp.factors:
                r["factor_" + f] = str(fv.get(f))
        factor_results = {}
        for f in exp.factors:
            factor_results[f] = {
                "raw": an.compare(rows, "factor_" + f, exp.primary_metric, self.settings),
                "block_adjusted": an.compare(rows, "factor_" + f, exp.primary_metric, self.settings, adjust_blocks=True),
            }
        posts = db.scalars(select(Post).where(Post.experiment_id == exp.id)).all()
        status_counts: dict[str, int] = {}
        for p in posts:
            status_counts[p.status] = status_counts.get(p.status, 0) + 1
        return {
            "experiment": {"id": exp.id, "name": exp.name, "design": exp.design, "factors": exp.factors,
                           "blocks": exp.theme_keys, "hypothesis": exp.hypothesis,
                           "primary_metric": exp.primary_metric,
                           "metric_definition": an.RATE_DEFS[exp.primary_metric][2]},
            "posts_by_status": status_counts, "observations": len(rows), "factors": factor_results,
            "limitations": an.limitations_for(ds, self.settings) + [
                "O ajuste por bloco subtrai a média de cada tema; ele reduz, mas não elimina, efeitos de dia e de distribuição.",
            ],
        }