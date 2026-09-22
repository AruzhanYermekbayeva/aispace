from __future__ import annotations

import httpx

from tests.conftest import PASSWORD, client, create_user


async def test_register_login_me_logout() -> None:
    async with client() as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": " Ivan@AiSpace.local ", "full_name": " Иван  Петров ",
                  "password": PASSWORD},
        )  # fmt: skip
        assert r.status_code == 201, r.text
        assert r.json()["email"] == "ivan@aispace.local"
        assert r.json()["full_name"] == "Иван Петров"

        r = await c.post(
            "/api/v1/auth/login", json={"email": "IVAN@aispace.local", "password": PASSWORD}
        )
        assert r.status_code == 200
        token = r.json()["token"]
        assert "aispace_session" in r.cookies

        assert (await c.get("/api/v1/auth/me")).json()["email"] == "ivan@aispace.local"

    # тот же токен работает как Bearer (для API-клиентов без cookie)
    async with client() as c2:
        r = await c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert (await c2.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
                ).status_code == 204  # fmt: skip
        # серверная сессия отозвана — токен больше не действует
        r = await c2.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


async def test_register_rejects_foreign_domain() -> None:
    async with client() as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "hacker@gmail.com", "full_name": "X", "password": PASSWORD},
        )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"]["field"] == "email"
    assert "@aispace.local" in body["message"]


async def test_register_duplicate_email_case_insensitive() -> None:
    await create_user("dup@aispace.local")
    async with client() as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "DUP@aispace.local", "full_name": "X", "password": PASSWORD},
        )
    assert r.status_code == 409


async def test_register_short_password() -> None:
    async with client() as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "a@aispace.local", "full_name": "X", "password": "short"},
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["field"] == "password"


async def test_login_same_error_for_wrong_password_and_unknown_user() -> None:
    await create_user("known@aispace.local")
    async with client() as c:
        wrong = await c.post(
            "/api/v1/auth/login", json={"email": "known@aispace.local", "password": "nope-nope"}
        )
        unknown = await c.post(
            "/api/v1/auth/login", json={"email": "ghost@aispace.local", "password": "nope-nope"}
        )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_login_rate_limited() -> None:
    await create_user("brute@aispace.local")
    async with client() as c:
        for _ in range(10):
            await c.post(
                "/api/v1/auth/login", json={"email": "brute@aispace.local", "password": "bad-pass"}
            )
        r = await c.post(
            "/api/v1/auth/login", json={"email": "brute@aispace.local", "password": PASSWORD}
        )
    assert r.status_code == 429
    assert "Retry-After" in r.headers


async def test_api_requires_auth_and_web_redirects_to_login() -> None:
    async with client() as c:
        r = await c.get("/api/v1/bookings/my")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

        r = await c.get("/schedule")
        assert r.status_code == 303
        assert r.headers["location"].startswith("/login")


async def test_only_admin_manages_rooms(alice: httpx.AsyncClient, admin: httpx.AsyncClient) -> None:
    payload = {"name": "Новая", "capacity": 6, "aliases": ["  Новенькая ", "новенькая"]}
    assert (await alice.post("/api/v1/rooms", json=payload)).status_code == 403

    r = await admin.post("/api/v1/rooms", json=payload)
    assert r.status_code == 201
    assert r.json()["aliases"] == ["новенькая"]  # нормализованы и без дублей
    assert (await admin.post("/api/v1/rooms", json=payload)).status_code == 409

    room_id = r.json()["id"]
    r = await admin.patch(f"/api/v1/rooms/{room_id}", json={"is_active": False})
    assert r.json()["is_active"] is False
    assert all(x["id"] != room_id for x in (await alice.get("/api/v1/rooms")).json())


async def test_csrf_foreign_origin_rejected(alice: httpx.AsyncClient) -> None:
    r = await alice.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf"
    # свой origin проходит
    r = await alice.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 204
