from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
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

    update_mode: Literal["webhook", "polling"] = "webhook"
    telegram_bot_token: SecretStr
    telegram_webhook_secret: Optional[SecretStr] = None
    max_bot_token: SecretStr
    max_operator_user_id: int
    max_webhook_secret: Optional[SecretStr] = None
    public_base_url: Optional[str] = None

    database_path: Path = Path("data/bridge.db")
    log_level: str = "INFO"
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    polling_timeout_seconds: int = Field(default=50, ge=1, le=90)
    polling_drop_pending_updates: bool = False
    max_download_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    send_confirmations: bool = True
    max_ca_file: Optional[Path] = None

    telegram_api_base: str = "https://api.telegram.org"
    max_api_base: str = "https://platform-api2.max.ru"

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL должен начинаться с https://")
        return value

    @field_validator("telegram_webhook_secret", "max_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None:
            return None
        if not _WEBHOOK_SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError(
                "секрет webhook должен содержать 5-256 символов: A-Z, a-z, 0-9, _ или -"
            )
        return value

    @field_validator("max_ca_file")
    @classmethod
    def validate_ca_file(cls, value: Optional[Path]) -> Optional[Path]:
        if value is not None and not value.is_file():
            raise ValueError(f"MAX_CA_FILE не найден: {value}")
        return value

    @model_validator(mode="after")
    def validate_mode_settings(self) -> Settings:
        if self.update_mode == "webhook":
            missing = []
            if not self.public_base_url:
                missing.append("PUBLIC_BASE_URL")
            if self.telegram_webhook_secret is None:
                missing.append("TELEGRAM_WEBHOOK_SECRET")
            if self.max_webhook_secret is None:
                missing.append("MAX_WEBHOOK_SECRET")
            if missing:
                raise ValueError("для UPDATE_MODE=webhook нужны переменные: " + ", ".join(missing))
        return self

    @property
    def telegram_webhook_url(self) -> str:
        if not self.public_base_url:
            raise RuntimeError("PUBLIC_BASE_URL не задан")
        return f"{self.public_base_url}/webhooks/telegram"

    @property
    def max_webhook_url(self) -> str:
        if not self.public_base_url:
            raise RuntimeError("PUBLIC_BASE_URL не задан")
        return f"{self.public_base_url}/webhooks/max"

    @property
    def telegram_webhook_secret_value(self) -> str:
        if self.telegram_webhook_secret is None:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET не задан")
        return self.telegram_webhook_secret.get_secret_value()

    @property
    def max_webhook_secret_value(self) -> str:
        if self.max_webhook_secret is None:
            raise RuntimeError("MAX_WEBHOOK_SECRET не задан")
        return self.max_webhook_secret.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
