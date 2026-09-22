from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, status

from app.deps import DB, AdminUser, CurrentUser
from app.schemas import BookingOut, RoomIn, RoomOut, RoomPatch, RoomScheduleOut
from app.services import bookings as booking_service
from app.services import rooms as room_service
from app.timeutils import local_today, work_bounds

from .bookings import booking_out

router = APIRouter(tags=["rooms"])


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(
    session: DB, user: CurrentUser, include_inactive: bool = False
) -> list[RoomOut]:
    rooms = await room_service.list_rooms(
        session, include_inactive=include_inactive and user.is_admin
    )
    return [RoomOut.model_validate(r) for r in rooms]


@router.post("/rooms", response_model=RoomOut, status_code=status.HTTP_201_CREATED)
async def create_room(payload: RoomIn, session: DB, _: AdminUser) -> RoomOut:
    return RoomOut.model_validate(await room_service.create_room(session, payload))


@router.patch("/rooms/{room_id}", response_model=RoomOut)
async def update_room(room_id: int, payload: RoomPatch, session: DB, _: AdminUser) -> RoomOut:
    return RoomOut.model_validate(await room_service.update_room(session, room_id, payload))


@router.get("/schedule", response_model=list[RoomScheduleOut])
async def schedule(
    session: DB,
    _: CurrentUser,
    day: date | None = Query(
        default=None, alias="date", description="YYYY-MM-DD, по умолчанию сегодня"
    ),
) -> list[RoomScheduleOut]:
    """Занятость всех активных комнат на день (в рабочие часы)."""
    start, end = work_bounds(day or local_today())
    rooms = await room_service.list_rooms(session)
    bookings = await booking_service.bookings_between(session, start, end)
    by_room: dict[int, list[BookingOut]] = {r.id: [] for r in rooms}
    for b in bookings:
        if b.room_id in by_room:
            by_room[b.room_id].append(booking_out(b))
    return [RoomScheduleOut(room=RoomOut.model_validate(r), bookings=by_room[r.id]) for r in rooms]
