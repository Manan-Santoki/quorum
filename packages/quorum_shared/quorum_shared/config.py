"""Central configuration for both Quorum bots, loaded from environment / .env."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration for the Quorum suite. Read once from the environment.

    Both bots import the shared ``settings`` singleton, so a single ``.env``
    drives the whole system.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    # Sync SQLAlchemy URL (psycopg3). APScheduler's SQLAlchemyJobStore also
    # uses a sync engine, so we standardise on sync sessions across the suite.
    database_url: str = Field(
        default="postgresql+psycopg://quorum:quorum@postgres:5432/quorum",
        alias="DATABASE_URL",
    )

    # --- Discord tokens (two separate applications) -------------------------
    discord_token_scheduler: str = Field(default="", alias="DISCORD_TOKEN_SCHEDULER")
    discord_token_scribe: str = Field(default="", alias="DISCORD_TOKEN_SCRIBE")

    # Comma-separated guild ids for instant slash-command sync during dev.
    # Leave empty in production for global command registration.
    # NoDecode: keep pydantic-settings from JSON-decoding the env string so our
    # CSV validator below handles it (and an empty string means "none").
    dev_guild_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list, alias="DEV_GUILD_IDS"
    )

    # --- Transcription providers --------------------------------------------
    transcribe_provider: str = Field(default="groq", alias="TRANSCRIBE_PROVIDER")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = Field(default="whisper-large-v3-turbo", alias="GROQ_MODEL")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_transcribe_model: str = Field(
        default="openai/whisper-large-v3", alias="OPENROUTER_TRANSCRIBE_MODEL"
    )
    faster_whisper_model: str = Field(
        default="distil-large-v3", alias="FASTER_WHISPER_MODEL"
    )

    # --- Minutes (summarisation) provider -----------------------------------
    minutes_provider: str = Field(default="deepseek", alias="MINUTES_PROVIDER")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )

    # --- Google Calendar (optional push) ------------------------------------
    # Path to a service-account JSON key mounted into the container.
    google_sa_json: str = Field(default="", alias="GOOGLE_SA_JSON")

    # --- Scheduler behaviour ------------------------------------------------
    # Minutes-before-start at which reminders fire, highest first.
    reminder_offsets: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [1440, 60, 10], alias="REMINDER_OFFSETS"
    )
    default_timezone: str = Field(default="UTC", alias="DEFAULT_TIMEZONE")

    # Public base URL where the iCal feed is reachable (no trailing slash).
    public_base_url: str = Field(default="http://localhost:8080", alias="PUBLIC_BASE_URL")
    ical_host: str = Field(default="0.0.0.0", alias="ICAL_HOST")
    ical_port: int = Field(default=8080, alias="ICAL_PORT")

    # --- Scribe guardrails --------------------------------------------------
    max_recording_minutes: int = Field(default=180, alias="MAX_RECORDING_MINUTES")
    # Groq per-file cap in MB (25 free tier, 100 dev tier). Tracks larger than
    # this are chunked before upload.
    transcribe_max_file_mb: int = Field(default=24, alias="TRANSCRIBE_MAX_FILE_MB")

    @field_validator("dev_guild_ids", "reminder_offsets", mode="before")
    @classmethod
    def _split_int_csv(cls, v: object) -> object:
        """Accept ``"1,2,3"`` env strings for int-list fields."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            return [int(part.strip()) for part in v.split(",") if part.strip()]
        return v


settings = Settings()
