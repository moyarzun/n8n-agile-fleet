.PHONY: setup start stop restart logs dashboard status install install-claude install-gemini

REPO_DIR := $(shell pwd)
FLEET_SCRIPT := $(REPO_DIR)/agile_scripts/mcp_fleet_server.py

# ── Setup ─────────────────────────────────────────────────────────────────────

setup:
	@if [ ! -f .env ]; then cp .env.example .env; echo "✓ .env creado desde .env.example — edítalo con tus API keys."; \
	else echo "✓ .env ya existe."; fi

# ── Docker ────────────────────────────────────────────────────────────────────

start: setup
	docker compose up -d
	@echo "✓ Fleet corriendo en http://localhost:8000"
	@echo "  Dashboard: http://localhost:8000/"

stop:
	docker compose down

restart:
	docker compose restart fleet-api
	@echo "✓ fleet-api reiniciado"

logs:
	docker compose logs -f fleet-api

status:
	docker compose ps

dashboard:
	@which open > /dev/null 2>&1 && open http://localhost:8000/ || \
	which xdg-open > /dev/null 2>&1 && xdg-open http://localhost:8000/ || \
	echo "Abre http://localhost:8000/ en tu browser"

# ── Instalación de skill en agentes ──────────────────────────────────────────

install:
	@bash install.sh

install-claude:
	@bash install.sh --agent claude

install-gemini:
	@bash install.sh --agent gemini
