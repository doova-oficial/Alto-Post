"""Logging com mascaramento de segredos."""
from __future__ import annotations

import logging
import re

_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
    re.compile(r"(client_secret=)[^&\s\"']+", re.I),
    re.compile(r"()\bgsk_[A-Za-z0-9]{10,}"),
    re.compile(r"()\b(?:EAA|IGAA|IGQV)[A-Za-z0-9_\-]{20,}"),
]


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    for secret in extra_secrets or []:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "***", text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets_provider=None):
        super().__init__()
        self._secrets_provider = secrets_provider

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        secrets = self._secrets_provider() if self._secrets_provider else []
        clean = redact(msg, secrets)
        if clean != msg or record.args:
            record.msg = clean
            record.args = None
        return True


def setup_logging(level: str = "INFO", secrets_provider=None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(getattr(h, "_doova", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler._doova = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter(secrets_provider))
    # httpx registra URLs completas no nível INFO; mantemos em WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)