"""Dependências compartilhadas pelas rotas."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Post
from app.services.content_service import ContentError, StateError

_content_factory = None
_publisher_factory = None
_http_factory = None


def set_factories(content=None, publisher=None, http=None) -> None:
    """Permite injetar serviços (usado nos testes)."""
    global _content_factory, _publisher_factory, _http_factory
    _content_factory = content
    _publisher_factory = publisher
    _http_factory = http


def http_client():
    """Cliente HTTP para buscas externas (fotos, tendências). None = o serviço cria o seu."""
    return _http_factory() if _http_factory else None


def content_service():
    if _content_factory:
        return _content_factory()
    from app.services.content_service import ContentService

    return ContentService()


def publisher():
    if _publisher_factory:
        return _publisher_factory()
    from app.services.publisher import Publisher

    return Publisher()


def get_post_or_404(db: Session, post_id: str) -> Post:
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "publicação não encontrada")
    return post


def http_error(e: Exception) -> HTTPException:
    if isinstance(e, StateError):
        return HTTPException(409, str(e))
    if isinstance(e, ContentError):
        return HTTPException(422, str(e))
    return HTTPException(500, "erro interno")