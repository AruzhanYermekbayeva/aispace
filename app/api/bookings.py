from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.deps import DB, CurrentUser
from app.models import Booking
from app.schemas import BookingIn, BookingOut
from app.services import bookings as booking_service
from app.services.ics import booking_to_ics

router = APIRouter(prefix="/bookings", tags=["bookings"])


def booking_out(b: Booking) -> BookingOut:
    return BookingOut(
        id=b.id,
        room_id=b.room_id,
        room_name=b.room.name,
        user_id=b.user_id,
        user_name=b.user.full_name,
        title=b.title,
        start_at=b.start_at,
        end_at=b.end_at,
        status=b.status,
        source=b.source,
    )


@router.post(
    "",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Слот занят. В details — конфликты и свободные альтернативы"},
        422: {"description": "Нарушены правила бронирования"},
    },
)
async def create_booking(payload: BookingIn, session: DB, user: CurrentUser) -> BookingOut:
    booking = await booking_service.create_booking(
        session,
        user,
        room_id=payload.room_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        title=payload.title,
    )
    return booking_out(booking)


@router.get("/my", response_model=list[BookingOut])
async def my_bookings(
    session: DB, user: CurrentUser, include_past: bool = False
) -> list[BookingOut]:
    items = await booking_service.user_bookings(session, user, include_past=include_past)
    return [booking_out(b) for b in items]


@router.get("/{booking_id}", response_model=BookingOut)
async def get_booking(booking_id: int, session: DB, _: CurrentUser) -> BookingOut:
    return booking_out(await booking_service.get_booking(session, booking_id))


@router.post("/{booking_id}/cancel", response_model=BookingOut)
async def cancel_booking(booking_id: int, session: DB, user: CurrentUser) -> BookingOut:
    return booking_out(await booking_service.cancel_booking(session, user, booking_id))


@router.get("/{booking_id}/calendar.ics", response_class=Response)
async def booking_ics(booking_id: int, request: Request, session: DB, _: CurrentUser) -> Response:
    booking = await booking_service.get_booking(session, booking_id)
    return Response(
        booking_to_ics(booking, host=request.url.hostname or "aispace"),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="booking-{booking.id}.ics"'},
    )
