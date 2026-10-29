"""Backups do banco de dados.

* SQLite: cópia consistente com a API de backup do SQLite (funciona com o banco em uso),
  mantendo os ``BACKUP_KEEP`` arquivos mais recentes.
* PostgreSQL: use os backups gerenciados do provedor (Render Postgres pago faz backups
  diários com recuperação pontual) e/ou ``pg_dump`` agendado — ver docs/OPERACAO.md.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import Settings, get_settings
from app.database import utcnow


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    p = url.split("sqlite:///", 1)[1]
    return None if p in ("", ":memory:") else Path(p)


def run_backup(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    src = sqlite_path(settings.database_url)
    if src is None:
        return {"done": False, "reason": "banco não é SQLite; use backups do PostgreSQL (ver docs/OPERACAO.md)"}
    if not src.exists():
        return {"done": False, "reason": f"arquivo {src} não existe"}
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.backup_dir / f"doova-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
        s.backup(d)
    backups = sorted(settings.backup_dir.glob("doova-*.db"))
    for old in backups[: max(0, len(backups) - settings.backup_keep)]:
        old.unlink(missing_ok=True)
    return {"done": True, "file": str(dest), "kept": min(len(backups), settings.backup_keep)}