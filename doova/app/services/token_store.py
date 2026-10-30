"""Armazenamento do token da Meta renovado automaticamente.

O token inicial vem de ``META_ACCESS_TOKEN``. Quando o sistema o renova, o novo
valor é guardado criptografado (Fernet, chave ``TOKEN_ENCRYPTION_KEY``) no banco,
pois variáveis de ambiente não podem ser alteradas pela própria aplicação.
Sem chave de criptografia, a renovação automática fica desativada e o token
precisa ser renovado manualmente (ver docs/TOKENS.md).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.config import Settings, get_settings
from app.database import session_scope, utcnow

logger = logging.getLogger(__name__)
TOKEN_NAME = "meta_access_token"


def _fernet(settings: Settings):
    if not settings.token_encryption_key or not settings.token_encryption_key.get_secret_value():
        return None
    from cryptography.fernet import Fernet

    return Fernet(settings.token_encryption_key.get_secret_value().encode())


def current_access_token(settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    # Unified Login recebe o SUAT por variável de ambiente. Não deixe um token
    # antigo armazenado por um fluxo anterior sobrescrever o SUAT configurado.
    if settings.meta_login_type == "unified":
        if settings.meta_access_token and settings.meta_access_token.get_secret_value():
            return settings.meta_access_token.get_secret_value()
        return None
    f = _fernet(settings)
    if f is not None:
        try:
            from app.models import StoredToken

            with session_scope() as db:
                row = db.query(StoredToken).filter_by(name=TOKEN_NAME).one_or_none()
                if row is not None and (row.expires_at is None or row.expires_at > utcnow()):
                    return f.decrypt(row.ciphertext.encode()).decode()
        except Exception as e:  # banco indisponível ou chave trocada
            logger.warning("Não foi possível ler token armazenado: %s", type(e).__name__)
    if settings.meta_access_token and settings.meta_access_token.get_secret_value():
        return settings.meta_access_token.get_secret_value()
    return None


def token_status(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    from app.models import StoredToken

    with session_scope() as db:
        row = db.query(StoredToken).filter_by(name=TOKEN_NAME).one_or_none()
        if settings.meta_login_type == "unified":
            return {
                "source": "env" if settings.meta_access_token else "missing",
                "expires_at": None,
                "refreshed_at": None,
                "auto_refresh": False,
                "token_type": "system_user_access_token",
            }
        return {
            "source": "database" if row else ("env" if settings.meta_access_token else "missing"),
            "expires_at": row.expires_at.isoformat() + "Z" if row and row.expires_at else None,
            "refreshed_at": row.refreshed_at.isoformat() + "Z" if row else None,
            "auto_refresh": _fernet(settings) is not None,
            "token_type": "long_lived_user_token",
        }


def refresh_token(settings: Settings | None = None, meta=None) -> dict:
    """Renova o token se a criptografia estiver configurada. Retorna o novo estado."""
    settings = settings or get_settings()
    f = _fernet(settings)
    if settings.meta_login_type == "unified":
        return {"refreshed": False, "reason": "Unified Login usa SUAT; não renovar pelo fluxo de long-lived token. Substitua o token no painel da Meta quando necessário."}
    if f is None:
        return {"refreshed": False, "reason": "TOKEN_ENCRYPTION_KEY não configurada; renove manualmente"}
    tok = current_access_token(settings)
    if not tok:
        return {"refreshed": False, "reason": "nenhum token configurado"}
    from app.models import StoredToken
    from app.services.meta_service import MetaService

    meta = meta or MetaService(settings)
    new_tok, expires_in = meta.refresh_long_lived_token(tok)
    expires_at = utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None
    with session_scope() as db:
        row = db.query(StoredToken).filter_by(name=TOKEN_NAME).one_or_none()
        if row is None:
            row = StoredToken(name=TOKEN_NAME, ciphertext="")
            db.add(row)
        row.ciphertext = f.encrypt(new_tok.encode()).decode()
        row.expires_at = expires_at
        row.refreshed_at = utcnow()
    logger.info("Token da Meta renovado; expira em %s", expires_at)
    return {"refreshed": True, "expires_at": expires_at.isoformat() + "Z" if expires_at else None}