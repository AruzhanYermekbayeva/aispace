#!/bin/sh
# Старт контейнера приложения: миграции → начальные данные → сервер.
# Postgres к этому моменту уже здоров (depends_on: service_healthy в compose).
set -e

echo "==> alembic upgrade head"
alembic upgrade head

echo "==> seed (admin + demo rooms, idempotent)"
python -m app.seed

echo "==> uvicorn on :8000"
# Один воркер сознательно: rate limiter хранит состояние в памяти процесса.
# Для 60 человек async-воркера хватает с большим запасом.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
