from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import Conflict, NotFound
from app.models import Room
from app.schemas import RoomIn, RoomPatch


def _clean_aliases(aliases: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for a in aliases:
        a = " ".join(a.split()).lower()[:60]
        if a:
            seen.setdefault(a, None)
    return list(seen)


async def list_rooms(session: AsyncSession, *, include_inactive: bool = False) -> list[Room]:
    q = select(Room).order_by(Room.capacity.desc(), Room.name)
    if not include_inactive:
        q = q.where(Room.is_active.is_(True))
    return list(await session.scalars(q))


async def get_room(session: AsyncSession, room_id: int, *, active_only: bool = True) -> Room:
    room = await session.get(Room, room_id)
    if room is None or (active_only and not room.is_active):
        raise NotFound("Комната не найдена или недоступна для бронирования")
    return room


async def create_room(session: AsyncSession, data: RoomIn) -> Room:
    room = Room(
        name=data.name.strip(),
        capacity=data.capacity,
        location=(data.location or "").strip() or None,
        description=(data.description or "").strip() or None,
        aliases=_clean_aliases(data.aliases),
    )
    session.add(room)
    await _commit_unique(session)
    return room


async def update_room(session: AsyncSession, room_id: int, data: RoomPatch) -> Room:
    """Удаления комнат нет: на них ссылается история броней. Вместо этого is_active=false —
    комната пропадает из расписания и выбора, существующие брони остаются видны владельцам."""
    room = await get_room(session, room_id, active_only=False)
    changes = data.model_dump(exclude_unset=True)
    if "aliases" in changes and changes["aliases"] is not None:
        changes["aliases"] = _clean_aliases(changes["aliases"])
    for field, value in changes.items():
        if value is not None or field in {"location", "description"}:
            setattr(room, field, value.strip() if isinstance(value, str) else value)
    await _commit_unique(session)
    return room


async def _commit_unique(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Комната с таким названием уже есть", details={"field": "name"}) from exc
