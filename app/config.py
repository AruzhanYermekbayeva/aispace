"""Конфигурация приложения. Все значения берутся из переменных окружения / .env."""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- База данных ---
    database_url: str = "postgresql+psycopg://aispace:aispace@db:5432/aispace"

    # --- Безопасность ---
    session_ttl_hours: int = 24 * 7
    cookie_secure: bool = False  # включить за HTTPS-прокси
    scrypt_n: int = 2**17  # стоимость хеширования паролей; в тестах понижается
    # Регистрация только с этих доменов (через запятую). Пусто — любой домен.
    allowed_email_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["aispace.local"]
    )
    # --- Первый администратор (создаётся при старте, если такого email ещё нет) ---
    admin_email: str = "admin@aispace.local"
    admin_password: str = "admin12345"
    admin_name: str = "Администратор"
    seed_demo_rooms: bool = True

    # --- Правила бронирования ---
    office_tz: str = "Asia/Almaty"
    work_day_start: time = time(8, 0)
    work_day_end: time = time(20, 0)
    slot_minutes: int = 15
    min_booking_minutes: int = 15
    max_booking_minutes: int = 240
    booking_horizon_days: int = 60

    # --- LLM (DeepSeek, OpenAI-совместимый API) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 1
    nl_rate_limit_per_minute: int = 10
    nl_max_text_length: int = 500

    @field_validator("allowed_email_domains", mode="before")
    @classmethod
    def _split_domains(cls, v: object) -> object:
        if isinstance(v, str):
            return [d.strip().lower().lstrip("@") for d in v.split(",") if d.strip()]
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.office_tz)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
