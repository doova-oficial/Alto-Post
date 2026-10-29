"""Linha de comando: ``python -m app.cli <comando>``.

Comandos:
  init-db            cria as tabelas
  tick               executa as tarefas agendadas vencidas (para cron)
  run-job NOME       executa uma tarefa (publish, maintenance, metrics, token_refresh, backup)
  seed-launch        cria as publicações do plano de lançamento (content/themes.yaml)
  trends-run         assuntos do momento: busca, triagem, foto e publicações para revisão
  photo-search TERMO busca fotos no Openverse/Pixabay (não importa)
  render-samples     gera uma amostra de cada template em generated/amostras
  pause / resume     pausa ou retoma a publicação automática
  report             gera relatórios CSV/JSON
  gen-token          gera um ADMIN_API_TOKEN aleatório
  gen-key            gera uma TOKEN_ENCRYPTION_KEY (Fernet)
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys


def _init():
    from app.config import get_settings
    from app.database import create_all, init_engine
    from app.logging_config import setup_logging

    s = get_settings()
    setup_logging(s.log_level, secrets_provider=lambda: get_settings().secret_values())
    init_engine(s.database_url)
    create_all()
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init-db", "tick", "render-samples", "resume", "report", "gen-token", "gen-key"):
        sub.add_parser(name)
    rj = sub.add_parser("run-job")
    rj.add_argument("job")
    sl = sub.add_parser("seed-launch")
    sl.add_argument("--campaign", default="lancamento")
    sl.add_argument("--no-caption", action="store_true", help="não chama a Groq; legendas ficam para depois")
    tr = sub.add_parser("trends-run")
    tr.add_argument("--force", action="store_true", help="roda mesmo com TREND_AUTO_ENABLED=false")
    ps = sub.add_parser("photo-search")
    ps.add_argument("query")
    ps.add_argument("--provider", default="openverse", choices=["openverse", "pixabay"])
    pa = sub.add_parser("pause")
    pa.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    if args.cmd == "gen-token":
        print(secrets.token_urlsafe(36))
        return 0
    if args.cmd == "gen-key":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    if args.cmd == "render-samples":
        import runpy
        from pathlib import Path

        runpy.run_path(str(Path(__file__).resolve().parent.parent / "scripts" / "render_samples.py"), run_name="__main__")
        return 0

    _init()
    from app.database import session_scope

    if args.cmd == "init-db":
        print("tabelas criadas")
    elif args.cmd == "tick":
        from app.services.scheduler import tick

        print(json.dumps(tick(), ensure_ascii=False, default=str, indent=2))
    elif args.cmd == "run-job":
        from app.services.scheduler import JOBS, run_job

        if args.job not in JOBS:
            print(f"tarefa desconhecida; use {sorted(JOBS)}", file=sys.stderr)
            return 2
        print(json.dumps(run_job(args.job), ensure_ascii=False, default=str, indent=2))
    elif args.cmd in ("pause", "resume"):
        from app.services.state import set_paused

        with session_scope() as db:
            print(set_paused(db, args.cmd == "pause", by="cli", reason=getattr(args, "reason", "")))
    elif args.cmd == "report":
        from app.services.analytics_service import write_reports

        with session_scope() as db:
            print(json.dumps(write_reports(db), ensure_ascii=False, indent=2))
    elif args.cmd == "trends-run":
        from app.services.trend_content import run_trend_auto

        with session_scope() as db:
            print(json.dumps(run_trend_auto(db, force=args.force), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "photo-search":
        from app.services.image_search import ImageSearchError, search

        with session_scope() as db:
            try:
                items = search(db, args.query, args.provider)
            except ImageSearchError as e:
                print(f"erro: {e}", file=sys.stderr)
                return 1
            for c in items[:10]:
                print(f"{c.provider}:{c.provider_id} {c.width}x{c.height} {c.license} | {c.creator} | {c.landing_url}")
    elif args.cmd == "seed-launch":
        from app.services.content_service import ContentError, ContentService, PostInput
        from app.services.library import launch_plan, theme_overrides

        cs = ContentService()
        with session_scope() as db:
            from sqlalchemy import select

            from app.models import Post

            for key, t in launch_plan():
                if db.scalar(select(Post).where(Post.theme_key == key)):
                    print(f"{key}: já existe, ignorado")
                    continue
                try:
                    p = cs.create_post(db, PostInput(
                        template_key=t["template"], overrides=theme_overrides(t), topic=t["topic"],
                        audience=t["audience"], objective=t.get("objective", ""), theme_key=key,
                        campaign_slug=args.campaign, content_kind=t.get("content_kind", "institutional"),
                        message_type=t.get("message_type"), generate_caption=not args.no_caption))
                    print(f"{key}: {p.id} [{p.status}] {p.artwork.png_path}")
                except ContentError as e:
                    print(f"{key}: ERRO {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())