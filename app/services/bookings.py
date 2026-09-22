"""Бизнес-логика бронирований.

Защита от пересечений двухуровневая:
1. Предварительная проверка (SELECT) — чтобы вернуть понятную ошибку со списком
   конфликтов и альтернатив.
2. EXCLUDE-ограничение в PostgreSQL — единственный источник истины. Если два запроса
   одновременно прошли п.1, второй INSERT всё равно упадёт с 23P01, и мы превратим
   это в тот же BookingConflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import BookingConflict, Conflict, Forbidden, NotFound, ValidationFailed
from app.models import Booking, BookingSource, BookingStatus, Room, User
from app.services.rooms import get_room
from app.timeutils import ensure_aware, floor_to_slot, now_utc, to_local, work_bounds

EXCLUSION_VIOLATION = "23P01"
MAX_ALTERNATIVES = 3


@dataclass(frozen=True)
class Slot:
    start_at: datetime
    end_at: datetime


@dataclass
class Alternatives:
    slots: list[Slot] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)


# --------------------------------------------------------------------------- валидация


def validate_times(
    start_at: datetime, end_at: datetime, *, now: datetime
) -> tuple[datetime, datetime]:
    """Правила времени брони. Чистая функция — легко тестируется без БД."""
    s = get_settings()
    start, end = ensure_aware(start_at), ensure_aware(end_at)
    ls, le = to_local(start), to_local(end)

    if end <= start:
        raise ValidationFailed("Время окончания должно быть позже начала", field="end_at")
    for name, value in (("start_at", ls), ("end_at", le)):
        if value.minute % s.slot_minutes or value.second or value.microsecond:
            raise ValidationFailed(
                f"Время должно быть кратно {s.slot_minutes} минутам (например 14:00, 14:15)",
                field=name,
            )
    minutes = (end - start) // timedelta(minutes=1)
    if not s.min_booking_minutes <= minutes <= s.max_booking_minutes:
        raise ValidationFailed(
            f"Длительность брони — от {s.min_booking_minutes} мин до "
            f"{s.max_booking_minutes // 60} ч",
            field="end_at",
        )
    day_start, day_end = work_bounds(ls.date())
    if ls < day_start or le > day_end:
        raise ValidationFailed(
            f"Бронировать можно только в рабочие часы "
            f"{s.work_day_start:%H:%M}–{s.work_day_end:%H:%M}",
            field="start_at",
        )
    # Текущий, уже начавшийся слот бронировать можно: «нужна переговорка прямо сейчас».
    if start < floor_to_slot(now):
        raise ValidationFailed("Нельзя забронировать время в прошлом", field="start_at")
    if start > now + timedelta(days=s.booking_horizon_days):
        raise ValidationFailed(
            f"Бронировать можно не дальше чем на {s.booking_horizon_days} дней вперёд",
            field="start_at",
        )
    return start, end


def clean_title(title: str) -> str:
    title = " ".join(title.split())
    if not title:
        raise ValidationFailed("Укажите тему встречи", field="title")
    if len(title) > 200:
        raise ValidationFailed("Тема встречи — не длиннее 200 символов", field="title")
    return title


# --------------------------------------------------------------------------- запросы


def _overlaps(start: datetime, end: datetime) -> ColumnElement[bool]:
    return (
        (Booking.status == BookingStatus.active)
        & (Booking.start_at < end)
        & (Booking.end_at > start)
    )


async def find_conflicts(
    session: AsyncSession, room_id: int, start: datetime, end: datetime
) -> list[Booking]:
    q = (
        select(Booking)
        .where(Booking.room_id == room_id, _overlaps(start, end))
        .order_by(Booking.start_at)
    )
    return list(await session.scalars(q))


async def bookings_between(
    session: AsyncSession, start: datetime, end: datetime, *, room_id: int | None = None
) -> list[Booking]:
    q = select(Booking).where(_overlaps(start, end)).order_by(Booking.start_at)
    if room_id is not None:
        q = q.where(Booking.room_id == room_id)
    return list(await session.scalars(q))


async def free_rooms(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    min_capacity: int = 1,
    exclude_room_id: int | None = None,
    prefer_capacity: int | None = None,
) -> list[Room]:
    """Активные комнаты, свободные на весь интервал. Сортировка: ближайшие по вместимости,
    сначала те, где мест не меньше нужного."""
    busy = exists().where(Booking.room_id == Room.id, _overlaps(start, end))
    q = select(Room).where(Room.is_active.is_(True), ~busy, Room.capacity >= min_capacity)
    if exclude_room_id is not None:
        q = q.where(Room.id != exclude_room_id)
    rooms = list(await session.scalars(q))
    target = prefer_capacity or min_capacity
    rooms.sort(key=lambda r: (r.capacity < target, abs(r.capacity - target), r.name))
    return rooms


async def free_slots_same_day(
    session: AsyncSession, room_id: int, start: datetime, end: datetime, *, now: datetime
) -> list[Slot]:
    """Ближайшие к желаемому времени свободные слоты той же длительности в тот же день."""
    step = timedelta(minutes=get_settings().slot_minutes)
    duration = end - start
    day_start, day_end = work_bounds(to_local(start).date())
    busy = [
        (b.start_at, b.end_at)
        for b in await bookings_between(session, day_start, day_end, room_id=room_id)
    ]
    candidates: list[datetime] = []
    t = max(day_start, floor_to_slot(now))
    while t + duration <= day_end:
        if not any(bs < t + duration and be > t for bs, be in busy):
            candidates.append(t)
        t += step
    # Жадно берём ближайшие к желаемому времени, но не пересекающиеся между собой:
    # «15:00–16:30» и «15:15–16:45» — по сути один вариант, пользователю нужен выбор.
    picked: list[datetime] = []
    for c in sorted(candidates, key=lambda c: abs(c - start)):
        if all(c + duration <= p or c >= p + duration for p in picked):
            picked.append(c)
            if len(picked) == MAX_ALTERNATIVES:
                break
    return [Slot(c, c + duration) for c in sorted(picked)]


async def alternatives_for(
    session: AsyncSession, room: Room, start: datetime, end: datetime, *, now: datetime
) -> Alternatives:
    return Alternatives(
        slots=await free_slots_same_day(session, room.id, start, end, now=now),
        rooms=(
            await free_rooms(
                session, start, end, exclude_room_id=room.id, prefer_capacity=room.capacity
            )
        )[:MAX_ALTERNATIVES],
    )


# --------------------------------------------------------------------------- команды


async def _conflict_error(
    session: AsyncSession, room: Room, start: datetime, end: datetime, now: datetime
) -> BookingConflict:
    conflicts = await find_conflicts(session, room.id, start, end)
    alt = await alternatives_for(session, room, start, end, now=now)
    return BookingConflict(
        f"«{room.name}» уже занята в это время",
        details={
            "conflicts": [
                {
                    "id": b.id,
                    "title": b.title,
                    "user_name": b.user.full_name,
                    "start_at": b.start_at.isoformat(),
                    "end_at": b.end_at.isoformat(),
                }
                for b in conflicts
            ],
            "alternative_slots": [
                {"start_at": s.start_at.isoformat(), "end_at": s.end_at.isoformat()}
                for s in alt.slots
            ],
            "alternative_rooms": [
                {"room_id": r.id, "room_name": r.name, "capacity": r.capacity} for r in alt.rooms
            ],
        },
        conflicts=conflicts,
        alternatives=alt,
    )


async def create_booking(
    session: AsyncSession,
    user: User,
    *,
    room_id: int,
    start_at: datetime,
    end_at: datetime,
    title: str,
    source: BookingSource = BookingSource.form,
    now: datetime | None = None,
) -> Booking:
    now = now or now_utc()
    room = await get_room(session, room_id)
    start, end = validate_times(start_at, end_at, now=now)
    title = clean_title(title)

    if await find_conflicts(session, room.id, start, end):
        raise await _conflict_error(session, room, start, end, now)

    booking = Booking(
        room_id=room.id, user_id=user.id, title=title, start_at=start, end_at=end, source=source
    )
    try:
        # SAVEPOINT: при ошибке откатывается только вставка, а room/user в сессии
        # остаются загруженными (полный rollback «протух» бы все объекты сессии).
        async with session.begin_nested():
            session.add(booking)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == EXCLUSION_VIOLATION:
            # Проиграли гонку: кто-то занял слот между проверкой и вставкой.
            raise await _conflict_error(session, room, start, end, now) from exc
        raise
    await session.commit()
    await session.refresh(booking)
    return booking


async def get_booking(session: AsyncSession, booking_id: int) -> Booking:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise NotFound("Бронь не найдена")
    return booking


async def cancel_booking(
    session: AsyncSession, user: User, booking_id: int, *, now: datetime | None = None
) -> Booking:
    now = now or now_utc()
    # FOR UPDATE: две одновременные отмены не перезапишут друг друга.
    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update(of=Booking)
    )
    if booking is None:
        raise NotFound("Бронь не найдена")
    if booking.user_id != user.id and not user.is_admin:
        raise Forbidden("Отменить можно только свою бронь")
    if booking.status == BookingStatus.cancelled:
        raise Conflict("Бронь уже отменена")
    if booking.end_at <= now:
        raise Conflict("Встреча уже прошла — отменять нечего")
    booking.status = BookingStatus.cancelled
    booking.cancelled_at = now
    await session.commit()
    return booking


async def user_bookings(
    session: AsyncSession, user: User, *, include_past: bool = False, now: datetime | None = None
) -> list[Booking]:
    now = now or now_utc()
    q = select(Booking).where(Booking.user_id == user.id)
    if include_past:
        q = q.order_by(Booking.start_at.desc()).limit(200)
    else:
        q = q.where(Booking.status == BookingStatus.active, Booking.end_at > now).order_by(
            Booking.start_at
        )
    return list(await session.scalars(q))
