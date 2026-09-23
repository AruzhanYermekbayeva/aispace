from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api import auth, bookings, nl, rooms
from app.db import SessionFactory
from app.errors import DomainError, Unauthorized
from app.web import routes as web

log = logging.getLogger("aispace")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """CSRF-защита для cookie-сессий: изменяющий запрос из браузера должен прийти с нашего
    же origin. Вместе с SameSite=Lax этого достаточно без CSRF-токенов в каждой форме.
    Запросы без Origin/Referer (curl, серверные клиенты с Bearer) пропускаются."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in UNSAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            foreign = (
                source
                and source != "null"
                and urlsplit(source).netloc != request.headers.get("host")
            )
            if foreign:
                return JSONResponse(
                    {
                        "error": {
                            "code": "csrf",
                            "message": "Запрос с чужого источника",
                            "details": {},
                        }
                    },
                    status_code=403,
                )
        return await call_next(request)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AiSpace",
        description="Бронирование переговорных комнат. JSON API: /api/v1, веб-интерфейс: /",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.add_middleware(OriginCheckMiddleware)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> Response:
        if not _is_api(request):
            if isinstance(exc, Unauthorized):
                return web.redirect(request, f"/login?next={request.url.path}")
            return web.render_error(request, exc)
        headers = {}
        if retry := exc.details.get("retry_after_seconds"):
            headers["Retry-After"] = str(retry)
        return JSONResponse(exc.to_dict(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "Некорректные данные запроса",
                    "details": {"errors": errors},
                }
            },
            status_code=422,
        )

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth.router)
    api.include_router(rooms.router)
    api.include_router(nl.router)  # до bookings: /bookings/parse не должен матчиться как {id}
    api.include_router(bookings.router)

    @api.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(api)
    app.include_router(web.router)
    app.mount(
        "/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static"
    )
    return app


app = create_app()
