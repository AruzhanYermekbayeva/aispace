# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при изменении pyproject.toml.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh

RUN useradd --system --uid 10001 --no-create-home aispace && chmod +x /entrypoint.sh
USER aispace

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"

ENTRYPOINT ["/entrypoint.sh"]

# ---------------------------------------------------------------- test
# docker compose --profile test run --rm tests
FROM runtime AS test
USER root
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['dependency-groups']['dev']))" > /tmp/dev.txt \
 && pip install -r /tmp/dev.txt
COPY tests ./tests
USER aispace
ENTRYPOINT []
CMD ["sh", "-c", "ruff check --no-cache app tests && python -m pytest -q -p no:cacheprovider"]
