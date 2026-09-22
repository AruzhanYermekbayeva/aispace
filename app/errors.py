"""Доменные ошибки. Сервисный слой бросает их, а API и веб-слой решают, как показать.

Формат ответа API единый:
    {"error": {"code": "booking_conflict", "message": "...", "details": {...}}}
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class BookingConflict(Conflict):
    """Слот занят. Кроме JSON-деталей несёт объекты для веб-слоя (конфликты и альтернативы)."""

    code = "booking_conflict"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        conflicts: list[Any] | None = None,
        alternatives: Any = None,
    ) -> None:
        super().__init__(message, details=details)
        self.conflicts = conflicts or []
        self.alternatives = alternatives


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_error"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message, details={"field": field} if field else None)
        self.field = field


class RateLimited(DomainError):
    status_code = 429
    code = "rate_limited"


class ExternalServiceError(DomainError):
    """Отказ внешнего сервиса (LLM). Остальной сервис при этом продолжает работать."""

    status_code = 503
    code = "llm_unavailable"


class LLMNotConfigured(ExternalServiceError):
    code = "llm_not_configured"


class LLMBadResponse(ExternalServiceError):
    status_code = 502
    code = "llm_bad_response"
