from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import SessionFactory
from app.models import Room
from tests.conftest import at, create_user, iso, login

API = "/api/v1/bookings"


def body(room: Room, day: int, start: str, end: str, title: str = "Встреча") -> dict[str, Any]:
    return {
        "room_id": room.id,
        "start_at": iso(day, start),
        "end_at": iso(day, end),
        "title": title,
    }


async def test_create_booking(alice: httpx.AsyncClient, rooms: dict[str, Room]) -> None:
    r = await alice.post(
        API, json=body(rooms["big"], 1, "14:00", "15:30", "  Обсуждение   релиза ")
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["title"] == "Обсуждение релиза"
    assert data["room_name"] == "Большая"
    assert data["user_name"] == "Алиса"
    assert data["status"] == "active"
    assert data["source"] == "form"


async def test_overlap_rejected_with_alternatives(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    assert (await alice.post(API, json=body(rooms["big"], 1, "14:00", "15:00"))).status_code == 201

    r = await bob.post(API, json=body(rooms["big"], 1, "14:30", "15:30"))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "booking_conflict"
    assert [c["user_name"] for c in err["details"]["conflicts"]] == ["Алиса"]

    # альтернативные слоты — той же длительности, свободны, ближайшие к желаемому
    slots = err["details"]["alternative_slots"]
    assert slots, "должны быть свободные слоты"
    assert {"start_at": iso(1, "15:00"), "end_at": iso(1, "16:00")} in slots
    for s in slots:
        assert not (s["start_at"] < iso(1, "15:00") and s["end_at"] > iso(1, "14:00"))
    # варианты различаются, а не сдвинуты на 15 минут друг от друга
    for x, y in pairwise(slots):
        assert x["end_at"] <= y["start_at"]
    # другие свободные комнаты на это же время
    names = [x["room_name"] for x in err["details"]["alternative_rooms"]]
    assert set(names) == {"Аквариум", "Малая"}


@pytest.mark.parametrize(
    ("start", "end"),
    [("13:00", "14:15"), ("14:15", "14:45"), ("13:00", "16:00"), ("14:00", "15:00")],
    ids=["overlaps-start", "inside", "covers", "exact"],
)
async def test_all_overlap_shapes_rejected(
    alice: httpx.AsyncClient, rooms: dict[str, Room], start: str, end: str
) -> None:
    assert (await alice.post(API, json=body(rooms["big"], 1, "14:00", "15:00"))).status_code == 201
    assert (await alice.post(API, json=body(rooms["big"], 1, start, end))).status_code == 409


async def test_adjacent_and_other_room_allowed(
    alice: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    assert (await alice.post(API, json=body(rooms["big"], 1, "14:00", "15:00"))).status_code == 201
    # полуоткрытый интервал: конец одной = начало другой — не конфликт
    assert (await alice.post(API, json=body(rooms["big"], 1, "15:00", "16:00"))).status_code == 201
    assert (await alice.post(API, json=body(rooms["big"], 1, "13:00", "14:00"))).status_code == 201
    assert (await alice.post(API, json=body(rooms["mid"], 1, "14:00", "15:00"))).status_code == 201


async def test_cancel_frees_slot(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    booking_id = (await alice.post(API, json=body(rooms["big"], 1, "10:00", "11:00"))).json()["id"]
    assert (await bob.post(API, json=body(rooms["big"], 1, "10:00", "11:00"))).status_code == 409

    r = await alice.post(f"{API}/{booking_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert (await alice.post(f"{API}/{booking_id}/cancel")).status_code == 409  # уже отменена

    assert (await bob.post(API, json=body(rooms["big"], 1, "10:00", "11:00"))).status_code == 201


async def test_cancel_permissions(
    alice: httpx.AsyncClient,
    bob: httpx.AsyncClient,
    admin: httpx.AsyncClient,
    rooms: dict[str, Room],
) -> None:
    booking_id = (await alice.post(API, json=body(rooms["big"], 1, "10:00", "11:00"))).json()["id"]
    assert (await bob.post(f"{API}/{booking_id}/cancel")).status_code == 403
    assert (await admin.post(f"{API}/{booking_id}/cancel")).status_code == 200
    assert (await alice.post(f"{API}/999999/cancel")).status_code == 404


@pytest.mark.parametrize(
    ("day", "start", "end", "field"),
    [
        (1, "15:00", "14:00", "end_at"),  # конец раньше начала
        (1, "14:00", "14:00", "end_at"),  # нулевая длительность
        (1, "14:10", "15:00", "start_at"),  # не кратно 15 минутам
        (1, "10:00", "15:00", "end_at"),  # дольше 4 часов
        (1, "07:00", "08:30", "start_at"),  # до начала рабочего дня
        (1, "19:30", "20:30", "start_at"),  # после конца рабочего дня
        (-1, "10:00", "11:00", "start_at"),  # в прошлом
        (90, "10:00", "11:00", "start_at"),  # дальше горизонта
    ],
    ids=["end-before-start", "zero", "unaligned", "too-long", "too-early", "too-late", "past",
         "too-far"],
)  # fmt: skip
async def test_time_rules(
    alice: httpx.AsyncClient, rooms: dict[str, Room], day: int, start: str, end: str, field: str
) -> None:
    r = await alice.post(API, json=body(rooms["big"], day, start, end))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["field"] == field


async def test_booking_across_midnight_rejected(
    alice: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    payload = {"room_id": rooms["big"].id, "start_at": iso(1, "19:00"),
               "end_at": iso(2, "09:00"), "title": "Ночная"}  # fmt: skip
    assert (await alice.post(API, json=payload)).status_code == 422


async def test_malformed_input(alice: httpx.AsyncClient, rooms: dict[str, Room]) -> None:
    r = await alice.post(API, json={"room_id": "abc", "start_at": "завтра", "title": ""})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["error"]["details"]["errors"]}
    assert {"room_id", "start_at", "end_at", "title"} <= fields

    r = await alice.post(API, json={**body(rooms["big"], 1, "10:00", "11:00"), "title": "   "})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["field"] == "title"


async def test_unknown_or_inactive_room(
    alice: httpx.AsyncClient, admin: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    payload = body(rooms["big"], 1, "10:00", "11:00")
    assert (await alice.post(API, json={**payload, "room_id": 424242})).status_code == 404
    await admin.patch(f"/api/v1/rooms/{rooms['big'].id}", json={"is_active": False})
    assert (await alice.post(API, json=payload)).status_code == 404


async def test_naive_datetime_is_office_time(
    alice: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    start = at(1, "10:00")
    naive = {"room_id": rooms["big"].id, "title": "X",
             "start_at": start.replace(tzinfo=None).isoformat(),
             "end_at": at(1, "11:00").replace(tzinfo=None).isoformat()}  # fmt: skip
    r = await alice.post(API, json=naive)
    assert r.status_code == 201
    assert datetime.fromisoformat(r.json()["start_at"]) == start  # тот же абсолютный момент


async def test_my_bookings_and_schedule(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    await alice.post(API, json=body(rooms["big"], 1, "10:00", "11:00", "Алисина"))
    await bob.post(API, json=body(rooms["mid"], 1, "10:00", "11:00", "Бобова"))

    mine = (await alice.get(f"{API}/my")).json()
    assert [b["title"] for b in mine] == ["Алисина"]

    day = at(1, "10:00").date().isoformat()
    schedule = (await alice.get("/api/v1/schedule", params={"date": day})).json()
    by_room = {s["room"]["name"]: [b["title"] for b in s["bookings"]] for s in schedule}
    assert by_room == {"Большая": ["Алисина"], "Аквариум": ["Бобова"], "Малая": []}


async def test_ics_export(alice: httpx.AsyncClient, rooms: dict[str, Room]) -> None:
    booking_id = (
        await alice.post(API, json=body(rooms["big"], 1, "14:00", "15:30", "Релиз; v2, финал"))
    ).json()["id"]
    r = await alice.get(f"{API}/{booking_id}/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    ics = r.text
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert "SUMMARY:Релиз\\; v2\\, финал\r\n" in ics
    start_utc = at(1, "14:00").astimezone(UTC)
    assert f"DTSTART:{start_utc:%Y%m%dT%H%M%SZ}" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


# --------------------------------------------------------------------------- конкурентность


async def test_concurrent_requests_only_one_wins(rooms: dict[str, Room]) -> None:
    """Главная проверка: 10 одновременных запросов на один слот — ровно одна бронь.

    Каждый клиент — отдельная сессия и отдельное соединение с БД. Предварительная
    проверка в сервисе не спасает от гонки; спасает EXCLUDE-ограничение."""
    clients = []
    for i in range(10):
        await create_user(f"u{i}@aispace.local")
        clients.append(await login(f"u{i}@aispace.local"))
    try:
        responses = await asyncio.gather(
            *(c.post(API, json=body(rooms["big"], 2, "11:00", "12:00", f"#{i}"))
              for i, c in enumerate(clients))
        )  # fmt: skip
    finally:
        for c in clients:
            await c.aclose()
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 9, codes
    async with SessionFactory() as s:
        count = await s.scalar(
            text("SELECT count(*) FROM bookings WHERE status = 'active' AND room_id = :r"),
            {"r": rooms["big"].id},
        )
    assert count == 1


async def test_db_constraint_holds_without_application_code(rooms: dict[str, Room]) -> None:
    """Даже в обход сервиса (прямой SQL) база не даст сохранить пересечение."""
    user = await create_user("raw@aispace.local")
    insert = text(
        "INSERT INTO bookings (room_id, user_id, title, start_at, end_at) "
        "VALUES (:room, :user, 'raw', :s, :e)"
    )
    params = {"room": rooms["big"].id, "user": user.id}
    async with SessionFactory() as s:
        await s.execute(insert, {**params, "s": at(3, "10:00"), "e": at(3, "11:00")})
        await s.commit()
        with pytest.raises(IntegrityError) as exc_info:
            await s.execute(insert, {**params, "s": at(3, "10:30"), "e": at(3, "11:30")})
        assert getattr(exc_info.value.orig, "sqlstate", None) == "23P01"
        await s.rollback()
        # отменённая бронь в ограничении не участвует
        await s.execute(text("UPDATE bookings SET status = 'cancelled'"))
        await s.execute(insert, {**params, "s": at(3, "10:30"), "e": at(3, "11:30")})
        await s.commit()
