"""Работа со временем. Правило: в БД и в сервисах — aware UTC, в интерфейсе — часовой пояс офиса."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.config import get_settings


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(get_settings().tz)


def local_today() -> date:
    return to_local(now_utc()).date()


def ensure_aware(dt: datetime) -> datetime:
    """Наивное время трактуем как время офиса (так его вводят люди в форме)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=get_settings().tz)
    return dt


def local_dt(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=get_settings().tz)


def work_bounds(day: date) -> tuple[datetime, datetime]:
    s = get_settings()
    return local_dt(day, s.work_day_start), local_dt(day, s.work_day_end)


def floor_to_slot(dt: datetime) -> datetime:
    step = get_settings().slot_minutes
    local = to_local(dt)
    return local.replace(minute=local.minute - local.minute % step, second=0, microsecond=0)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip


def human_date(day: date) -> str:
    return f"{WEEKDAYS_SHORT_RU[day.weekday()]}, {day.day} {MONTHS_GEN_RU[day.month - 1]}"


def human_range(start: datetime, end: datetime) -> str:
    ls, le = to_local(start), to_local(end)
    return f"{human_date(ls.date())}, {ls:%H:%M}–{le:%H:%M}"
