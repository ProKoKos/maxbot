.PHONY: up down logs migrate shell-web shell-db ps build

# ── Docker lifecycle ──────────────────────────────────────────────────────────

up:
	@cp -n .env.example .env 2>/dev/null || true
	docker compose up --build -d
	@echo ""
	@echo "✅  MaxBot is running."
	@echo "    Dashboard → http://localhost:8000"
	@echo "    Logs      → make logs"

down:
	docker compose down

restart:
	docker compose restart

build:
	docker compose build --no-cache

# ── Logs ──────────────────────────────────────────────────────────────────────

logs:
	docker compose logs -f --tail=100

logs-bot:
	docker compose logs -f --tail=100 bot

logs-web:
	docker compose logs -f --tail=100 web

logs-scheduler:
	docker compose logs -f --tail=100 scheduler

# ── Database ──────────────────────────────────────────────────────────────────

migrate:
	docker compose run --rm migrate

migrate-create:
	@read -p "Migration name: " name; \
	docker compose run --rm web alembic revision --autogenerate -m "$$name"

# ── Shells ────────────────────────────────────────────────────────────────────

shell-web:
	docker compose exec web bash

shell-db:
	docker compose exec db psql -U $${POSTGRES_USER:-maxbot} -d $${POSTGRES_DB:-maxbot}

# ── Status ────────────────────────────────────────────────────────────────────

ps:
	docker compose ps
