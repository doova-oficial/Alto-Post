"""Fotos de bancos gratuitos e assuntos do momento."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import StockPhoto
from app.routes.deps import content_service, http_client
from app.security import AdminDep, GenerationLimit
from app.services import image_search as isr
from app.timeutil import iso_utc

router = APIRouter(prefix="/api", dependencies=[AdminDep])
Provider = Literal["openverse", "pixabay"]


class PhotoImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    provider_id: str = Field(min_length=1, max_length=120)


class PhotoFind(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_en: str = Field(min_length=2, max_length=100, description="termo genérico em inglês (Openverse)")
    query_pt: str | None = Field(default=None, max_length=100, description="termo em português (Pixabay)")


class TrendRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = Field(default=True, description="roda mesmo com TREND_AUTO_ENABLED=false")


def _err(e: isr.ImageSearchError) -> HTTPException:
    return HTTPException(502 if e.upstream else 422, str(e))


def photo_out(p: StockPhoto) -> dict:
    return {"id": p.id, "provider": p.provider, "provider_id": p.provider_id, "query": p.query, "title": p.title,
            "creator": p.creator, "source": p.source, "license": p.license, "license_version": p.license_version,
            "license_url": p.license_url, "landing_url": p.landing_url, "width": p.width, "height": p.height,
            "credit_on_art": isr.short_credit(p), "credit_in_caption": isr.caption_credit(p),
            "image_url": f"/api/photos/{p.id}/image", "created_at": iso_utc(p.created_at)}


@router.get("/photos/search", tags=["fotos"], summary="Buscar fotos no Openverse ou no Pixabay (não importa)")
def photo_search(q: str = Query(..., min_length=2, max_length=100), provider: Provider = "openverse",
                 db: Session = Depends(get_db)):
    try:
        items = isr.search(db, q, provider, client=http_client())
    except isr.ImageSearchError as e:
        raise _err(e)
    return {"query": q, "provider": provider, "source_notice": "Imagens via " + provider.capitalize(),
            "results": [c.public() for c in items]}


@router.post("/photos/import", tags=["fotos"], summary="Baixar e guardar uma foto (com licença e crédito)",
             dependencies=[GenerationLimit], status_code=201)
def photo_import(req: PhotoImport, db: Session = Depends(get_db)):
    try:
        cand = isr.get_candidate(db, req.provider, req.provider_id, client=http_client())
        return photo_out(isr.import_photo(db, cand, client=http_client()))
    except isr.ImageSearchError as e:
        raise _err(e)


@router.post("/photos/find", tags=["fotos"], summary="Procurar e importar a primeira foto adequada",
             dependencies=[GenerationLimit])
def photo_find(req: PhotoFind, db: Session = Depends(get_db)):
    photo, log = isr.find_photo(db, req.query_en, req.query_pt, client=http_client())
    if photo is None:
        raise HTTPException(404, {"message": "nenhuma foto adequada encontrada", "log": log})
    return {"photo": photo_out(photo), "log": log}


@router.get("/photos", tags=["fotos"], summary="Fotos já importadas")
def photo_list(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.scalars(select(StockPhoto).order_by(StockPhoto.created_at.desc()).limit(limit)).all()
    return [photo_out(p) for p in rows]


@router.get("/photos/{photo_id}", tags=["fotos"], summary="Detalhes de uma foto importada")
def photo_detail(photo_id: str, db: Session = Depends(get_db)):
    p = db.get(StockPhoto, photo_id)
    if p is None:
        raise HTTPException(404, "foto não encontrada")
    return photo_out(p)


@router.get("/photos/{photo_id}/image", tags=["fotos"], summary="Baixar a foto importada (JPEG)")
def photo_image(photo_id: str, db: Session = Depends(get_db)):
    p = db.get(StockPhoto, photo_id)
    if p is None:
        raise HTTPException(404, "foto não encontrada")
    return Response(p.jpg_bytes, media_type="image/jpeg")


@router.get("/trends/evaluate", tags=["conteúdo"], summary="Avaliar os assuntos em alta (sem criar nada)")
def trends_evaluate(db: Session = Depends(get_db)):
    from app.services.trend_content import run_trend_auto

    return run_trend_auto(db, get_settings(), client=http_client(), create=False)


@router.post("/trends/run", tags=["conteúdo"], summary="Criar publicações com foto a partir dos assuntos em alta",
             dependencies=[GenerationLimit])
def trends_run(req: TrendRun = TrendRun(), db: Session = Depends(get_db)):
    from app.services.trend_content import run_trend_auto

    return run_trend_auto(db, get_settings(), force=req.force, client=http_client(), content=content_service())