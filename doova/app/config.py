"""Configuração central carregada de variáveis de ambiente (.env).

Nenhum segredo possui valor padrão real. Todos os valores sensíveis devem vir
do ambiente (ou de um arquivo .env local que nunca é versionado).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ------------------------------------------------------------------ geral
    app_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    timezone: str = "America/Sao_Paulo"
    database_url: str = f"sqlite:///{PROJECT_DIR / 'data' / 'doova.db'}"
    generated_dir: Path = PROJECT_DIR / "generated"
    backup_dir: Path = PROJECT_DIR / "data" / "backups"
    backup_keep: int = Field(default=14, ge=1, le=365)
    docs_enabled: bool = True

    # ------------------------------------------------------------- segurança
    admin_api_token: SecretStr | None = None
    rate_limit_per_minute: int = Field(default=60, ge=1)
    rate_limit_generation_per_minute: int = Field(default=10, ge=1)
    token_encryption_key: SecretStr | None = None  # chave Fernet (base64 32 bytes)

    # ----------------------------------------------------------------- groq
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = Field(default=30.0, gt=0)
    groq_max_retries: int = Field(default=3, ge=0, le=10)
    groq_min_interval_seconds: float = Field(default=2.0, ge=0)
    groq_temperature: float = Field(default=0.7, ge=0, le=2)
    groq_max_completion_tokens: int = Field(default=1024, ge=64, le=8192)
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"

    # ------------------------------------------------------------- legendas
    caption_allow_emojis: bool = False
    caption_max_hashtags: int = Field(default=3, ge=0, le=30)
    caption_min_chars: int = Field(default=40, ge=1)
    caption_max_chars: int = Field(default=900, ge=50, le=2200)
    caption_max_attempts: int = Field(default=4, ge=1, le=20)

    # ----------------------------------------------------------------- meta
    meta_login_type: Literal["instagram", "facebook", "unified"] = "unified"
    meta_graph_api_version: str = "v26.0"
    meta_ig_user_id: str | None = None
    meta_access_token: SecretStr | None = None
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_timeout_seconds: float = Field(default=30.0, gt=0)
    public_base_url: str | None = None  # URL pública HTTPS deste serviço (Meta baixa a imagem daqui)

    # ------------------------------------------------------------ publicação
    dry_run: bool = True
    require_manual_approval: bool = False  # false = modo automático (aprova, agenda e publica sozinho)
    max_posts_per_day: int = Field(default=2, ge=1, le=50)  # limite próprio, abaixo do limite da Meta (100/24h)
    min_minutes_between_posts: int = Field(default=180, ge=0)
    publish_max_attempts: int = Field(default=3, ge=1, le=10)
    container_max_checks: int = Field(default=6, ge=1, le=20)
    container_check_interval_seconds: float = Field(default=5.0, ge=0, le=60)
    auto_schedule: bool = True
    posting_slots: str = "11:30,19:00"  # horários locais usados pelo agendamento automático
    posting_weekdays: str = "0,1,2,3,4,5,6"  # 0=segunda ... 6=domingo
    # horário: "adaptive" aprende com o engajamento; "fixed" usa só POSTING_SLOTS
    posting_strategy: Literal["adaptive", "fixed"] = "adaptive"
    posting_candidate_slots: str = "07:30,09:00,11:30,12:30,15:00,18:00,19:30,21:00"
    timing_min_samples: int = Field(default=3, ge=1, le=50)  # publicações medidas por horário antes de escolher
    timing_explore_rate: float = Field(default=0.2, ge=0, le=1)  # fração que continua testando outros horários
    timing_window_minutes: int = Field(default=45, ge=5, le=180)  # tolerância para associar um post a um horário
    trend_publish_asap: bool = True  # assunto do momento sai o quanto antes se o próximo horário estiver longe

    # ------------------------------------------- modo automático: vigilância
    alert_webhook_url: SecretStr | None = None  # Discord, Slack, ntfy ou qualquer URL que aceite JSON
    alert_webhook_kind: Literal["auto", "discord", "slack", "ntfy", "json"] = "auto"
    alert_dedupe_hours: int = Field(default=12, ge=1, le=168)
    auto_pause_failures: int = Field(default=3, ge=0, le=20)  # falhas definitivas em 24 h que pausam tudo (0 = nunca)
    queue_min_days: int = Field(default=2, ge=0, le=30)  # avisa quando a fila cobre menos dias que isso
    photo_auto_check: bool = True  # recusa fotos cujo título/tags indicam pessoas ou marcas

    # ------------------------------------------------------------ agendador
    scheduler_enabled: bool = True
    publisher_interval_seconds: int = Field(default=60, ge=20)
    metrics_interval_hours: int = Field(default=6, ge=1)
    metrics_max_age_days: int = Field(default=30, ge=1, le=730)
    instance_id: str | None = None

    # ------------------------------------------------------------- análises
    analysis_target_age_hours: int = Field(default=72, ge=1)
    analysis_min_samples_per_group: int = Field(default=5, ge=2)
    analysis_alpha: float = Field(default=0.05, gt=0, lt=1)
    analysis_permutations: int = Field(default=5000, ge=200, le=100000)

    # ------------------------------------------------------------- tendências
    google_trends_rss_enabled: bool = True
    google_trends_geo: str = "BR"

    # ------------------------------------------------- fotos de bancos gratuitos
    image_search_enabled: bool = True
    image_providers: str = "openverse,pixabay"  # ordem de busca
    pixabay_api_key: SecretStr | None = None    # opcional; sem chave usa só o Openverse
    image_search_timeout_seconds: float = Field(default=20.0, gt=0)
    image_search_cache_hours: int = Field(default=24, ge=24)  # o Pixabay exige cache de 24 h
    image_min_width: int = Field(default=900, ge=400)
    image_reuse_days: int = Field(default=60, ge=0)  # não repete a mesma foto nesse período
    contact_email: str | None = None  # vai no User-Agent das buscas (boa prática com APIs abertas)

    # -------------------------------------------------------------- vÃ­deos
    video_enabled: bool = True
    video_provider: Literal["pixabay"] = "pixabay"
    video_timeout_seconds: float = Field(default=45.0, gt=0)
    video_duration_seconds: int = Field(default=12, ge=5, le=60)
    video_width: int = Field(default=1080, ge=480, le=2160)
    video_height: int = Field(default=1920, ge=640, le=3840)
    video_font_size: int = Field(default=64, ge=24, le=140)
    video_max_download_bytes: int = Field(default=80 * 1024 * 1024, ge=1_000_000)

    # ------------------------------------------- assuntos do momento (automÃ¡tico)
    trend_auto_enabled: bool = True
    trend_auto_interval_hours: int = Field(default=6, ge=1)
    trend_auto_max_per_day: int = Field(default=1, ge=1, le=5)
    trend_post_ttl_hours: int = Field(default=48, ge=6, le=240)
    trend_blocklist_extra: str = ""  # palavras proibidas extras, separadas por vírgula
    trend_use_groq: bool = True

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    # --------------------------------------------------------------- helpers
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def meta_host(self) -> str:
        return "https://graph.instagram.com" if self.meta_login_type == "instagram" else "https://graph.facebook.com"

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_ig_user_id and self.meta_access_token and self.meta_access_token.get_secret_value())

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key and self.groq_api_key.get_secret_value())

    @property
    def pixabay_configured(self) -> bool:
        return bool(self.pixabay_api_key and self.pixabay_api_key.get_secret_value())

    def image_provider_list(self) -> list[str]:
        return [p.strip().lower() for p in self.image_providers.split(",")
                if p.strip().lower() in ("openverse", "pixabay")]

    def trend_blocklist(self) -> list[str]:
        return [w.strip() for w in self.trend_blocklist_extra.split(",") if w.strip()]

    @property
    def autonomous(self) -> bool:
        return not self.require_manual_approval

    def slot_times(self) -> list[tuple[int, int]]:
        return self._parse_slots(self.posting_slots)

    def candidate_slot_times(self) -> list[tuple[int, int]]:
        return self._parse_slots(self.posting_candidate_slots) or self.slot_times()

    @staticmethod
    def _parse_slots(text: str) -> list[tuple[int, int]]:
        out = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            h, m = part.split(":")
            out.append((int(h), int(m)))
        return sorted(out)

    def weekdays(self) -> set[int]:
        return {int(x) for x in self.posting_weekdays.split(",") if x.strip() != ""}

    def secret_values(self) -> list[str]:
        vals = []
        for s in (self.admin_api_token, self.groq_api_key, self.meta_access_token, self.meta_app_secret, self.token_encryption_key,
                  self.pixabay_api_key, self.alert_webhook_url):
            if s is not None and s.get_secret_value():
                vals.append(s.get_secret_value())
        return vals


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
