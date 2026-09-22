"""Веб-интерфейс: серверный рендеринг (Jinja2) + HTMX для частичных обновлений.

Этот модуль — тонкий адаптер: разбирает формы, вызывает те же сервисы, что и JSON API,
и рендерит шаблоны. Бизнес-правил здесь нет.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.api.auth import client_ip, login_limiter, set_session_cookie
from app.config import get_settings
from app.deps import DB, SESSION_COOKIE, AdminUser, CurrentUser, OptionalUser
from app.errors import BookingConflict, DomainError, ExternalServiceError, ValidationFailed
from app.llm.client import LLMClient, get_llm_client
from app.models import BookingSource, BookingStatus, User
from app.schemas import RoomIn, RoomPatch
from app.services import auth as auth_service
from app.services import bookings as booking_service
from app.services import rooms as room_service
from app.services.nl_booking import Draft, check_rate_limit, draft_from_text
from app.timeutils import (
    human_date,
    human_range,
    local_dt,
    local_today,
    now_utc,
    to_local,
    week_start,
    work_bounds,
)
from app.web.timeline import build_row, hour_marks

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(
    human_date=human_date,
    human_range=human_range,
    to_local=to_local,
    settings=get_settings(),
)

LLM = Annotated[LLMClient, Depends(get_llm_client)]
ROOM_FORM_ERROR = "Проверьте название и вместимость"


# --------------------------------------------------------------------------- helpers


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def render(
    request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx or {}, status_code=status_code)


def redirect(request: Request, url: str) -> Response:
    """Обычный 303 для браузера; для HTMX — HX-Redirect, иначе он вставит страницу во фрагмент."""
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def render_error(request: Request, exc: DomainError) -> Response:
    ctx = {"error": exc, "user": getattr(request.state, "user", None)}
    if is_htmx(request):
        return render(
            request,
            "partials/flash.html",
            {"message": exc.message, "kind": "error"},
            status_code=exc.status_code,
        )
    return render(request, "error.html", ctx, status_code=exc.status_code)


def safe_next(url: str | None) -> str:
    # Только относительные пути — никаких open redirect на чужие сайты.
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/schedule"


def time_options(*, include_end: bool) -> list[str]:
    s = get_settings()
    step = timedelta(minutes=s.slot_minutes)
    t = datetime.combine(date.today(), s.work_day_start)
    end = datetime.combine(date.today(), s.work_day_end)
    out = []
    if not include_end:
        end -= step
    else:
        t += step
    while t <= end:
        out.append(f"{t:%H:%M}")
        t += step
    return out


def parse_day(value: str | None) -> date:
    if not value:
        return local_today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailed("Некорректная дата", field="date") from exc


def parse_hhmm(value: str, field: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationFailed("Некорректное время", field=field) from exc


# --------------------------------------------------------------------------- auth


@router.get("/")
async def index() -> Response:
    return RedirectResponse("/schedule", status_code=303)


@router.get("/login")
async def login_page(request: Request, user: OptionalUser, next: str | None = None) -> Response:
    if user:
        return redirect(request, safe_next(next))
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
async def login_submit(
    request: Request,
    session: DB,
    email: Annotated[str, Form(max_length=254)],
    password: Annotated[str, Form(max_length=128)],
    next: Annotated[str, Form()] = "/schedule",
) -> Response:
    try:
        login_limiter.check(f"{auth_service.normalize_email(email)}|{client_ip(request)}")
        user = await auth_service.authenticate(session, email=email, password=password)
    except DomainError as exc:
        return render(
            request,
            "login.html",
            {"error": exc.message, "email": email, "next": next},
            status_code=exc.status_code,
        )
    token = await auth_service.create_session(session, user)
    response = redirect(request, safe_next(next))
    set_session_cookie(response, token)
    return response


@router.get("/register")
async def register_page(request: Request) -> Response:
    return render(request, "register.html", {"domains": get_settings().allowed_email_domains})


@router.post("/register")
async def register_submit(
    request: Request,
    session: DB,
    email: Annotated[str, Form(max_length=254)],
    full_name: Annotated[str, Form(max_length=120)],
    password: Annotated[str, Form(max_length=128)],
) -> Response:
    try:
        user = await auth_service.register_user(
            session, email=email, full_name=full_name, password=password
        )
    except DomainError as exc:
        ctx = {
            "error": exc.message,
            "email": email,
            "full_name": full_name,
            "domains": get_settings().allowed_email_domains,
        }
        return render(request, "register.html", ctx, status_code=exc.status_code)
    token = await auth_service.create_session(session, user)
    response = redirect(request, "/schedule")
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout(request: Request, session: DB) -> Response:
    if token := request.cookies.get(SESSION_COOKIE):
        await auth_service.destroy_session(session, token)
    response = redirect(request, "/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------- расписание


@router.get("/schedule")
async def schedule(
    request: Request,
    session: DB,
    user: CurrentUser,
    day: Annotated[str | None, Query(alias="date")] = None,
    view: str = "day",
    room_id: int | None = None,
    created: int | None = None,
) -> Response:
    selected = parse_day(day)
    now = now_utc()
    rooms = await room_service.list_rooms(session)
    rows = []
    week_room = None
    heading = human_date(selected)
    prev_day, next_day = selected - timedelta(days=1), selected + timedelta(days=1)

    if view == "week" and rooms:
        week_room = next((r for r in rooms if r.id == room_id), rooms[0])
        monday = week_start(selected)
        start, _ = work_bounds(monday)
        _, end = work_bounds(monday + timedelta(days=6))
        bookings = await booking_service.bookings_between(session, start, end, room_id=week_room.id)
        for i in range(7):
            d = monday + timedelta(days=i)
            day_bookings = [b for b in bookings if to_local(b.start_at).date() == d]
            rows.append(build_row(
                label=human_date(d), sublabel="", day=d, room_id=week_room.id,
                href=f"/schedule?{urlencode({'date': d.isoformat()})}",
                bookings=day_bookings, user=user, now=now,
            ))  # fmt: skip
        prev_day, next_day = selected - timedelta(days=7), selected + timedelta(days=7)
        sunday = monday + timedelta(days=6)
        heading = f"{week_room.name} · {monday:%d.%m} — {sunday:%d.%m}"
    else:
        view = "day"
        start, end = work_bounds(selected)
        bookings = await booking_service.bookings_between(session, start, end)
        for r in rooms:
            week_link = urlencode({"date": selected.isoformat(), "view": "week", "room_id": r.id})
            rows.append(build_row(
                label=r.name, sublabel=" · ".join(filter(None, [f"{r.capacity} чел.", r.location])),
                href=f"/schedule?{week_link}", day=selected, room_id=r.id,
                bookings=[b for b in bookings if b.room_id == r.id], user=user, now=now,
            ))  # fmt: skip
        prev_day, next_day = selected - timedelta(days=1), selected + timedelta(days=1)
        heading = human_date(selected)

    ctx = {
        "user": user,
        "rows": rows,
        "heading": heading,
        "marks": hour_marks(),
        "view": view,
        "selected": selected,
        "today": local_today(),
        "prev_day": prev_day,
        "next_day": next_day,
        "rooms": rooms,
        "week_room": week_room,
        "created": await _created_booking(session, created, user),
    }
    template = "partials/timeline.html" if is_htmx(request) else "schedule.html"
    return render(request, template, ctx)


async def _created_booking(session: DB, booking_id: int | None, user: User) -> Any:
    if not booking_id:
        return None
    try:
        b = await booking_service.get_booking(session, booking_id)
    except DomainError:
        return None
    return b if b.user_id == user.id else None


# --------------------------------------------------------------------------- создание брони


def _form_ctx(user: User, rooms: list[Any], **values: Any) -> dict[str, Any]:
    ctx = {
        "user": user,
        "rooms": rooms,
        "start_options": time_options(include_end=False),
        "end_options": time_options(include_end=True),
        "form": {
            "room_id": None,
            "date": local_today().isoformat(),
            "start": "",
            "end": "",
            "title": "",
            "source": "form",
        },
        "error": None,
        "conflict": None,
        "draft": None,
        "nl_error": None,
        "nl_text": "",
        "llm_enabled": get_settings().llm_enabled,
    }
    ctx.update(values)
    # Альтернативы приходят либо из ошибки конфликта, либо из черновика по фразе.
    if ctx["conflict"] is not None:
        ctx["alt_slots"] = ctx["conflict"].alternatives.slots
        ctx["alt_rooms"] = ctx["conflict"].alternatives.rooms
    elif ctx["draft"] is not None:
        ctx["alt_slots"] = ctx["draft"].alternative_slots
        ctx["alt_rooms"] = ctx["draft"].alternative_rooms
    else:
        ctx["alt_slots"], ctx["alt_rooms"] = [], []
    return ctx


@router.get("/bookings/new")
async def new_booking(
    request: Request,
    session: DB,
    user: CurrentUser,
    room_id: int | None = None,
    day: Annotated[str | None, Query(alias="date")] = None,
    start: str | None = None,
    end: str | None = None,
    title: str = "",
) -> Response:
    rooms = await room_service.list_rooms(session)
    form = {
        "room_id": room_id,
        "date": day or local_today().isoformat(),
        "start": start or "",
        "end": end or "",
        "title": title,
        "source": "form",
    }
    if start and not end:
        try:
            st = datetime.combine(date.today(), parse_hhmm(start, "start")) + timedelta(hours=1)
            form["end"] = min(f"{st:%H:%M}", f"{get_settings().work_day_end:%H:%M}")
        except ValidationFailed:
            form["start"] = ""
    return render(request, "booking_new.html", _form_ctx(user, rooms, form=form))


@router.post("/bookings")
async def create_booking(
    request: Request,
    session: DB,
    user: CurrentUser,
    room_id: Annotated[int, Form()],
    day: Annotated[str, Form(alias="date")],
    start: Annotated[str, Form()],
    end: Annotated[str, Form()],
    title: Annotated[str, Form(max_length=300)],
    source: Annotated[str, Form()] = "form",
) -> Response:
    rooms = await room_service.list_rooms(session)
    form = {
        "room_id": room_id,
        "date": day,
        "start": start,
        "end": end,
        "title": title,
        "source": source,
    }
    try:
        d = parse_day(day)
        booking = await booking_service.create_booking(
            session,
            user,
            room_id=room_id,
            start_at=local_dt(d, parse_hhmm(start, "start")),
            end_at=local_dt(d, parse_hhmm(end, "end")),
            title=title,
            source=BookingSource.natural_language if source == "nl" else BookingSource.form,
        )
    except BookingConflict as exc:
        return render(
            request,
            "partials/booking_form.html",
            _form_ctx(user, rooms, form=form, error=exc.message, conflict=exc),
            status_code=409,
        )
    except DomainError as exc:
        return render(
            request,
            "partials/booking_form.html",
            _form_ctx(user, rooms, form=form, error=exc.message),
            status_code=exc.status_code,
        )
    day_param = to_local(booking.start_at).date().isoformat()
    return redirect(request, f"/schedule?{urlencode({'date': day_param, 'created': booking.id})}")


@router.post("/bookings/parse")
async def parse_booking(
    request: Request,
    session: DB,
    user: CurrentUser,
    llm: LLM,
    text: Annotated[str, Form(max_length=2000)],
) -> Response:
    rooms = await room_service.list_rooms(session)
    try:
        check_rate_limit(user.id)
        draft = await draft_from_text(session, llm, text)
    except (ExternalServiceError, ValidationFailed, DomainError) as exc:
        # Отказ LLM не ломает сценарий: пользователь видит причину и обычную форму.
        return render(
            request,
            "partials/booking_form.html",
            _form_ctx(user, rooms, nl_error=exc.message, nl_text=text),
        )
    return render(
        request,
        "partials/booking_form.html",
        _form_ctx(user, rooms, form=_draft_form(draft), draft=draft, nl_text=text),
    )


def _draft_form(d: Draft) -> dict[str, Any]:
    ls = to_local(d.start_at) if d.start_at else None
    le = to_local(d.end_at) if d.end_at else None
    return {
        "room_id": d.room.id if d.room else None,
        "date": ls.date().isoformat() if ls else local_today().isoformat(),
        "start": f"{ls:%H:%M}" if ls else "",
        "end": f"{le:%H:%M}" if le else "",
        "title": d.title or "",
        "source": "nl",
    }


# --------------------------------------------------------------------------- мои брони


@router.get("/my")
async def my_bookings(
    request: Request, session: DB, user: CurrentUser, past: bool = False
) -> Response:
    items = await booking_service.user_bookings(session, user, include_past=past)
    return render(
        request,
        "my_bookings.html",
        {
            "user": user,
            "bookings": items,
            "past": past,
            "now": now_utc(),
            "active": BookingStatus.active,
        },
    )


@router.post("/bookings/{booking_id}/cancel")
async def cancel(request: Request, session: DB, user: CurrentUser, booking_id: int) -> Response:
    booking = await booking_service.cancel_booking(session, user, booking_id)
    if is_htmx(request):
        return render(
            request,
            "partials/booking_row.html",
            {
                "b": booking,
                "user": user,
                "now": now_utc(),
                "active": BookingStatus.active,
                "just_cancelled": True,
            },
        )
    return redirect(request, request.headers.get("referer") or "/my")


# --------------------------------------------------------------------------- админка комнат


@router.get("/admin/rooms")
async def admin_rooms(request: Request, session: DB, user: AdminUser) -> Response:
    rooms = await room_service.list_rooms(session, include_inactive=True)
    return render(request, "admin_rooms.html", {"user": user, "rooms": rooms})


def _room_form_error(request: Request, user: User, rooms: list[Any], exc: Exception) -> Response:
    if isinstance(exc, DomainError):
        message, status_code = exc.message, exc.status_code
    else:  # ошибка pydantic-схемы
        message, status_code = ROOM_FORM_ERROR, 422
    ctx = {"user": user, "rooms": rooms, "error": message}
    return render(request, "admin_rooms.html", ctx, status_code=status_code)


def _aliases(raw: str) -> list[str]:
    return [a for a in (x.strip() for x in raw.split(",")) if a][:10]


@router.post("/admin/rooms")
async def admin_create_room(
    request: Request,
    session: DB,
    user: AdminUser,
    name: Annotated[str, Form()],
    capacity: Annotated[int, Form()],
    location: Annotated[str, Form()] = "",
    aliases: Annotated[str, Form()] = "",
) -> Response:
    try:
        data = RoomIn(
            name=name, capacity=capacity, location=location or None, aliases=_aliases(aliases)
        )
        await room_service.create_room(session, data)
    except (DomainError, ValueError) as exc:
        rooms = await room_service.list_rooms(session, include_inactive=True)
        return _room_form_error(request, user, rooms, exc)
    return redirect(request, "/admin/rooms")


@router.post("/admin/rooms/{room_id}")
async def admin_update_room(
    request: Request,
    session: DB,
    user: AdminUser,
    room_id: int,
    name: Annotated[str, Form()],
    capacity: Annotated[int, Form()],
    location: Annotated[str, Form()] = "",
    aliases: Annotated[str, Form()] = "",
    is_active: Annotated[bool, Form()] = False,
) -> Response:
    try:
        data = RoomPatch(
            name=name,
            capacity=capacity,
            location=location or None,
            aliases=_aliases(aliases),
            is_active=is_active,
        )
        await room_service.update_room(session, room_id, data)
    except (DomainError, ValueError) as exc:
        rooms = await room_service.list_rooms(session, include_inactive=True)
        return _room_form_error(request, user, rooms, exc)
    return redirect(request, "/admin/rooms")
