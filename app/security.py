"""Хеширование паролей и токены сессий. Только стандартная библиотека.

Пароли — scrypt (hashlib), параметры по рекомендации OWASP: N=2^17, r=8, p=1.
Формат строки: scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64> — параметры хранятся рядом
с хешем, поэтому их можно усилить позже без миграции старых паролей.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

from app.config import get_settings

_R, _P, _DKLEN = 8, 1, 32
_MAXMEM = 256 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    n = get_settings().scrypt_n
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${n}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Хеш-заглушка: проверяем пароль и для несуществующего email, чтобы время ответа
# не выдавало, зарегистрирован ли адрес.
@lru_cache
def dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
