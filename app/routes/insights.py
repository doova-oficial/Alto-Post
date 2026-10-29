"""Métricas, experimentos e sugestões de conteúdo."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Experiment, MetricSnapshot, VerifiedOpportunity
from app.routes.deps import content_service, get_post_or_404, http_error
from app.schemas import ExperimentCreate, ExperimentSchedule, OpportunityCreate, post_out
from app.security import AdminDep, GenerationLimit
from app.services import analytics_service as an
from app.services.content_service import ContentError
from app.services.experiment_service import ExperimentService
from app.timeutil import iso_utc, local_to_utc

router = APIRouter(prefix="/api", dependencies=[AdminDep])


# ------------------------------------------------------------- métricas
@router.get("/metrics/summary", tags=["métricas"], summary="Resumo legível dos resultados")
def metrics_summary(metric: str = "engagement_rate", db: Session = Depends(get_db)):
    if metric not in an.RATE_DEFS:
        raise HTTPException(422, f"métrica inválida; use uma de {sorted(an.RATE_DEFS)}")
    return an.summary(db, metric=metric)


@router.get("/metrics/compare", tags=["métricas"], summary="Comparar grupos por uma dimensão")
def metrics_compare(dimension: str = Query(..., description="ex.: template, title_position, weekday"),
                    metric: str = "engagement_rate", db: Session = Depends(get_db)):
    if dimension not in an.DIMENSIONS:
        raise HTTPException(422, f"dimensão inválida; use uma de {sorted(an.DIMENSIONS)}")
    if metric not in an.RATE_DEFS and metric not in an.COUNT_FIELDS:
        raise HTTPException(422, "métrica inválida")
    ds = an.build_dataset(db)
    out = an.compare(ds["rows"], dimension, metric)
    out["limitations"] = an.limitations_for(ds)
    return out


@router.get("/metrics/posts/{post_id}", tags=["métricas"], summary="Medições de uma publicação")
def post_metrics(post_id: str, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    snaps = db.scalars(select(MetricSnapshot).where(MetricSnapshot.post_id == p.id)
                       .order_by(MetricSnapshot.collected_at)).all()
    return {"post": post_out(p), "snapshots": [{**an.snapshot_dict(s), **an.compute_rates(an.snapshot_dict(s))}
                                               for s in snaps],
            "rate_definitions": {k: v[2] for k, v in an.RATE_DEFS.items()}}


@router.post("/metrics/collect", tags=["métricas"], summary="Coletar métricas agora (API da Meta)",
             dependencies=[GenerationLimit])
def collect(db: Session = Depends(get_db)):
    if not get_settings().meta_configured:
        raise HTTPException(409, "Meta não configurada")
    return an.collect_metrics(db)


@router.post("/metrics/reports", tags=["métricas"], summary="Gerar relatórios CSV e JSON")
def reports(metric: str = "engagement_rate", db: Session = Depends(get_db)):
    return an.write_reports(db, metric=metric)


# ---------------------------------------------------------- experimentos
def _exp(db: Session, exp_id: int) -> Experiment:
    exp = db.get(Experiment, exp_id)
    if exp is None:
        raise HTTPException(404, "experimento não encontrado")
    return exp


@router.post("/experiments", tags=["experimentos"], summary="Criar experimento A/B ou fatorial", status_code=201)
def create_experiment(req: ExperimentCreate, db: Session = Depends(get_db)):
    svc = ExperimentService(content_service())
    try:
        exp = svc.create(db, name=req.name, factors=req.factors, theme_keys=req.theme_keys, template_key=req.template,
                         base_params=req.base_params.overrides(), hypothesis=req.hypothesis,
                         primary_metric=req.primary_metric, caption_strategy=req.caption_strategy,
                         min_days_between_block_posts=req.min_days_between_block_posts)
    except ContentError as e:
        raise http_error(e)
    return {"id": exp.id, "name": exp.name, "design": exp.design, "plan": svc.plan(exp)}


@router.get("/experiments", tags=["experimentos"], summary="Listar experimentos")
def list_experiments(db: Session = Depends(get_db)):
    return [{"id": e.id, "name": e.name, "design": e.design, "factors": e.factors, "blocks": e.theme_keys,
             "status": e.status, "created_at": iso_utc(e.created_at)} for e in db.scalars(select(Experiment))]


@router.post("/experiments/{exp_id}/generate", tags=["experimentos"], summary="Gerar as publicações do experimento",
             dependencies=[GenerationLimit])
def generate_experiment(exp_id: int, db: Session = Depends(get_db)):
    exp = _exp(db, exp_id)
    try:
        posts = ExperimentService(content_service()).generate_posts(db, exp)
    except ContentError as e:
        raise http_error(e)
    return [post_out(p) for p in posts]


@router.post("/experiments/{exp_id}/schedule", tags=["experimentos"],
             summary="Agendar o experimento (ordem aleatória por bloco, mesmo horário)")
def schedule_experiment(exp_id: int, req: ExperimentSchedule, db: Session = Depends(get_db)):
    exp = _exp(db, exp_id)
    try:
        return ExperimentService(content_service()).schedule(db, exp, date.fromisoformat(req.start_date), req.slot,
                                                             req.seed)
    except ContentError as e:
        raise http_error(e)


@router.get("/experiments/{exp_id}/results", tags=["experimentos"], summary="Comparar resultados do experimento")
def experiment_results(exp_id: int, db: Session = Depends(get_db)):
    return ExperimentService(content_service()).results(db, _exp(db, exp_id))


# ----------------------------------------------------------- sugestões
@router.get("/suggestions", tags=["conteúdo"], summary="Sugestões de temas (datas, histórico, FAQ, tendências)")
def get_suggestions(include_trends: bool = True, limit: int = Query(10, ge=1, le=30), db: Session = Depends(get_db)):
    from app.services.trends_service import suggestions

    return suggestions(db, include_trends=include_trends, limit=limit)


@router.get("/library", tags=["conteúdo"], summary="Biblioteca de temas e plano de lançamento")
def library_view():
    from app.services import library

    return {"themes": library.themes(), "launch_plan": [k for k, _ in library.launch_plan()], "faq": library.faq()}


# ------------------------------------------------------- oportunidades
@router.post("/opportunities", tags=["conteúdo"], summary="Registrar oportunidade de trabalho VERIFICADA",
             status_code=201)
def create_opportunity(req: OpportunityCreate, db: Session = Depends(get_db)):
    opp = VerifiedOpportunity(title=req.title, description=req.description, source=req.source,
                              verified_by=req.verified_by,
                              expires_at=local_to_utc(req.expires_at) if req.expires_at else None)
    db.add(opp)
    db.commit()
    return {"id": opp.id, "title": opp.title, "expires_at": iso_utc(opp.expires_at)}


@router.post("/opportunities/{opp_id}/deactivate", tags=["conteúdo"], summary="Desativar oportunidade")
def deactivate_opportunity(opp_id: int, db: Session = Depends(get_db)):
    opp = db.get(VerifiedOpportunity, opp_id)
    if opp is None:
        raise HTTPException(404, "oportunidade não encontrada")
    opp.active = False
    db.commit()
    return {"id": opp.id, "active": False}