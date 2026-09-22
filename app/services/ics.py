"""Экспорт брони в iCalendar (RFC 5545) — чтобы встреча попала в Google/Outlook-календарь.

Формат простой, поэтому без зависимостей: экранирование, CRLF и перенос длинных строк.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Booking, BookingStatus


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Строки длиннее 75 октетов переносятся: CRLF + пробел (RFC 5545, 3.1)."""
    out, current = [], b""
    for ch in line:
        encoded = ch.encode()
        if len(current) + len(encoded) > 75:
            out.append(current.decode())
            current = b" "
        current += encoded
    out.append(current.decode())
    return "\r\n".join(out)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def booking_to_ics(booking: Booking, *, host: str = "aispace") -> str:
    status = "CANCELLED" if booking.status == BookingStatus.cancelled else "CONFIRMED"
    location = booking.room.name + (f", {booking.room.location}" if booking.room.location else "")
    description = f"Бронь переговорной в AiSpace. Организатор: {booking.user.full_name}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AiSpace//Room Booking//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:booking-{booking.id}@{host}",
        f"DTSTAMP:{_fmt(datetime.now(UTC))}",
        f"DTSTART:{_fmt(booking.start_at)}",
        f"DTEND:{_fmt(booking.end_at)}",
        f"SUMMARY:{_escape(booking.title)}",
        f"LOCATION:{_escape(location)}",
        f"DESCRIPTION:{_escape(description)}",
        f"STATUS:{status}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
