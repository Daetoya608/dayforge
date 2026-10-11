# Dayforge: разработка локально и управление сервером. Список команд — `make help`.

REMOTE ?= root@82.25.58.76
BRANCH ?= dev
LINES ?= 100
SERVICE = dayforge
SESSIONS_DIR = /srv/dayforge/data/logs/sessions
SSH = ssh -o BatchMode=yes $(REMOTE)

.DEFAULT_GOAL := help
.PHONY: help install test lint fmt check migrate run \
	sync deploy status start stop restart logs logs-follow sessions session-log ssh

help: ## Показать команды
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo "\n  Переменные: REMOTE=$(REMOTE) BRANCH=$(BRANCH) LINES=$(LINES)"

# --- локально ---

install: ## Поставить зависимости (uv sync)
	uv sync

test: ## Тесты
	uv run pytest -q

lint: ## Линтер и проверка форматирования
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Отформатировать код и поправить то, что ruff умеет сам
	uv run ruff format .
	uv run ruff check --fix .

check: lint test ## Линтер + тесты — перед коммитом

migrate: ## Применить миграции к локальной БД (BASE_DIR из .env)
	uv run alembic upgrade head

run: migrate ## Запустить Dayforge локально (откажется, если серверный бот работает; FORCE=1 — всё равно)
	@if [ -z "$(FORCE)" ] && $(SSH) -o ConnectTimeout=5 systemctl is-active -q $(SERVICE) 2>/dev/null; then \
		echo "На $(REMOTE) работает $(SERVICE): два процесса с одним токеном бота мешают друг другу."; \
		echo "Останови сервер (make stop) или запусти с FORCE=1."; exit 1; fi
	uv run python -m dayforge

# --- сервер ---

sync: ## Синхронизировать ветку BRANCH на сервер: подготовка, push, обновление, перезапуск
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "Есть незакоммиченные изменения — на сервер уйдёт только закоммиченное в $(BRANCH)."; fi
	deploy/deploy.sh $(REMOTE) $(BRANCH)

deploy: sync ## То же, что sync

status: ## Статус службы, версия кода, активные сессии агентов
	@$(SSH) 'systemctl status $(SERVICE) --no-pager -n 0 | head -5; \
		echo "код: $$(git --git-dir=/srv/dayforge/dayforge.git log -1 --format="%h %s" $(BRANCH))"; \
		echo "процессов агентов: $$(pgrep -u dayforge-agent -c -f "claude|codex" || true)"'

start: ## Запустить службу на сервере
	$(SSH) systemctl start $(SERVICE)
	@$(SSH) systemctl is-active $(SERVICE)

stop: ## Остановить службу (текущая задача встанет на паузу и продолжится после запуска)
	$(SSH) systemctl stop $(SERVICE)
	@$(SSH) systemctl is-active $(SERVICE) || true

restart: ## Перезапустить службу
	$(SSH) systemctl restart $(SERVICE)
	@$(SSH) systemctl is-active $(SERVICE)

logs: ## Последние LINES строк журнала службы
	@$(SSH) journalctl -u $(SERVICE) --no-pager -n $(LINES) | grep -vE 'apscheduler|pam_unix\(sudo'

logs-follow: ## Журнал службы в реальном времени (Ctrl+C — выход)
	ssh -t $(REMOTE) journalctl -u $(SERVICE) -f -n $(LINES)

sessions: ## Последние сессии агентов (файлы логов)
	@$(SSH) 'ls -lt $(SESSIONS_DIR) | head -15'

session-log: ## Хвост лога сессии агента: make session-log ID=42
	@test -n "$(ID)" || { echo "Укажи ID=<номер сессии>"; exit 1; }
	@$(SSH) tail -n $(LINES) $(SESSIONS_DIR)/$(ID).jsonl

ssh: ## Открыть SSH-сессию на сервере
	ssh $(REMOTE)
