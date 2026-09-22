"""Начальные данные. Идемпотентно: запускается при каждом старте контейнера.

- Администратор из ADMIN_EMAIL / ADMIN_PASSWORD, если такого пользователя ещё нет.
- Демо-комнаты, если таблица комнат пуста и SEED_DEMO_ROOMS=true.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionFactory, engine
from app.models import Room, User
from app.security import hash_password
from app.services.auth import normalize_email

log = logging.getLogger("aispace.seed")

DEMO_ROOMS = [
    {
        "name": "Большая переговорная",
        "capacity": 16,
        "location": "3 этаж, у ресепшена",
        "description": "Проектор, ВКС, маркерная доска",
        "aliases": ["большая", "большая переговорка", "конференц-зал"],
    },
    {
        "name": "Аквариум",
        "capacity": 8,
        "location": "3 этаж, стеклянная",
        "description": "Телевизор для демонстрации экрана",
        "aliases": ["стеклянная", "средняя"],
    },
    {
        "name": "Малая переговорная",
        "capacity": 4,
        "location": "2 этаж",
        "description": None,
        "aliases": ["малая", "маленькая", "малая переговорка"],
    },
    {
        "name": "Фокус",
        "capacity": 2,
        "location": "2 этаж, у кухни",
        "description": "Для созвонов 1-на-1",
        "aliases": ["переговорка для звонков", "кабинка", "1-на-1"],
    },
]


async def seed() -> None:
    s = get_settings()
    async with SessionFactory() as session:
        if s.admin_email and s.admin_password:
            email = normalize_email(s.admin_email)
            if not await session.scalar(select(User.id).where(User.email == email)):
                session.add(
                    User(
                        email=email,
                        full_name=s.admin_name,
                        password_hash=hash_password(s.admin_password),
                        is_admin=True,
                    )
                )
                log.info("Создан администратор %s", email)
        if s.seed_demo_rooms and not await session.scalar(select(func.count(Room.id))):
            session.add_all(Room(**r) for r in DEMO_ROOMS)
            log.info("Созданы демо-комнаты: %d", len(DEMO_ROOMS))
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    asyncio.run(seed())
