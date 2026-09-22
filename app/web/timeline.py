"""Модель представления сетки занятости: считает проценты для позиционирования,
чтобы шаблон оставался «глупым»."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from app.config import get_settings
from app.models import Booking, User
from app.timeutils import to_local, work_bounds

CELL_MINUTES = 30


@dataclass
class Block:
    id: int
    left: float
    width: float
    title: str
    owner: str
    time_label: str
    mine: bool
    past: bool


@dataclass
class Cell:
    left: float
    width: float
    label: str
    href: str | None  # None — прошедшее время, бронировать нельзя


@dataclass
class Row:
    label: str
    sublabel: str
    href: str | None
    blocks: list[Block] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    now_left: float | None = None
    is_today: bool = False


def hour_marks() -> list[tuple[float, str]]:
    s = get_settings()
    start = s.work_day_start.hour * 60 + s.work_day_start.minute
    end = s.work_day_end.hour * 60 + s.work_day_end.minute
    total = end - start
    marks = []
    for minute in range(start + (-start % 60), end + 1, 60):
        marks.append((100 * (minute - start) / total, f"{minute // 60:02d}:00"))
    return marks


def build_row(
    *,
    label: str,
    sublabel: str,
    href: str | None,
    day: date,
    room_id: int,
    bookings: list[Booking],
    user: User,
    now: datetime,
) -> Row:
    day_start, day_end = work_bounds(day)
    total = (day_end - day_start).total_seconds()

    def pct(dt: datetime) -> float:
        return max(0.0, min(100.0, 100 * (dt - day_start).total_seconds() / total))

    row = Row(label=label, sublabel=sublabel, href=href)
    for b in bookings:
        ls, le = to_local(b.start_at), to_local(b.end_at)
        row.blocks.append(
            Block(
                id=b.id,
                left=pct(b.start_at),
                width=pct(b.end_at) - pct(b.start_at),
                title=b.title,
                owner=b.user.full_name,
                time_label=f"{ls:%H:%M}–{le:%H:%M}",
                mine=b.user_id == user.id,
                past=b.end_at <= now,
            )
        )
    t = day_start
    step = timedelta(minutes=CELL_MINUTES)
    while t < day_end:
        lt = to_local(t)
        href = None
        if t + step > now:
            params = {"room_id": room_id, "date": day.isoformat(), "start": f"{lt:%H:%M}"}
            href = "/bookings/new?" + urlencode(params)
        row.cells.append(
            Cell(
                left=pct(t),
                width=100 * step.total_seconds() / total,
                label=f"{lt:%H:%M}",
                href=href,
            )
        )
        t += step
    if day_start <= now < day_end:
        row.now_left = pct(now)
    row.is_today = to_local(now).date() == day
    return row
