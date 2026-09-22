"""Клиент LLM. DeepSeek совместим с OpenAI Chat Completions, поэтому хватает httpx:
полный контроль над таймаутами, ретраями и разбором ошибок без лишнего SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings
from app.errors import ExternalServiceError, LLMBadResponse, LLMNotConfigured

log = logging.getLogger("aispace.llm")


class LLMClient(Protocol):
    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Вернуть JSON-объект, который модель сгенерировала в ответ на промпт."""
        ...


class DeepSeekClient:
    def __init__(
        self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport  # для тестов: httpx.MockTransport

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        s = self.settings
        if not s.llm_enabled:
            raise LLMNotConfigured(
                "Разбор фраз не настроен (не задан DEEPSEEK_API_KEY). Заполните форму вручную."
            )
        payload = {
            "model": s.deepseek_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 500,
        }
        headers = {"Authorization": f"Bearer {s.deepseek_api_key}"}
        url = s.deepseek_base_url.rstrip("/") + "/chat/completions"

        # Ретраим только то, что имеет смысл повторить: сетевые сбои, 429 и 5xx.
        # Таймаут не ретраим — пользователь и так ждал llm_timeout_seconds.
        last_error = "нет ответа"
        for attempt in range(s.llm_max_retries + 1):
            if attempt:
                await asyncio.sleep(0.5 * attempt)
            try:
                async with httpx.AsyncClient(
                    timeout=s.llm_timeout_seconds, transport=self._transport
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                log.warning("DeepSeek timeout after %ss", s.llm_timeout_seconds)
                raise ExternalServiceError(
                    "Сервис разбора фраз не ответил вовремя. Попробуйте ещё раз или "
                    "заполните форму вручную."
                ) from exc
            except httpx.TransportError as exc:
                last_error = f"сетевая ошибка: {exc.__class__.__name__}"
                log.warning("DeepSeek transport error (attempt %d): %s", attempt + 1, exc)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                log.warning("DeepSeek HTTP %s (attempt %d)", resp.status_code, attempt + 1)
                continue
            if resp.status_code in (401, 403):
                log.error("DeepSeek rejected API key: HTTP %s", resp.status_code)
                raise ExternalServiceError(
                    "Сервис разбора фраз отклонил ключ API — сообщите администратору."
                )
            if resp.status_code == 402:
                log.error("DeepSeek: insufficient balance")
                raise ExternalServiceError(
                    "У сервиса разбора фраз закончился баланс — сообщите администратору."
                )
            if resp.status_code >= 400:
                log.error("DeepSeek HTTP %s: %s", resp.status_code, resp.text[:500])
                raise LLMBadResponse("Сервис разбора фраз вернул ошибку. Заполните форму вручную.")
            return _extract_json(resp)

        raise ExternalServiceError(
            f"Сервис разбора фраз сейчас недоступен ({last_error}). Заполните форму вручную."
        )


def _extract_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.error("DeepSeek returned unparsable body: %s", resp.text[:500])
        raise LLMBadResponse(
            "Не удалось разобрать ответ модели. Попробуйте переформулировать."
        ) from exc
    if not isinstance(data, dict):
        raise LLMBadResponse("Не удалось разобрать ответ модели. Попробуйте переформулировать.")
    return data


def get_llm_client() -> LLMClient:
    return DeepSeekClient()
