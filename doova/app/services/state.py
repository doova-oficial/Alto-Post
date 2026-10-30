"""Estado persistente da aplicação e travas distribuídas simples (via banco).

As travas evitam que a mesma tarefa rode duas vezes ao mesmo tempo — por exemplo,
após uma reinicialização, com duas instâncias ativas durante um deploy, ou quando
um cron externo e o agendador interno disparam juntos.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import session_scope, utcnow
from app.models import AppState, JobLock

PAUSE_KEY = "automation"
_OWNER = None


def instance_owner() -> str:
    global _OWNER
    if _OWNER is None:
        _OWNER = get_settings().instance_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    return _OWNER


def get_state(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(AppState, key)
    return row.value if row is not None else default


def set_state(db: Session, key: str, value: Any) -> None:
    row = db.get(AppState, key)
    if row is None:
        db.add(AppState(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def is_paused(db: Session) -> bool:
    return bool((get_state(db, PAUSE_KEY, {}) or {}).get("paused", False))


def set_paused(db: Session, paused: bool, by: str = "admin", reason: str = "") -> dict:
    value = {"paused": paused, "by": by, "reason": reason, "at": utcnow().isoformat() + "Z"}
    set_state(db, PAUSE_KEY, value)
    return value


def acquire_lock(name: str, ttl_seconds: int, owner: str | None = None) -> bool:
    owner = owner or instance_owner()
    now = utcnow()
    exp = now + timedelta(seconds=ttl_seconds)
    with session_scope() as db:
        res = db.execute(
            update(JobLock)
            .where(JobLock.name == name, or_(JobLock.expires_at < now, JobLock.owner == owner))
            .values(owner=owner, expires_at=exp)
        )
        if res.rowcount:
            return True
        exists = db.get(JobLock, name)
        if exists is not None:
            return False
    try:
        with session_scope() as db:
            db.add(JobLock(name=name, owner=owner, expires_at=exp))
        return True
    except IntegrityError:
        return False


def release_lock(name: str, owner: str | None = None) -> None:
    owner = owner or instance_owner()
    with session_scope() as db:
        db.execute(delete(JobLock).where(JobLock.name == name, JobLock.owner == owner))