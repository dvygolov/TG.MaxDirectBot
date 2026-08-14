from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{5,256}$")


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из переменных окружения или `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    max_bot_token: SecretStr
    max_operator_user_id: int
    max_webhook_secret: SecretStr
    public_base_url: str

    database_path: Path = Path("data/bridge.db")
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_download_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    send_confirmations: bool = True
    max_ca_file: Path | None = None

    telegram_api_base: str = "https://api.telegram.org"
    max_api_base: str = "https://platform-api2.max.ru"

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL должен начинаться с https://")
        return value

    @field_validator("telegram_webhook_secret", "max_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        if not _WEBHOOK_SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "секрет webhook должен содержать 5-256 символов: A-Z, a-z, 0-9, _ или -"
            )
        return value

    @field_validator("max_ca_file")
    @classmethod
    def validate_ca_file(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_file():
            raise ValueError(f"MAX_CA_FILE не найден: {value}")
        return value

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/telegram"

    @property
    def max_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/max"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
