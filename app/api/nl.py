from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps import DB, CurrentUser
from app.llm.client import LLMClient, get_llm_client
from app.schemas import DraftOut, ParseIn, RoomSlotOut, SlotOut
from app.services.nl_booking import Draft, draft_from_text, nl_limiter

router = APIRouter(prefix="/bookings", tags=["natural language"])

LLM = Annotated[LLMClient, Depends(get_llm_client)]


def draft_out(d: Draft) -> DraftOut:
    return DraftOut(
        room_id=d.room.id if d.room else None,
        room_name=d.room.name if d.room else None,
        start_at=d.start_at,
        end_at=d.end_at,
        title=d.title,
        missing=d.missing,
        assumptions=d.assumptions,
        problems=d.problems,
        conflict=d.conflict,
        alternative_slots=[
            SlotOut(start_at=s.start_at, end_at=s.end_at) for s in d.alternative_slots
        ],
        alternative_rooms=[
            RoomSlotOut(room_id=r.id, room_name=r.name, capacity=r.capacity)
            for r in d.alternative_rooms
        ],
        ready=d.ready,
    )


@router.post(
    "/parse",
    response_model=DraftOut,
    responses={
        422: {"description": "Фраза пустая, слишком длинная или не про бронь"},
        429: {"description": "Превышен лимит запросов к LLM"},
        502: {"description": "LLM вернула мусор"},
        503: {"description": "LLM недоступна или не настроена — используйте обычную форму"},
    },
)
async def parse_phrase(payload: ParseIn, session: DB, user: CurrentUser, llm: LLM) -> DraftOut:
    """Разобрать фразу в черновик брони. **Ничего не создаёт**: чтобы забронировать,
    отправьте поля черновика в `POST /bookings`."""
    nl_limiter.check(f"user:{user.id}", "Слишком много запросов на разбор фраз, подождите минуту")
    return draft_out(await draft_from_text(session, llm, payload.text))
