"""Тесты гоняются на настоящем PostgreSQL: EXCLUDE-ограничение, tstzrange и btree_gist
в SQLite не воспроизвести, а именно они — суть защиты от двойных броней.

Схема создаётся теми же миграциями Alembic, что и в проде (заодно проверяем миграции).
"""

from __future__ import annotations

import os

# Настройки должны быть выставлены ДО импорта приложения.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/aispace_test"
)
os.environ["ALLOWED_EMAIL_DOMAINS"] = "aispace.local"
os.environ["SCRYPT_N"] = "1024"  # быстрый хеш только для тестов
os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["OFFICE_TZ"] = "Asia/Almaty"
os.environ["NL_RATE_LIMIT_PER_MINUTE"] = "10"

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.api.auth import login_limiter
from app.db import SessionFactory
from app.llm.client import get_llm_client
from app.main import app
from app.models import Room, User
from app.security import hash_password
from app.services.nl_booking import nl_limiter
from app.timeutils import local_dt, local_today

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct-horse"


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    # downgrade → upgrade: миграция обратима
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    async with SessionFactory() as s:
        await s.execute(
            text("TRUNCATE bookings, auth_sessions, rooms, users RESTART IDENTITY CASCADE")
        )
        await s.commit()
    login_limiter.reset()
    nl_limiter.reset()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------------- фабрики


async def create_user(email: str, *, name: str = "Тест", admin: bool = False) -> User:
    async with SessionFactory() as s:
        user = User(
            email=email, full_name=name, password_hash=hash_password(PASSWORD), is_admin=admin
        )
        s.add(user)
        await s.commit()
        return user


async def create_room(name: str, capacity: int, aliases: list[str] | None = None) -> Room:
    async with SessionFactory() as s:
        room = Room(name=name, capacity=capacity, aliases=aliases or [])
        s.add(room)
        await s.commit()
        return room


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def login(email: str) -> httpx.AsyncClient:
    c = client()
    r = await c.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


def at(days: int, hhmm: str) -> datetime:
    """Локальное время офиса через `days` дней."""
    h, m = map(int, hhmm.split(":"))
    return local_dt(
        local_today() + timedelta(days=days), datetime.min.time().replace(hour=h, minute=m)
    )


def iso(days: int, hhmm: str) -> str:
    return at(days, hhmm).isoformat()


@pytest.fixture
async def rooms() -> dict[str, Room]:
    return {
        "big": await create_room("Большая", 16, ["большая переговорка"]),
        "mid": await create_room("Аквариум", 8),
        "small": await create_room("Малая", 4, ["малая"]),
    }


@pytest.fixture
async def alice() -> AsyncIterator[httpx.AsyncClient]:
    await create_user("alice@aispace.local", name="Алиса")
    c = await login("alice@aispace.local")
    yield c
    await c.aclose()


@pytest.fixture
async def bob() -> AsyncIterator[httpx.AsyncClient]:
    await create_user("bob@aispace.local", name="Боб")
    c = await login("bob@aispace.local")
    yield c
    await c.aclose()


@pytest.fixture
async def admin() -> AsyncIterator[httpx.AsyncClient]:
    await create_user("admin@aispace.local", name="Админ", admin=True)
    c = await login("admin@aispace.local")
    yield c
    await c.aclose()


class FakeLLM:
    """Подменяет DeepSeek: возвращает заданный JSON или бросает заданное исключение."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake_llm() -> Any:
    def install(result: dict[str, Any] | Exception) -> FakeLLM:
        fake = FakeLLM(result)
        app.dependency_overrides[get_llm_client] = lambda: fake
        return fake

    return install
