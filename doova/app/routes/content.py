"""Artes, legendas e publicações."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Artwork, Post, PostEvent, PostStatus
from app.routes.deps import content_service, get_post_or_404, http_error, publisher
from app.schemas import (
    ApproveRequest,
    ArtRequest,
    CaptionPreviewRequest,
    CaptionUpdate,
    PostCreate,
    RegenerateRequest,
    RejectRequest,
    ScheduleRequest,
    post_out,
)
from app.security import AdminDep, GenerationLimit
from app.services.content_service import AUDIENCES, ContentError, PostInput
from app.services.groq_service import CaptionGenerationError, CaptionRequest
from app.services.library import get_theme, theme_overrides
from app.timeutil import iso_utc

router = APIRouter(prefix="/api", dependencies=[AdminDep])

PENDING = [PostStatus.DRAFT.value, PostStatus.CAPTION_FAILED.value, PostStatus.NEEDS_REVIEW.value,
           PostStatus.PENDING_APPROVAL.value]


@router.get("/templates", tags=["artes"], summary="Listar templates disponíveis")
def list_templates():
    return content_service().cfg.describe()


@router.post("/art", tags=["artes"], summary="Gerar uma arte avulsa", dependencies=[GenerationLimit])
def generate_art(req: ArtRequest, db: Session = Depends(get_db)):
    try:
        art = content_service().create_art(db, req.template, req.params.overrides())
    except ContentError as e:
        raise http_error(e)
    return {"artwork_id": art.id, "variant_id": art.variant_id, "png_path": art.png_path,
            "image_url": f"/api/art/{art.id}/image", "layout_report": art.layout_report}


@router.get("/art/{artwork_id}/image", tags=["artes"], summary="Baixar o PNG de uma arte",
            responses={200: {"content": {"image/png": {}}}})
def art_image(artwork_id: str, db: Session = Depends(get_db)):
    art = db.get(Artwork, artwork_id)
    if art is None:
        raise HTTPException(404, "arte não encontrada")
    from pathlib import Path

    if Path(art.png_path).is_file():
        return FileResponse(art.png_path, media_type="image/png")
    if art.jpg_bytes:
        from fastapi import Response

        return Response(art.jpg_bytes, media_type="image/jpeg")
    raise HTTPException(404, "arquivo da arte não está disponível")


@router.post("/captions/preview", tags=["legendas"], summary="Gerar uma legenda (sem criar publicação)",
             dependencies=[GenerationLimit])
def caption_preview(req: CaptionPreviewRequest):
    cs = content_service()
    try:
        res = cs.captions.generate(CaptionRequest(
            topic=req.topic, audience=AUDIENCES[req.audience], objective=req.objective, art_text=req.art_text,
            template=req.template, cta=req.cta, message_type=req.message_type, extra_context=req.extra_context))
    except CaptionGenerationError as e:
        raise HTTPException(503 if e.retryable else 502, f"legenda não gerada: {e}")
    return {"caption": res.text, "valid": res.check.is_valid, "needs_review": res.check.needs_review,
            "issues": res.check.issues, "model": res.model}


@router.post("/posts", tags=["publicações"], summary="Criar uma publicação (arte + legenda)",
             dependencies=[GenerationLimit], status_code=201)
def create_post(req: PostCreate, db: Session = Depends(get_db)):
    overrides: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    if req.theme_key:
        try:
            theme = get_theme(req.theme_key)
        except KeyError as e:
            raise HTTPException(422, str(e))
        overrides = theme_overrides(theme)
        defaults = theme
    template = req.template or defaults.get("template")
    if not template:
        raise HTTPException(422, "informe 'template' ou 'theme_key'")
    overrides.update(req.params.overrides())
    data = PostInput(
        template_key=template, overrides=overrides, topic=req.topic or defaults.get("topic", ""),
        audience=req.audience or defaults.get("audience", "ambos"),
        objective=req.objective or defaults.get("objective", "apresentar o Doova e atrair os primeiros usuários reais"),
        theme_key=req.theme_key, campaign_slug=req.campaign,
        content_kind=req.content_kind or defaults.get("content_kind", "institutional"),
        message_type=req.message_type or defaults.get("message_type"), caption=req.caption,
        generate_caption=req.generate_caption, extra_context=req.extra_context, scheduled_at=req.scheduled_at,
        verified_opportunity_id=req.verified_opportunity_id, alt_text=req.alt_text)
    try:
        post = content_service().create_post(db, data)
    except ContentError as e:
        raise http_error(e)
    return post_out(post, detail=True)


@router.get("/posts", tags=["publicações"], summary="Listar publicações (filtro por estado)")
def list_posts(status: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db)):
    q = select(Post)
    if status:
        q = q.where(Post.status.in_(status.split(",")))
    posts = db.scalars(q.order_by(Post.created_at.desc()).limit(limit).offset(offset)).all()
    return [post_out(p) for p in posts]


@router.get("/posts/pending", tags=["publicações"], summary="Listar publicações pendentes de revisão/aprovação")
def pending(db: Session = Depends(get_db)):
    posts = db.scalars(select(Post).where(Post.status.in_(PENDING)).order_by(Post.created_at)).all()
    return [post_out(p) for p in posts]


@router.get("/posts/history", tags=["publicações"], summary="Histórico de publicações e eventos")
def history(limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    events = db.scalars(select(PostEvent).order_by(PostEvent.at.desc()).limit(limit)).all()
    done = db.scalars(select(Post).where(Post.status.in_([PostStatus.PUBLISHED.value, PostStatus.SIMULATED.value,
                                                          PostStatus.FAILED.value]))
                      .order_by(Post.updated_at.desc()).limit(limit)).all()
    return {"posts": [post_out(p) for p in done],
            "events": [{"post_id": e.post_id, "at": iso_utc(e.at), "from": e.from_status, "to": e.to_status,
                        "message": e.message} for e in events]}


@router.get("/posts/{post_id}", tags=["publicações"], summary="Detalhes de uma publicação")
def get_post(post_id: str, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    out = post_out(p, detail=True)
    out["events"] = [{"at": iso_utc(e.at), "from": e.from_status, "to": e.to_status, "message": e.message}
                     for e in db.scalars(select(PostEvent).where(PostEvent.post_id == p.id).order_by(PostEvent.id))]
    return out


@router.put("/posts/{post_id}/caption", tags=["publicações"], summary="Definir a legenda manualmente")
def set_caption(post_id: str, req: CaptionUpdate, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().set_manual_caption(db, p, req.caption)
    except ContentError as e:
        raise http_error(e)
    return post_out(p, detail=True)


@router.post("/posts/{post_id}/caption/regenerate", tags=["publicações"], summary="Gerar a legenda novamente",
             dependencies=[GenerationLimit])
def regenerate(post_id: str, req: RegenerateRequest, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().regenerate_caption(db, p, req.extra_context)
    except ContentError as e:
        raise http_error(e)
    return post_out(p, detail=True)


@router.post("/posts/{post_id}/approve", tags=["publicações"], summary="Aprovar uma publicação")
def approve(post_id: str, req: ApproveRequest, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().approve(db, p, req.approved_by)
    except ContentError as e:
        raise http_error(e)
    return post_out(p)


@router.post("/posts/{post_id}/reject", tags=["publicações"], summary="Rejeitar uma publicação")
def reject(post_id: str, req: RejectRequest, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().reject(db, p, req.reason, req.rejected_by)
    except ContentError as e:
        raise http_error(e)
    return post_out(p)


@router.post("/posts/{post_id}/cancel", tags=["publicações"], summary="Cancelar uma publicação")
def cancel(post_id: str, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().cancel(db, p)
    except ContentError as e:
        raise http_error(e)
    return post_out(p)


@router.post("/posts/{post_id}/schedule", tags=["publicações"], summary="Agendar uma publicação")
def schedule(post_id: str, req: ScheduleRequest, db: Session = Depends(get_db)):
    p = get_post_or_404(db, post_id)
    try:
        content_service().schedule(db, p, req.scheduled_at)
    except ContentError as e:
        raise http_error(e)
    return post_out(p)


@router.post("/posts/{post_id}/publish-now", tags=["publicações"],
             summary="Publicar imediatamente (somente aprovadas; respeita simulação, pausa e limites)",
             dependencies=[GenerationLimit])
def publish_now(post_id: str, db: Session = Depends(get_db)):
    get_post_or_404(db, post_id)
    from app.services.publisher import PublisherBusy

    try:
        res = publisher().publish_now(post_id)
    except PublisherBusy as e:
        raise HTTPException(409, str(e))
    code = {"skipped": 409, "paused": 409, "missing": 404}.get(res.get("outcome"))
    if code:
        raise HTTPException(code, res.get("message") or res.get("outcome"))
    return res