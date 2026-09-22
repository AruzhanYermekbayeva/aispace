"""Бронь фразой: разбор ответа модели, достраивание черновика и отказы внешнего сервиса.

DeepSeek в тестах не вызывается: сервисный уровень проверяем через FakeLLM, а сам
HTTP-клиент — через httpx.MockTransport (таймауты, 5xx, мусор в ответе).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.errors import ExternalServiceError, LLMBadResponse, LLMNotConfigured
from app.llm.client import DeepSeekClient
from app.models import Room
from tests.conftest import at, iso

PARSE = "/api/v1/bookings/parse"


def phrase(**overrides: Any) -> dict[str, Any]:
    base = {
        "is_booking_request": True,
        "room_id": None,
        "date": at(1, "14:00").date().isoformat(),
        "start_time": "14:00",
        "end_time": None,
        "duration_minutes": 90,
        "title": "Обсуждение релиза",
        "attendees": None,
    }
    return base | overrides


async def test_happy_path_draft_is_ready_and_nothing_is_created(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake = fake_llm(phrase(room_id=rooms["big"].id))
    text = "забронируй большую переговорку завтра с 14:00 на полтора часа, обсуждение релиза"
    r = await alice.post(PARSE, json={"text": text})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ready"] is True
    assert d["room_name"] == "Большая"
    assert d["start_at"] == iso(1, "14:00")
    assert d["end_at"] == iso(1, "15:30")
    assert d["title"] == "Обсуждение релиза"
    assert d["missing"] == d["problems"] == d["assumptions"] == []

    # в промпт ушли комнаты, «сегодня» и календарь; фраза — отдельным user-сообщением
    system, user = fake.calls[0]
    assert "Большая" in system and "(завтра)" in system
    assert user == text
    # парсинг ничего не создаёт
    assert (await alice.get("/api/v1/bookings/my")).json() == []

    # подтверждение — обычным POST /bookings с полями черновика
    r = await alice.post(
        "/api/v1/bookings",
        json={k: d[k] for k in ("room_id", "start_at", "end_at", "title")},
    )
    assert r.status_code == 201


async def test_defaults_are_reported_as_assumptions(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase(room_id=rooms["small"].id, duration_minutes=None, title=None))
    d = (await alice.post(PARSE, json={"text": "малая завтра в 14"})).json()
    assert d["end_at"] == iso(1, "15:00")
    assert d["title"] == "Встреча"
    assert len(d["assumptions"]) == 2
    assert d["ready"] is True


async def test_end_time_takes_precedence(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase(room_id=rooms["small"].id, end_time="16:15", duration_minutes=None))
    d = (await alice.post(PARSE, json={"text": "малая завтра с 14 до 16:15"})).json()
    assert d["end_at"] == iso(1, "16:15")


async def test_room_picked_by_attendees(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    # «Аквариум» (8) занят — для 6 человек остаётся только «Большая» (16)
    await bob.post("/api/v1/bookings", json={"room_id": rooms["mid"].id, "title": "x",
                   "start_at": iso(1, "14:00"), "end_at": iso(1, "15:00")})  # fmt: skip
    fake_llm(phrase(attendees=6))
    d = (await alice.post(PARSE, json={"text": "переговорка на 6 человек завтра в 14"})).json()
    assert d["room_name"] == "Большая"
    assert any("подобрали" in a for a in d["assumptions"])
    assert d["ready"] is True


async def test_room_missing_offers_free_rooms(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase())
    d = (await alice.post(PARSE, json={"text": "завтра в 14 на полтора часа"})).json()
    assert d["ready"] is False
    assert d["missing"] == ["room"]
    assert {r["room_name"] for r in d["alternative_rooms"]} == {"Большая", "Аквариум", "Малая"}


async def test_time_missing(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase(room_id=rooms["big"].id, start_time=None))
    d = (await alice.post(PARSE, json={"text": "большую на завтра"})).json()
    assert d["missing"] == ["start_time"]
    assert d["ready"] is False


async def test_conflict_in_draft_with_alternatives(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    await bob.post("/api/v1/bookings", json={"room_id": rooms["big"].id, "title": "x",
                   "start_at": iso(1, "14:00"), "end_at": iso(1, "15:00")})  # fmt: skip
    fake_llm(phrase(room_id=rooms["big"].id))
    d = (await alice.post(PARSE, json={"text": "большая завтра 14:00 полтора часа"})).json()
    assert d["conflict"] is True
    assert d["ready"] is False
    assert d["alternative_slots"] and d["alternative_rooms"]


async def test_llm_times_are_validated_by_business_rules(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase(room_id=rooms["big"].id, start_time="21:00", duration_minutes=60))
    d = (await alice.post(PARSE, json={"text": "большая завтра в 9 вечера"})).json()
    assert d["ready"] is False
    assert any("рабочие часы" in p for p in d["problems"])


async def test_unknown_room_id_from_model(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(phrase(room_id=9999))
    d = (await alice.post(PARSE, json={"text": "в комнате 9999 завтра"})).json()
    assert d["room_id"] is None
    assert d["ready"] is False


async def test_not_a_booking(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm({"is_booking_request": False})
    r = await alice.post(PARSE, json={"text": "какая погода завтра?"})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["field"] == "text"


@pytest.mark.parametrize(
    "raw",
    [{"start_time": "25:99"}, {"date": "послезавтра"}, {"duration_minutes": -5},
     {"room_id": "большая"}],
    ids=["bad-time", "bad-date", "negative-duration", "room-as-text"],
)  # fmt: skip
async def test_garbage_from_model_is_502(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any, raw: dict[str, Any]
) -> None:
    fake_llm(phrase(**raw))
    r = await alice.post(PARSE, json={"text": "что-то"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "llm_bad_response"


async def test_input_limits(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake = fake_llm(phrase())
    assert (await alice.post(PARSE, json={"text": "   "})).status_code == 422
    assert (await alice.post(PARSE, json={"text": "а" * 501})).status_code == 422
    assert fake.calls == []  # до модели не дошло


async def test_rate_limit(alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any) -> None:
    fake_llm(phrase(room_id=rooms["big"].id))
    for _ in range(10):
        assert (await alice.post(PARSE, json={"text": "большая завтра в 14"})).status_code == 200
    r = await alice.post(PARSE, json={"text": "большая завтра в 14"})
    assert r.status_code == 429


async def test_service_unavailable_is_503(
    alice: httpx.AsyncClient, rooms: dict[str, Room], fake_llm: Any
) -> None:
    fake_llm(ExternalServiceError("DeepSeek недоступен"))
    r = await alice.post(PARSE, json={"text": "большая завтра в 14"})
    assert r.status_code == 503
    # основной сценарий при этом работает
    r = await alice.post("/api/v1/bookings", json={"room_id": rooms["big"].id, "title": "x",
                         "start_at": iso(1, "14:00"), "end_at": iso(1, "15:00")})  # fmt: skip
    assert r.status_code == 201


# --------------------------------------------------------------------------- HTTP-клиент


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def make_client(handler: Any, **settings: Any) -> DeepSeekClient:
    s = Settings(deepseek_api_key="k", llm_max_retries=1, **settings)
    return DeepSeekClient(s, transport=httpx.MockTransport(handler))


async def test_client_success_sends_json_mode() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=completion('{"is_booking_request": true}'))

    assert await make_client(handler).complete_json("sys", "user") == {"is_booking_request": True}
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["temperature"] == 0
    assert seen["auth"] == "Bearer k"


async def test_client_retries_5xx_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=completion("{}"))

    assert await make_client(handler).complete_json("s", "u") == {}
    assert len(calls) == 2


async def test_client_gives_up_after_retries() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("boom")

    with pytest.raises(ExternalServiceError):
        await make_client(handler).complete_json("s", "u")
    assert len(calls) == 2  # 1 попытка + 1 ретрай


async def test_client_timeout_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ExternalServiceError, match="не ответил вовремя"):
        await make_client(handler).complete_json("s", "u")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 402])
async def test_client_auth_and_billing_errors(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status))
    with pytest.raises(ExternalServiceError, match="администратору"):
        await client.complete_json("s", "u")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json=completion("not json at all")),
        httpx.Response(200, json=completion("[1, 2, 3]")),
        httpx.Response(400, json={"error": "bad request"}),
    ],
    ids=["html", "no-choices", "content-not-json", "content-not-object", "400"],
)
async def test_client_bad_responses(response: httpx.Response) -> None:
    with pytest.raises(LLMBadResponse):
        await make_client(lambda r: response).complete_json("s", "u")


async def test_client_without_key() -> None:
    with pytest.raises(LLMNotConfigured):
        await DeepSeekClient(Settings(deepseek_api_key="  ")).complete_json("s", "u")
