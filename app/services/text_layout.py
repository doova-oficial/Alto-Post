"""Quebra de linhas e ajuste de texto por largura real em pixels."""
from __future__ import annotations

from dataclasses import dataclass

from PIL import ImageDraw, ImageFont


class TextOverflowError(ValueError):
    """O texto não cabe na área disponível nem no menor tamanho legível."""


@dataclass
class TextBlock:
    text: str
    lines: list[str]
    font: ImageFont.FreeTypeFont
    size: int
    line_height: int
    width: int
    height: int


def text_width(font: ImageFont.FreeTypeFont, s: str) -> float:
    return font.getlength(s) if s else 0.0


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """Quebra gulosa por palavras; palavras maiores que a linha são hifenizadas."""
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for w in words:
            cand = w if not cur else f"{cur} {w}"
            if text_width(font, cand) <= max_width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if text_width(font, w) <= max_width:
                cur = w
                continue
            piece = ""
            for ch in w:
                if piece and text_width(font, piece + ch + "-") > max_width:
                    lines.append(piece + "-")
                    piece = ch
                else:
                    piece += ch
            cur = piece
        if cur:
            lines.append(cur)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def make_block(text: str, font: ImageFont.FreeTypeFont, size: int, max_width: float,
               line_spacing: float = 1.15) -> TextBlock:
    lines = wrap_text(text, font, max_width)
    ascent, descent = font.getmetrics()
    lh = max(1, round(size * line_spacing))
    width = int(max((text_width(font, ln) for ln in lines), default=0))
    height = (len(lines) - 1) * lh + ascent + descent if lines else 0
    return TextBlock(text=text, lines=lines, font=font, size=size, line_height=lh, width=width, height=height)


def draw_block(draw: ImageDraw.ImageDraw, block: TextBlock, x: float, y: float, fill, align: str = "left",
               box_width: float | None = None) -> None:
    for i, line in enumerate(block.lines):
        lx = x
        if align == "center" and box_width is not None:
            lx = x + (box_width - text_width(block.font, line)) / 2
        draw.text((lx, y + i * block.line_height), line, font=block.font, fill=fill, anchor="la")