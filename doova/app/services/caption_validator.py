"""Validação e limpeza das legendas.

* Remove emojis (e seus modificadores) sem afetar letras acentuadas,
  cedilha, aspas tipográficas, travessões ou outros sinais do português.
* Limita a quantidade de hashtags.
* Detecta promessas proibidas (renda garantida etc.), placeholders e
  saídas incompletas — esses casos vão para revisão humana.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Faixas Unicode de emojis e pictogramas. Nenhuma delas contém letras latinas.
_EMOJI_RANGES = [
    (0x1F000, 0x1FAFF),  # mahjong, cartas, símbolos e pictogramas, emoticons, transporte, suplementares
    (0x2600, 0x27BF),    # símbolos diversos e dingbats
    (0x2300, 0x23FF),    # técnicos (relógios, ampulheta...)
    (0x2B00, 0x2BFF),    # setas e estrelas (⭐ ⬆)
    (0x1F1E6, 0x1F1FF),  # indicadores regionais (bandeiras)
    (0xE0020, 0xE007F),  # tags (bandeiras de subdivisões)
    (0x2190, 0x21FF),    # setas
    (0x25A0, 0x25FF),    # formas geométricas (▶ ◀)
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3299),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x24C2, 0x24C2),
    (0x203C, 0x203C), (0x2049, 0x2049),
    (0x00A9, 0x00A9), (0x00AE, 0x00AE),
]
_JOINERS = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}  # ZWJ, seletores de variação, keycap
_SKIN = (0x1F3FB, 0x1F3FF)

_EMOJI_CLASS = "".join(
    (re.escape(chr(a)) if a == b else f"{re.escape(chr(a))}-{re.escape(chr(b))}") for a, b in _EMOJI_RANGES
)
_EMOJI_RE = re.compile(f"[{_EMOJI_CLASS}]")
# Keycap: dígito/#/* seguido de FE0F? + 20E3 — remove a sequência inteira
_KEYCAP_RE = re.compile("[0-9#*]\ufe0f?\u20e3")
_HASHTAG_RE = re.compile(r"(?<![\w&])#([0-9A-Za-zÀ-ÖØ-öø-ÿ_]+)")

BANNED_PATTERNS = [
    (re.compile(r"renda\s+(extra\s+)?garantida", re.I), "promessa de renda garantida"),
    (re.compile(r"ganho[s]?\s+garantido", re.I), "promessa de ganho garantido"),
    (re.compile(r"dinheiro\s+f[aá]cil", re.I), "promessa de dinheiro fácil"),
    (re.compile(r"garantimos\s+(que\s+)?(voc[eê]\s+)?(vai\s+)?(ganhar|conseguir|fechar)", re.I), "garantia de resultado"),
    (re.compile(r"\b(milhares|centenas)\s+de\s+(usu[aá]rios|clientes|prestadores|vagas)", re.I), "número de usuários sem respaldo"),
    (re.compile(r"\b\d[\d\.]*\s*(mil\s+)?(usu[aá]rios|clientes|downloads|prestadores)\b", re.I), "estatística de usuários sem respaldo"),
    (re.compile(r"\b(vagas?\s+abertas?|oportunidade\s+imperd[ií]vel)\b", re.I), "vaga/oportunidade sem verificação"),
    (re.compile(r"\b100\s*%\s*(seguro|garantido|confi[aá]vel)", re.I), "garantia absoluta"),
    (re.compile(r"\b(o\s+melhor|o\s+maior|o\s+n[uú]mero\s+1)\s+(app|aplicativo|marketplace)", re.I), "superlativo sem respaldo"),
]
PLACEHOLDER_RE = re.compile(r"(\[[^\]]{2,40}\]|\{[^}]{2,40}\}|<[^>]{2,40}>|lorem ipsum|insira aqui|xxx)", re.I)
PREAMBLE_RE = re.compile(r"^\s*(claro[,!.]|aqui est[aá]|segue (a )?legenda|legenda\s*:)", re.I)


def _is_emoji_cp(cp: int) -> bool:
    if cp in _JOINERS or _SKIN[0] <= cp <= _SKIN[1]:
        return True
    return False


def strip_emojis(text: str) -> str:
    """Remove emojis, preservando acentuação e pontuação do português."""
    if not text:
        return text
    text = unicodedata.normalize("NFC", text)
    text = _KEYCAP_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = "".join(ch for ch in text if not _is_emoji_cp(ord(ch)))
    # limpa espaços duplicados deixados pela remoção, preservando quebras de linha
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def contains_emoji(text: str) -> bool:
    return strip_emojis(text) != unicodedata.normalize("NFC", text).strip() and bool(
        _EMOJI_RE.search(text) or any(_is_emoji_cp(ord(c)) for c in text) or _KEYCAP_RE.search(text)
    )


def extract_hashtags(text: str) -> list[str]:
    return ["#" + h for h in _HASHTAG_RE.findall(text)]


def limit_hashtags(text: str, max_hashtags: int) -> tuple[str, int]:
    """Mantém as primeiras ``max_hashtags`` hashtags distintas; remove as demais."""
    seen: list[str] = []
    removed = 0

    def repl(m: re.Match) -> str:
        nonlocal removed
        tag = m.group(0).lower()
        if tag in seen:
            removed += 1
            return ""
        if len(seen) >= max_hashtags:
            removed += 1
            return ""
        seen.append(tag)
        return m.group(0)

    out = _HASHTAG_RE.sub(repl, text)
    out = "\n".join(re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, removed


def normalize_punctuation(text: str) -> str:
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    return text


@dataclass
class CaptionCheck:
    text: str
    is_valid: bool
    needs_review: bool
    issues: list[str] = field(default_factory=list)
    emojis_removed: bool = False
    hashtags_removed: int = 0


def clean_model_output(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    # remove aspas ou blocos de código envolvendo toda a legenda
    text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    if len(text) >= 2 and text[0] in "\"“'" and text[-1] in "\"”'":
        text = text[1:-1].strip()
    return text


def validate_caption(text: str, *, allow_emojis: bool = False, max_hashtags: int = 3, min_chars: int = 40,
                     max_chars: int = 900, verified_opportunity: bool = False) -> CaptionCheck:
    issues: list[str] = []
    review = False
    original = clean_model_output(text)
    cleaned = original
    emojis_removed = False
    if not allow_emojis:
        stripped = strip_emojis(cleaned)
        emojis_removed = stripped != unicodedata.normalize("NFC", cleaned).strip()
        cleaned = stripped
    cleaned = normalize_punctuation(cleaned)
    cleaned, removed = limit_hashtags(cleaned, max_hashtags)
    if emojis_removed:
        issues.append("emojis removidos automaticamente")
    if removed:
        issues.append(f"{removed} hashtag(s) excedente(s) removida(s)")

    if not cleaned:
        return CaptionCheck("", False, True, issues + ["legenda vazia"], emojis_removed, removed)
    if len(cleaned) < min_chars:
        issues.append(f"legenda curta demais ({len(cleaned)} < {min_chars} caracteres)")
        review = True
    if len(cleaned) > max_chars:
        issues.append(f"legenda longa demais ({len(cleaned)} > {max_chars} caracteres)")
        review = True
    if len(cleaned) > 2200:
        return CaptionCheck(cleaned, False, True, issues + ["acima do limite do Instagram (2.200)"], emojis_removed, removed)
    if PLACEHOLDER_RE.search(cleaned):
        issues.append("contém marcador/placeholder")
        review = True
    if PREAMBLE_RE.search(cleaned):
        issues.append("contém introdução do modelo em vez de somente a legenda")
        review = True
    for pat, label in BANNED_PATTERNS:
        if label.startswith("vaga") and verified_opportunity:
            continue
        if pat.search(cleaned):
            issues.append(f"conteúdo proibido: {label}")
            review = True
    if cleaned[-1] not in ".!?…\"”)" and not cleaned.split()[-1].startswith("#"):
        issues.append("legenda parece incompleta (sem pontuação final)")
        review = True
    if re.search(r"(.)\1{5,}", cleaned):
        issues.append("repetição anormal de caracteres")
        review = True
    return CaptionCheck(cleaned, not review, review, issues, emojis_removed, removed)