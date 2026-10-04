from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models_dev import MODELS_DEV_URL, OPENCODE_SNAPSHOT_URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZEN_PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8787

    # Key handed to Zen. Left empty on purpose: Zen treats an absent/"public" key as
    # anonymous traffic, which is the only tier that works without an account.
    # AliasChoices keeps the field name usable as a constructor keyword so the CLI can
    # pass it directly, while the environment variable stays un-prefixed.
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("api_key", "ZEN_API_KEY"))

    zen_base_url: str = "https://opencode.ai/zen/v1"
    request_timeout: float = 600.0
    connect_timeout: float = 15.0
    max_retries: int = 2

    # Open by design: any Authorization header value is accepted and ignored.
    # Set this to a comma separated list to turn client side auth on.
    allowed_client_keys: list[str] = Field(default_factory=list)

    # Tag upstream traffic with the headers the official OpenCode client sends
    # (x-opencode-client / -session / -request and its user agent). Zen gates its
    # free tier on "coming from OpenCode", so this keeps a proxied request
    # looking like the app. Measured: it does not lift the anonymous tier on its
    # own — ZEN_API_KEY is what does — but it costs nothing and keeps the
    # fingerprint stable if Zen tightens the check further.
    zen_client_headers: bool = True

    # Refresh window for the catalog. 0 primes it once at startup and then leaves it
    # alone; the free model list, which routes serve which model, and the context
    # windows are all re-read on this schedule.
    catalog_ttl: float = 900.0

    # Where the catalog comes from. Zen's /models lists ids and nothing else, and
    # models.dev is the catalog opencode itself reads: it is what says whether a
    # model is free (cost 0), which Zen route serves it (the AI SDK package it is
    # reached through) and how large its window is. Hard-coding any of that goes
    # stale every time Zen launches a model.
    models_dev_url: str | None = MODELS_DEV_URL

    # Second copy of the same file, committed to opencode's own repository and
    # refreshed daily, used only when the first source is unreachable.
    models_dev_mirror_url: str | None = OPENCODE_SNAPSHOT_URL

    log_level: str = "INFO"

    def catalog_sources(self) -> list[str]:
        return [url for url in (self.models_dev_url, self.models_dev_mirror_url) if url]

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("allowed_client_keys", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("models_dev_url", "models_dev_mirror_url", mode="before")
    @classmethod
    def _blank_url_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def bearer(self) -> str | None:
        return self.api_key

    @property
    def auth_required(self) -> bool:
        return bool(self.allowed_client_keys)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
