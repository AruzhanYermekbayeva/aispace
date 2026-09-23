from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import Conflict, Unauthorized, ValidationFailed
from app.models import AuthSession, User
from app.security import (
    dummy_hash,
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)
from app.timeutils import now_utc

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD, MAX_PASSWORD = 8, 128


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise ValidationFailed("Некорректный email", field="email")
    allowed = get_settings().allowed_email_domains
    domain = email.rsplit("@", 1)[1]
    if allowed and domain not in allowed:
        raise ValidationFailed(
            "Регистрация доступна только с корпоративной почты: "
            + ", ".join(f"@{d}" for d in allowed),
            field="email",
        )
    return email


def validate_password(password: str) -> None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValidationFailed(
            f"Пароль должен быть от {MIN_PASSWORD} до {MAX_PASSWORD} символов", field="password"
        )


async def register_user(
    session: AsyncSession, *, email: str, full_name: str, password: str, is_admin: bool = False
) -> User:
    email = validate_email(email)
    full_name = " ".join(full_name.split())
    if not 1 <= len(full_name) <= 120:
        raise ValidationFailed("Укажите имя (до 120 символов)", field="full_name")
    validate_password(password)

    if await session.scalar(select(User.id).where(User.email == email)):
        raise Conflict("Пользователь с таким email уже зарегистрирован", details={"field": "email"})
    user = User(
        email=email, full_name=full_name, password_hash=hash_password(password), is_admin=is_admin
    )
    try:
        async with session.begin_nested():  # гонка двух одновременных регистраций
            session.add(user)
    except IntegrityError as exc:
        raise Conflict(
            "Пользователь с таким email уже зарегистрирован", details={"field": "email"}
        ) from exc
    await session.commit()
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    # Проверяем хеш всегда — время ответа не выдаёт, существует ли пользователь.
    ok = verify_password(password, user.password_hash if user else dummy_hash())
    if not user or not ok:
        raise Unauthorized("Неверный email или пароль")
    if not user.is_active:
        raise Unauthorized("Учётная запись отключена")
    return user


async def create_session(session: AsyncSession, user: User) -> str:
    token = new_session_token()
    now = now_utc()
    # Заодно чистим протухшие сессии этого пользователя — таблица не растёт бесконечно.
    await session.execute(
        delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at < now)
    )
    session.add(
        AuthSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
        )
    )
    await session.commit()
    return token


async def user_by_token(session: AsyncSession, token: str) -> User | None:
    auth = await session.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == hash_token(token), AuthSession.expires_at > now_utc()
        )
    )
    if auth is None or not auth.user.is_active:
        return None
    return auth.user


async def destroy_session(session: AsyncSession, token: str) -> None:
    await session.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    await session.commit()
