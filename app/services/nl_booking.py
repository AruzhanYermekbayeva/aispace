"""Бронь по фразе на естественном языке.

Разделение ответственности:
- LLM только извлекает параметры из фразы (llm/prompt.py);
- здесь детерминированный код решает всё остальное: какая комната, какое время,
  проходят ли правила, есть ли конфликт;
- бронь НЕ создаётся. Пользователь видит заполненную форму и подтверждает её —
  модель ошибается в датах, а молча созданная неверная бронь хуже, чем лишний клик.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import ValidationFailed
from app.llm.client import LLMClient
from app.llm.prompt import build_system_prompt, parse_llm_output
from app.models import Room
from app.ratelimit import RateLimiter
from app.services import bookings as booking_service
from app.services.bookings import Slot
from app.services.rooms import list_rooms
from app.timeutils import local_dt, now_utc, to_local

DEFAULT_DURATION_MIN = 60
DEFAULT_TITLE = "Встреча"

nl_limiter = RateLimiter(limit=get_settings().nl_rate_limit_per_minute, window_seconds=60)


def check_rate_limit(user_id: int) -> None:
    """Каждый разбор — платный запрос к LLM, поэтому ограничиваем частоту на пользователя."""
    nl_limiter.check(f"user:{user_id}", "Слишком много запросов на разбор фраз, подождите минуту")


@dataclass
class Draft:
    room: Room | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    title: str | None = None
    missing: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    conflict: bool = False
    alternative_slots: list[Slot] = field(default_factory=list)
    alternative_rooms: list[Room] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(
            self.room and self.start_at and self.end_at and self.title
            and not (self.missing or self.problems or self.conflict)
        )  # fmt: skip


async def draft_from_text(
    session: AsyncSession, llm: LLMClient, text: str, *, now: datetime | None = None
) -> Draft:
    s = get_settings()
    now = now or now_utc()
    text = " ".join(text.split())
    if not text:
        raise ValidationFailed("Напишите, что забронировать", field="text")
    if len(text) > s.nl_max_text_length:
        raise ValidationFailed(
            f"Слишком длинная фраза (максимум {s.nl_max_text_length} символов)", field="text"
        )

    rooms = await list_rooms(session)
    system = build_system_prompt(
        rooms,
        now_local=to_local(now),
        tz=s.office_tz,
        work_start=s.work_day_start,
        work_end=s.work_day_end,
    )
    parsed = parse_llm_output(await llm.complete_json(system, text))
    if not parsed.is_booking_request:
        raise ValidationFailed(
            "Не похоже на запрос брони. Пример: «малая переговорка завтра в 11 на час, созвон "
            "с клиентом»",
            field="text",
        )

    draft = Draft()

    # --- комната
    by_id = {r.id: r for r in rooms}
    if parsed.room_id is not None:
        draft.room = by_id.get(parsed.room_id)
        if draft.room is None:
            draft.problems.append("Не удалось определить комнату — выберите её в списке")

    # --- время
    today = to_local(now).date()
    if parsed.start_time is None:
        draft.missing.append("start_time")
    else:
        day = parsed.date
        if day is None:
            day = today
            draft.assumptions.append("Дата не указана — поставили сегодня")
        draft.start_at = local_dt(day, parsed.start_time)
        if parsed.end_time is not None:
            draft.end_at = local_dt(day, parsed.end_time)
        elif parsed.duration_minutes is not None:
            draft.end_at = draft.start_at + timedelta(minutes=parsed.duration_minutes)
        else:
            draft.end_at = draft.start_at + timedelta(minutes=DEFAULT_DURATION_MIN)
            draft.assumptions.append("Длительность не указана — поставили 1 час")

    # --- тема
    draft.title = parsed.title
    if not draft.title:
        draft.title = DEFAULT_TITLE
        draft.assumptions.append(f"Тема не указана — «{DEFAULT_TITLE}»")

    if draft.start_at is None or draft.end_at is None:
        if draft.room is None:
            draft.missing.append("room")
        return draft

    try:
        draft.start_at, draft.end_at = booking_service.validate_times(
            draft.start_at, draft.end_at, now=now
        )
    except ValidationFailed as exc:
        draft.problems.append(exc.message)
        if draft.room is None:
            draft.missing.append("room")
        return draft

    # --- комната не названа: подбираем по числу участников или предлагаем свободные
    if draft.room is None and not draft.problems:
        free = await booking_service.free_rooms(
            session, draft.start_at, draft.end_at, min_capacity=parsed.attendees or 1
        )
        if parsed.attendees and free:
            draft.room = free[0]
            draft.assumptions.append(
                f"Комната не указана — подобрали «{draft.room.name}»: свободна и вмещает "
                f"{parsed.attendees} чел."
            )
        else:
            draft.missing.append("room")
            draft.alternative_rooms = free[: booking_service.MAX_ALTERNATIVES]
            if parsed.attendees and not free:
                draft.problems.append(
                    f"На это время нет свободной комнаты на {parsed.attendees} чел."
                )
        return draft

    room = draft.room
    if room and await booking_service.find_conflicts(
        session, room.id, draft.start_at, draft.end_at
    ):
        draft.conflict = True
        alt = await booking_service.alternatives_for(
            session, room, draft.start_at, draft.end_at, now=now
        )
        draft.alternative_slots, draft.alternative_rooms = alt.slots, alt.rooms
    return draft
