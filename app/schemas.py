"""Pydantic-схемы JSON API. Бизнес-валидация — в сервисах, здесь только форма данных."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import BookingSource, BookingStatus


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth ---
class RegisterIn(BaseModel):
    email: str = Field(max_length=254, examples=["ivan@aispace.local"])
    full_name: str = Field(max_length=120)
    password: str = Field(max_length=128)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    is_admin: bool


class LoginOut(BaseModel):
    user: UserOut
    token: str = Field(description="Можно передавать как Authorization: Bearer <token>")


# --- Rooms ---
class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    capacity: int = Field(gt=0, le=500)
    location: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=10)


class RoomPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    capacity: int | None = Field(default=None, gt=0, le=500)
    location: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] | None = Field(default=None, max_length=10)
    is_active: bool | None = None


class RoomOut(ORM):
    id: int
    name: str
    capacity: int
    location: str | None
    description: str | None
    aliases: list[str]
    is_active: bool


# --- Bookings ---
class BookingIn(BaseModel):
    room_id: int
    start_at: datetime = Field(description="ISO 8601. Без часового пояса — время офиса")
    end_at: datetime
    title: str = Field(min_length=1, max_length=200)


class BookingOut(ORM):
    id: int
    room_id: int
    room_name: str
    user_id: int
    user_name: str
    title: str
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    source: BookingSource


class SlotOut(BaseModel):
    start_at: datetime
    end_at: datetime


class RoomSlotOut(BaseModel):
    room_id: int
    room_name: str
    capacity: int


class RoomScheduleOut(BaseModel):
    room: RoomOut
    bookings: list[BookingOut]


# --- Natural language ---
class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DraftOut(BaseModel):
    """Черновик брони из фразы. Ничего не создаёт — клиент подтверждает через POST /bookings."""

    room_id: int | None
    room_name: str | None
    start_at: datetime | None
    end_at: datetime | None
    title: str | None
    missing: list[str] = Field(description="Чего не хватает, чтобы создать бронь")
    assumptions: list[str] = Field(description="Что мы достроили сами — показать пользователю")
    problems: list[str] = Field(description="Почему бронь с такими параметрами не пройдёт")
    conflict: bool
    alternative_slots: list[SlotOut]
    alternative_rooms: list[RoomSlotOut]
    ready: bool = Field(description="Можно отправлять в POST /bookings как есть")
