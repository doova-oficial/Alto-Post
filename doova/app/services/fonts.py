"""Resolução de fontes multiplataforma com fallback.

Ordem de busca:
1. Diretório indicado em ``DOOVA_FONT_DIR`` (opcional).
2. Fontes empacotadas no projeto (``app/assets/fonts`` — Inter, licença OFL).
3. Diretórios de fontes do sistema (Linux, macOS e Windows), descobertos em
   tempo de execução — nenhum caminho fixo de um único sistema operacional.
4. Fonte escalável embutida no Pillow (``ImageFont.load_default(size=...)``).

O peso solicitado (regular, medium, semibold, bold, extrabold) é aproximado
pelo peso disponível mais próximo.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
WEIGHTS = ["regular", "medium", "semibold", "bold", "extrabold"]
FONT_FAMILIES = ("auto", "inter", "condensed", "serif_display", "slab", "lato")
FAMILY_FILES: dict[str, dict[str, str]] = {
    "inter": {"regular": "Inter-Regular.otf", "medium": "Inter-Medium.otf", "semibold": "Inter-SemiBold.otf",
              "bold": "Inter-Bold.otf", "extrabold": "Inter-ExtraBold.otf"},
    # Noto Sans Condensed provides a bundled, genuinely condensed display face.
    "condensed": {"bold": "NotoSans-CondensedBold.ttf", "extrabold": "NotoSans-ExtraCondensedExtraBold.ttf"},
    "serif_display": {"regular": "NotoSerifDisplay-Regular.ttf", "bold": "NotoSerifDisplay-Bold.ttf"},
    "slab": {"regular": "RobotoSlab-Regular.otf", "bold": "RobotoSlab-Bold.otf"},
    "lato": {"regular": "Lato-Regular.ttf", "bold": "Lato-Bold.ttf", "extrabold": "Lato-Black.ttf"},
}

# Nomes de arquivo candidatos por peso, em ordem de preferência.
CANDIDATES: dict[str, list[str]] = {
    "regular": ["Inter-Regular.otf", "Inter-Regular.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                "NotoSans-Regular.ttf", "Roboto-Regular.ttf", "OpenSans-Regular.ttf", "Arial.ttf", "arial.ttf",
                "segoeui.ttf", "NimbusSans-Regular.otf"],
    "medium": ["Inter-Medium.otf", "Inter-Medium.ttf", "NotoSans-Medium.ttf", "Roboto-Medium.ttf",
               "OpenSans-Medium.ttf"],
    "semibold": ["Inter-SemiBold.otf", "Inter-SemiBold.ttf", "NotoSans-SemiBold.ttf", "OpenSans-SemiBold.ttf",
                 "seguisb.ttf"],
    "bold": ["Inter-Bold.otf", "Inter-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
             "NotoSans-Bold.ttf", "Roboto-Bold.ttf", "OpenSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf",
             "segoeuib.ttf", "NimbusSans-Bold.otf"],
    "extrabold": ["Inter-ExtraBold.otf", "Inter-ExtraBold.ttf", "Inter-Black.otf", "NotoSans-ExtraBold.ttf",
                  "OpenSans-ExtraBold.ttf", "Roboto-Black.ttf", "Arial Black.ttf", "ariblk.ttf"],
}


def system_font_dirs() -> list[Path]:
    home = Path.home()
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", "C:\\Windows")
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts",
                 home / ".local" / "share" / "fonts"]
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            dirs.append(Path(xdg) / "fonts")
    return [d for d in dirs if d.is_dir()]


class FontResolver:
    def __init__(self, extra_dirs: list[Path] | None = None, use_bundled: bool = True, use_system: bool = True):
        self.dirs: list[Path] = []
        env_dir = os.environ.get("DOOVA_FONT_DIR")
        if env_dir:
            self.dirs.append(Path(env_dir))
        self.dirs += list(extra_dirs or [])
        if use_bundled:
            self.dirs.append(BUNDLED_DIR)
        if use_system:
            self.dirs += system_font_dirs()
        self._index: dict[str, Path] | None = None
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, int], ImageFont.FreeTypeFont] = {}
        self.fallback_used = False

    def _build_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for d in self.dirs:
            if not d.is_dir():
                continue
            try:
                for p in d.rglob("*"):
                    if p.suffix.lower() in {".ttf", ".otf"}:
                        index.setdefault(p.name.lower(), p)
            except OSError:  # pragma: no cover - permissões
                continue
        return index

    @property
    def index(self) -> dict[str, Path]:
        if self._index is None:
            with self._lock:
                if self._index is None:
                    self._index = self._build_index()
        return self._index

    def path_for(self, weight: str, family: str = "inter") -> Path | None:
        if weight not in WEIGHTS:
            weight = "regular"
        family = family if family in FAMILY_FILES else "inter"
        family_files = FAMILY_FILES[family]
        # Resolve o peso mais próximo dentro da família antes de recorrer à Inter.
        i = WEIGHTS.index(weight)
        order = sorted(range(len(WEIGHTS)), key=lambda j: (abs(j - i), -j))
        for j in order:
            name = family_files.get(WEIGHTS[j])
            if name:
                path = self.index.get(name.lower())
                if path is not None:
                    return path
        for j in order:
            for name in CANDIDATES[WEIGHTS[j]]:
                path = self.index.get(name.lower())
                if path is not None:
                    return path
        return None

    def get(self, weight: str, size: int, family: str = "inter") -> ImageFont.FreeTypeFont:
        size = max(8, int(size))
        family = family if family in FAMILY_FILES else "inter"
        key = (family, weight, size)
        font = self._cache.get(key)
        if font is not None:
            return font
        path = self.path_for(weight, family)
        font = None
        if path is not None:
            try:
                font = ImageFont.truetype(str(path), size)
            except OSError:
                logger.warning("Falha ao abrir a fonte %s; usando fallback", path)
        if font is None:
            self.fallback_used = True
            font = ImageFont.load_default(size=size)
        self._cache[key] = font
        return font

    def describe(self) -> dict:
        # Mantém o contrato anterior: pesos da família padrão (Inter).
        return {w: (str(self.path_for(w, "inter")) if self.path_for(w, "inter") else "pillow-default")
                for w in WEIGHTS}

    def describe_families(self) -> dict:
        return {family: {w: (str(self.path_for(w, family)) if self.path_for(w, family) else "pillow-default")
                         for w in WEIGHTS} for family in FAMILY_FILES}


@lru_cache
def get_font_resolver() -> FontResolver:
    return FontResolver()