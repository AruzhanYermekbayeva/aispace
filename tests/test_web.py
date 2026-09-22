"""Сквозной сценарий через веб-интерфейс (HTML-формы и HTMX-запросы)."""

from __future__ import annotations

from typing import Any

import httpx

from app.errors import ExternalServiceError
from app.models import Room
from tests.conftest import PASSWORD, at, client, create_user

HX = {"HX-Request": "true"}


def form(room: Room, day: int, start: str, end: str, title: str = "Встреча") -> dict[str, Any]:
    return {"room_id": room.id, "date": at(day, start).date().isoformat(), "start": start,
            "end": end, "title": title}  # fmt: skip


async def test_full_web_scenario(rooms: dict[str, Room]) -> None:
    async with client() as c:
        # регистрация → сразу залогинены
        r = await c.post("/register", data={"email": "web@aispace.local", "full_name": "Веб",
                                            "password": PASSWORD})  # fmt: skip
        assert r.status_code == 303 and r.headers["location"] == "/schedule"

        page = (await c.get("/schedule")).text
        assert "Большая" in page and "Аквариум" in page

        # бронь через форму (HTMX) → HX-Redirect на расписание с подсветкой
        r = await c.post("/bookings", data=form(rooms["big"], 1, "10:00", "11:00", "Планёрка"),
                         headers=HX)  # fmt: skip
        assert r.status_code == 200
        target = r.headers["HX-Redirect"]
        assert "created=" in target

        page = (await c.get(target)).text
        assert "Забронировано" in page and "Планёрка" in page and "calendar.ics" in page

        # конфликт → та же форма с ошибкой и альтернативами, статус 409
        r = await c.post("/bookings", data=form(rooms["big"], 1, "10:30", "11:30"), headers=HX)
        assert r.status_code == 409
        assert "уже занята" in r.text
        assert "Свободно в этой комнате" in r.text
        assert "Свободные комнаты на это время" in r.text

        # мои брони → отмена через HTMX возвращает обновлённую строку
        page = (await c.get("/my")).text
        assert "Планёрка" in page
        booking_id = int(target.split("created=")[1])
        r = await c.post(f"/bookings/{booking_id}/cancel", headers=HX)
        assert r.status_code == 200 and "Отменено" in r.text

        # выход → расписание снова требует входа
        await c.post("/logout")
        assert (await c.get("/schedule")).status_code == 303


async def test_week_view(alice: httpx.AsyncClient, rooms: dict[str, Room]) -> None:
    r = await alice.get("/schedule", params={"view": "week", "room_id": rooms["mid"].id})
    assert r.status_code == 200
    assert "Аквариум ·" in r.text
    assert r.text.count('class="tl-row') == 8  # заголовок + 7 дней


async def test_htmx_navigation_returns_partial(
    alice: httpx.AsyncClient, rooms: dict[str, Room]
) -> None:
    r = await alice.get("/schedule", params={"date": at(1, "10:00").date().isoformat()},
                        headers=HX)  # fmt: skip
    assert r.status_code == 200
    assert "<html" not in r.text and "tl-row" in r.text


async def test_web_login_errors_and_safe_next() -> None:
    await create_user("w@aispace.local")
    async with client() as c:
        r = await c.post("/login", data={"email": "w@aispace.local", "password": "wrong-pass"})
        assert r.status_code == 401 and "Неверный email или пароль" in r.text
        # open redirect не проходит
        r = await c.post("/login", data={"email": "w@aispace.local", "password": PASSWORD,
                                         "next": "//evil.example"})  # fmt: skip
        assert r.headers["location"] == "/schedule"


async def test_web_parse_falls_back_when_llm_down(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(ExternalServiceError("Сервис разбора фраз сейчас недоступен"))
    r = await alice.post("/bookings/parse", data={"text": "большая завтра в 14"}, headers=HX)
    assert r.status_code == 200
    assert "сейчас недоступен" in r.text
    assert 'hx-post="/bookings"' in r.text  # обычная форма на месте
    assert "большая завтра в 14" in r.text  # фраза не потерялась


async def test_web_parse_prefills_form(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm({"room_id": rooms["small"].id, "date": at(1, "14:00").date().isoformat(),
              "start_time": "14:00", "duration_minutes": 90, "title": "Релиз"})  # fmt: skip
    r = await alice.post("/bookings/parse", data={"text": "малая завтра 14 полтора часа"},
                         headers=HX)  # fmt: skip
    assert r.status_code == 200
    assert f'<option value="{rooms["small"].id}" selected>' in r.text
    assert "<option selected>14:00</option>" in r.text
    assert "<option selected>15:30</option>" in r.text
    assert 'value="nl"' in r.text


async def test_admin_pages(alice: httpx.AsyncClient, admin: httpx.AsyncClient) -> None:
    assert (await alice.get("/admin/rooms")).status_code == 403
    r = await admin.post("/admin/rooms", data={"name": "Лофт", "capacity": 10,
                                               "aliases": "лофт, мансарда"})  # fmt: skip
    assert r.status_code == 303
    page = (await admin.get("/admin/rooms")).text
    assert "Лофт" in page and "лофт, мансарда" in page
    r = await admin.post("/admin/rooms", data={"name": "Лофт", "capacity": 10})
    assert r.status_code == 409 and "уже есть" in r.text


async def test_health() -> None:
    async with client() as c:
        assert (await c.get("/api/v1/health")).json() == {"status": "ok"}
