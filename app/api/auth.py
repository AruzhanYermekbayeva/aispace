from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.config import get_settings
from app.deps import DB, SESSION_COOKIE, CurrentUser, extract_token
from app.ratelimit import RateLimiter
from app.schemas import LoginIn, LoginOut, RegisterIn, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

# Защита от перебора паролей: не больше 10 попыток в минуту на связку email+IP.
login_limiter = RateLimiter(limit=10, window_seconds=60)


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterIn, session: DB) -> UserOut:
    user = await auth_service.register_user(
        session, email=payload.email, full_name=payload.full_name, password=payload.password
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, request: Request, response: Response, session: DB) -> LoginOut:
    login_limiter.check(f"{auth_service.normalize_email(payload.email)}|{client_ip(request)}")
    user = await auth_service.authenticate(session, email=payload.email, password=payload.password)
    token = await auth_service.create_session(session, user)
    set_session_cookie(response, token)
    return LoginOut(user=UserOut.model_validate(user), token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: DB) -> None:
    if token := extract_token(request):
        await auth_service.destroy_session(session, token)
    response.delete_cookie(SESSION_COOKIE)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
