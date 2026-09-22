.PHONY: up down logs test lint fmt reset

up:            ## поднять всё окружение
	docker compose up --build -d --wait
	@echo "→ http://localhost:$${APP_PORT:-8000}  (API: /api/docs)"

down:          ## остановить
	docker compose down

reset:         ## остановить и удалить данные БД
	docker compose down -v

logs:
	docker compose logs -f app

test:          ## линтер + тесты в Docker на отдельной временной БД
	docker compose --profile test run --rm --build tests

lint:
	ruff check . && ruff format --check .

fmt:
	ruff check --fix . && ruff format .
