from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import Forbidden, Unauthorized
from app.models import User
from app.services.auth import user_by_token

SESSION_COOKIE = "aispace_session"

DB = Annotated[AsyncSession, Depends(get_session)]


def extract_token(request: Request) -> str | None:
    """Токен из cookie (браузер) или заголовка Authorization: Bearer (API-клиенты)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


async def optional_user(request: Request, session: DB) -> User | None:
    token = extract_token(request)
    return await user_by_token(session, token) if token else None


async def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise Unauthorized("Требуется вход в систему")
    return user


async def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise Forbidden("Действие доступно только администратору")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]
AdminUser = Annotated[User, Depends(admin_user)]
