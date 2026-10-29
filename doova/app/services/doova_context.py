"""Contexto real do Doova e restrições de linguagem.

Este arquivo é a única fonte de "fatos" que a IA pode usar. Ajuste-o quando
o produto mudar. Funcionalidades só devem entrar em ``REAL_FEATURES`` depois
de existirem de fato no aplicativo.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CONTEXT_FILE = Path(__file__).resolve().parent.parent / "content" / "doova_context.yaml"


def load_context() -> dict:
    with open(CONTEXT_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def context_as_text(ctx: dict | None = None) -> str:
    ctx = ctx or load_context()
    lines = [
        f"Marca: {ctx['brand']}",
        f"O que é: {ctx['description']}",
        f"Região inicial: {ctx['region']}",
        f"Estágio: {ctx['stage']}",
        "Funcionalidades confirmadas (use somente estas):",
    ]
    feats = ctx.get("real_features") or []
    lines += [f"- {f}" for f in feats] if feats else ["- (nenhuma funcionalidade específica confirmada; fale apenas da proposta geral)"]
    if ctx.get("links"):
        lines.append("Links oficiais: " + ", ".join(ctx["links"]))
    lines.append("Fatos que NÃO devem ser afirmados: " + "; ".join(ctx.get("never_claim", [])))
    return "\n".join(lines)