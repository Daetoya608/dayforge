# Разработка: как устроен код и как его менять

Документ для разработчика или агента, который продолжает работу над Dayforge. Что система делает — в остальных документах `docs/`; здесь — где это в коде, как проверять и что уже известно о граблях.

## Состояние

- **Реализовано всё из MVP** ([README](../README.ru.md#объём-mvp)): бот со всеми командами, планировщик и исполнитель, MCP, раннер со шлюзом, календарь, релизы, отчёты, бэкап (код), деплой-скрипты.
- **Проверено вживую:** Claude Code 2.1 и codex-cli 0.160–0.162 (вызов MCP, продолжение сессии, запуск от `dayforge-agent` через sudo), GitHub API (токен, создание репозитория), Telegram (бот), деплой на VPS.
- **Не настроено:** бэкап (нет restic-хранилища — код готов, задание не создаётся без `RESTIC_REPOSITORY`).
- **Не входит в MVP:** обнаружение ручных изменений пользователя в репозиториях проектов, изоляция агентов в Docker, другие git-хостинги, параллельные исполнители.

## Карта кода

```
dayforge/
├── __main__.py, app.py   # точка входа и сборка процесса
├── config.py             # Config / StorageConfig из окружения (.env)
├── services.py           # контейнер Services + интерфейс Orchestrator
├── orchestrator.py       # планировщик, утверждение плана, перепланирование, релиз, удаление
├── chat.py               # ChatAgent — диалог /agent с системным агентом
├── actions.py            # обработчики подтверждаемых действий + применение изменений календаря
├── prompts.py            # рендер Jinja-шаблонов из prompts/
├── core/                 # предметная область; единственный слой, который пишет в БД
├── db/                   # модели, типы, сессии, миграции Alembic
├── agents/               # запуск CLI агентов
├── runner/               # очередь и выполнение задач
├── mcp_server/           # MCP-сервер для агентов
├── bot/                  # Telegram
├── vcs/                  # git и GitHub
├── scheduler/            # задания по времени
└── backup/               # restic
```

| Модуль | Главное | Спецификация |
|---|---|---|
| `core/projects.py` | `ProjectService`: создание (БД + рабочая директория одной операцией), приоритеты, статусы, пауза; `suggest_slug` | architecture, git-workflow |
| `core/plan_format.py` | `PlanContent` (pydantic) и `check_plan` — все правила проверки плана | plan-format |
| `core/plan_diff.py`, `core/plan_render.py` | diff версий плана; генерация `PLAN.md` | plan-format |
| `core/plans.py` | `PlanService`: submit / approve (синхронизация задач и вех, инициализация репозитория) / reject / render / render_proposed | plan-format |
| `core/calendar.py` | чистые функции: `ProjectCalendar.is_slot`, `forecast` | calendar |
| `core/calendar_service.py` | `CalendarService`: шаблоны и даты в БД, прогноз проекта, `today()` в поясе из настроек | calendar |
| `core/tasks.py` | `TaskService`: очередь доделывания (`carry_over`), указатель дня, переходы статусов, разовые задачи | run-lifecycle |
| `core/runs.py` | `RunService`: очередь запусков с правилами склейки, ежедневный запуск по слотам | run-lifecycle |
| `core/questions.py`, `core/secrets.py` | вопросы агентов и секреты (`.env` проекта) | run-lifecycle, telegram-bot |
| `core/sessions.py` | сессии агентов: токен MCP (в БД — только хеш), исход сессии | mcp-tools |
| `core/pending_actions.py` | действия, ждущие подтверждения кнопкой | telegram-bot |
| `core/reports.py`, `core/releases.py`, `core/repos.py` | отчёты; вехи, релизы и учёт коммитов; публикация на хостинге | git-workflow |
| `core/events.py` | события ядра и `Notifier`; `RecordingNotifier` для тестов | architecture |
| `core/settings.py` | `SettingsService` и `DEFAULTS` — все глобальные настройки | data-model#settings |
| `agents/process.py` | `CliProcess`: процесс в своей группе, разбор JSON-строк, лог JSONL с маскированием | agents |
| `agents/claude_code.py`, `agents/codex.py` | флаги CLI и разбор потока событий — **только здесь** | agents |
| `agents/launcher.py` | `AgentLauncher.launch`: сессия и токен, watchdog, вычисление исхода | agents |
| `runner/runner.py` | `Runner`: запуск проекта, задача, шлюз, релизы, лимиты, отчёт, `recover()` | run-lifecycle |
| `runner/gitops.py` | git-шаги раннера: ветки, WIP, PLAN.md, тесты шлюза | git-workflow |
| `mcp_server/` | `app.py` (сборка трёх эндпоинтов), `auth.py` (токен → сессия), `executor.py`, `planner.py`, `system.py` (инструменты), `params.py` | mcp-tools |
| `bot/` | `app.py` (диспетчер), `notifier.py` (события → сообщения), `handlers/` (команды), `callbacks.py`, `formatting.py`, `storage.py` (FSM в SQLite), `context.py` (`AppContext`) | telegram-bot |
| `vcs/git.py`, `vcs/github.py` | асинхронный git; `GitHubProvider` (REST + авторизация push через env) | git-workflow |
| `scheduler/` | `DayforgeScheduler`: ежедневный запуск, бэкап, истечение подтверждений, чистка логов | architecture |
| `backup/` | `BackupService` | operations#бэкапы |

Вне пакета: `prompts/` (шаблоны промптов и файлов проекта), `deploy/` (systemd, обёртка агента, sudoers, скрипты деплоя), `scripts/restore.sh`, `tests/`.

## Правила слоёв

- **В БД пишет только `core`.** Бот, MCP, раннер, оркестратор вызывают сервисы. Исключение — чтение для отображения (`bot/formatting.py`, `mcp_server/system.py` читают модели напрямую).
- **Ядро не знает про Telegram.** Сообщить пользователю — `services.notifier.send(<Event>)`; как показать — решает `bot/notifier.py`.
- **`Services`** собирается в `build_services` и передаётся всем. `Orchestrator` подключается после сборки (`services.orchestrator = …`), потому что ему самому нужны сервисы; MCP и бот зовут его через `services.require_orchestrator()` / `app.orchestrator`.
- **Детерминированное — в коде.** Агент не мержит, не пушит, не меняет `PLAN.md`; это делает раннер.

## Как живёт процесс (`app.py`)

1. Конфиг, БД, `GitHubProvider` (если есть токен), бот и `TelegramNotifier`, `build_services`, `notifier.attach`.
2. `AgentLauncher` (адаптеры от `build_adapters(AGENT_UNIX_USER)`), `Runner`, `Orchestrator`, `ChatAgent`, `BackupService` (если задан restic), `DayforgeScheduler`, диспетчер бота с `SqliteStorage`.
3. Старт MCP-сервера (uvicorn в общем event loop), затем `runner.recover()` и `orchestrator.recover()` — разбор прерванного рестартом, затем раннер, планировщик, polling бота.
4. Остановка (SIGTERM/SIGINT ловит aiogram): планировщик, оркестратор, раннер (текущая задача → `paused(interrupted)` + WIP-коммит), MCP, сессии HTTP, БД.

## Сквозные потоки по коду

**Создание проекта.** `bot/handlers/new_project.create` → `ProjectService.create` (+ `CalendarService.set_weekdays`) → `Orchestrator.start_planning` → фоновая `_planner_session` → `AgentLauncher.launch(role=PLANNER)` → агент вызывает MCP `ask_user` (`mcp_server/planner.py` → `QuestionService.ask` → событие `QuestionAsked` → `TelegramNotifier._question`) → пользователь отвечает (`bot/handlers/questions.py` → `QuestionService.answer` → `Orchestrator.after_answer` → продолжение той же сессии) → агент вызывает `submit_plan` (`PlanService.submit` → `PlanProposed`) → кнопка «Утвердить» (`bot/handlers/planning.py` → `Orchestrator.approve_plan` → `PlanService.approve` → первый коммит, ветка `dev` → `RepoSync.publish`).

**Ежедневный запуск.** `DayforgeScheduler.daily_run` → `RunService.enqueue_scheduled` → `Runner.kick` → `run_forever` → `execute_run` (под `Runner.lock(project)`) → `_run_project`: `TaskService.carry_over` + `start_next_day` → для каждой задачи `_execute_task`: ветка → промпт (`_task_prompt`: новая сессия или продолжение) → `AgentLauncher.launch(EXECUTOR)` → агент вызывает `complete_task` → `_gate` (ссылки, WIP, merge dev, тесты, merge в dev, учёт коммитов, PLAN.md, push, удаление ветки, релиз вехи) → отчёт (`ReportService.store` → `RunReport`).

**Вопрос исполнителя.** MCP `ask_user` → задача `paused(question)`, проект `blocked`, исход сессии `QUESTION` → раннер: WIP-коммит, push ветки, запуск `BLOCKED` → ответ → проект `active`, кнопка «Продолжить сейчас» (`bot/handlers/runs.continue_now` → `RunService.enqueue(CONTINUE)`) → `_task_prompt` находит отвеченный вопрос и продолжает ту же сессию CLI.

**Системный агент.** `bot/handlers/agent.free_text` → `ChatAgent.ask` → `AgentLauncher.launch(role=SYSTEM, read_only=True, cwd=projects/)` → инструменты `mcp_server/system.py`; необратимое → `PendingActionService.create` → кнопка → `bot/handlers/actions.decide` → обработчик из `actions.register_action_handlers`.

## Данные и миграции

- Модели — `db/models.py`; время — `UTCDateTime` (наивное время отклоняется), статусы — `str_enum` (строки без CHECK: новые значения не требуют миграции).
- Изменил модели → `uv run alembic revision --autogenerate -m "описание"` (URL БД берётся из `BASE_DIR`; для генерации удобно `BASE_DIR=<временная папка>` после `alembic upgrade head`) → `make fmt` → `make check`. Тест `test_migrations_match_models` сравнивает модели и миграции.
- Таблица `bot_kv` — служебное хранилище бота (FSM, частичные ответы, выбор в `/next`, предложения агента).

## Тесты

`make test` — около 210 тестов за ~15 с, без сети и без настоящих CLI.

| Инструмент | Что даёт |
|---|---|
| `tests/conftest.py` | `session_factory` (SQLite во временной папке), `active_project` (проект с рабочей директорией и задачей T1 в работе) |
| `tests/scripted_agent.py` | `ScriptedAdapter`: «агент» внутри процесса по сценарию — правит файлы, коммитит, вызывает сервисы так же, как MCP-инструменты (`complete`, `ask`, `commit_file`) |
| `tests/fake_cli/fake_agent.py` | поддельный CLI: печатает заданные строки, записывает argv и окружение — для тестов адаптеров |
| `tests/fake_cli/fake_claude_mcp.py` | поддельный `claude`, который по-настоящему ходит в MCP по HTTP — сквозной тест |
| `tests/fake_cli/fake_restic.py` | поддельный restic |
| `tests/mcp_helpers.py` | `running_server` (MCP на свободном порту), `client`, `call` (разбор ответа инструмента), `FakeOrchestrator` |
| `tests/bot_helpers.py` | `FakeSession` (Telegram API без сети: запоминает вызовы), `message`, `callback` — апдейты для `dispatcher.feed_update` |

Где писать тест на изменение: сервис ядра — рядом с `test_<сервис>.py`; раннер — `test_runner.py` со `ScriptedAdapter`; инструмент MCP — `test_mcp_tools.py` через настоящий HTTP; команда бота — `test_bot_handlers.py` (фикстура `harness`); флаги CLI — `test_agents.py` с `fake_agent.py`.

Живые проверки с настоящими CLI в автотесты не входят (тратят подписку). Скрипт для ручной проверки — вызов `list_projects` через MCP и продолжение сессии — удобно держать в рабочей папке; после обновления CLI прогоняй его для обоих агентов.

## Рецепты

**Новый MCP-инструмент.** Функция с `@server.tool()` в `mcp_server/executor.py | planner.py | system.py`; параметры с типами (из них строится JSON-схема — агент видит её как документацию), docstring — описание для агента. Сессию берёт `current_session(ctx, services.sessions)`; ошибка для агента — `raise tool_error("код", "объяснение")`. Завершающий вызов — сначала действие, потом `sessions.record_outcome`. Тест — `test_mcp_tools.py`. Обнови `docs/mcp-tools.md` и, если инструмент меняет поведение агента, промпт роли в `prompts/`.

**Новая команда или кнопка бота.** Обработчик в подходящем `bot/handlers/*.py` (зависимости — аргумент `app: AppContext`); данные кнопки — класс в `bot/callbacks.py` (лимит callback_data — 64 байта). Шаги диалогов с состоянием фильтруй `~F.text.startswith("/")`, чтобы команды не попадали в ответ. Новый роутер — в `bot/handlers/__init__.py: ROUTERS` (порядок важен: агент — последним). Команда в меню — `bot/app.py: COMMANDS`. Тексты — HTML: всё пользовательское и агентское экранируй `formatting.h()`. Обнови `docs/telegram-bot.md`.

**Новое событие для пользователя.** Dataclass в `core/events.py` (наследник `Event`) → обработчик в `TelegramNotifier._handlers` → отправка из ядра или раннера через `notifier.send`. Ошибки доставки не ломают вызывающего — notifier их глотает и логирует.

**Новая настройка.** Ключ и значение по умолчанию в `core/settings.py: DEFAULTS`, описание в `docs/data-model.md#settings`, подпись и подсказка в `bot/handlers/settings.py: LABELS/HINTS`, разбор — `parse_setting`. Если влияет на расписание — добавь ключ в `SCHEDULE_KEYS`.

**Новый шаблон промпта.** Файл в `prompts/` (Jinja2, `StrictUndefined`, `trim_blocks`): забытая переменная — ошибка, а не пустая строка. Рендер — `prompts.render("имя.md", **переменные)`. Добавь случай в `tests/test_prompts.py`.

**Новый агент (CLI).** Адаптер по образцу `agents/codex.py` (команда, `parse_line`, `detect_limit`), регистрация в `agents/__init__.py: build_adapters`, имя — в `core/projects.py: AGENTS` и `bot/handlers/settings.py: parse_agent`, разрешение в `deploy/dayforge-agent-exec`. Тест на поддельном CLI, затем живой прогон.

**Новый git-хостинг.** Реализация `vcs/base.py: VcsProvider` (создание и удаление репозитория, URL, релиз, `git_env` для push) и выбор провайдера в `app.py`.

## Грабли, проверенные на практике

**Claude Code 2.1**
- Reasoning — флаг `--effort` (`low`…`max`), не переменная окружения.
- `--mcp-config` подставляет `${ПЕРЕМЕННАЯ}` из окружения в заголовки — так токен не попадает в argv (`ps` видят все).
- Лимит подписки — событие `rate_limit_event` (`rate_limit_info.status`, `resetsAt`).
- Шаблоны `--allowedTools` для Bash совпадают только с началом команды: `Bash(git log:*)` не пропускает `git -C <проект> log`, шаблон со `*` в середине не работает, а `cd <проект> && git …` CLI блокирует сам (хуки чужого репозитория). Поэтому системный агент работает с git через MCP (`get_repo_status`, `get_git_log`, `push_project`), а не через оболочку. В режиме `-p` отклонённая команда отклоняется окончательно — подтвердить её пользователю нечем.
- Режим без подтверждений CLI не разрешает запускать от root (известное ограничение, вживую не проверялось) — ещё одна причина запускать агентов от `dayforge-agent`.

**Codex (codex-cli 0.160–0.162)**
- `codex exec resume` не принимает `--cd` и `--sandbox`: рабочая директория — cwd процесса, песочница — `-c sandbox_mode=…`, id сессии — после опций.
- Без `-c mcp_servers.dayforge.default_tools_approval_mode="approve"` в режиме только чтения каждый вызов MCP отклоняется («requires approval, but approval policy is never»).
- Отдельного системного промпта нет — роль идёт в начале сообщения.

**MCP SDK 2.x** — класс `MCPServer` (не `FastMCP`); три сервера собраны в одно Starlette-приложение из маршрутов `streamable_http_app`; текст `ToolError` приходит агенту с префиксом `Error executing tool …:`; тестовый клиент — `create_mcp_http_client` (httpx2).

**aiogram 3** — роутер подключается к одному диспетчеру (в тестах фикстура отсоединяет роутеры); `ParseMode.HTML` по умолчанию; FSM-состояния — в `bot_kv`, переживают рестарт.

**Окружение сервера**
- `sudo` сбрасывает `PATH` до `secure_path` — обёртка `dayforge-agent-exec` добавляет `~/.local/bin`; переменные проходят только из `env_keep` (`DAYFORGE_MCP_TOKEN`).
- Рабочие копии пишут два пользователя одной группы: `safe.directory = *` у обоих, `core.sharedRepository = group`, `.env` проекта — 660, каталог `projects/` — setgid.
- Рабочая копия проекта всегда на `dev` или ветке задачи; в `main` — только релизы (`gitops.ensure_on_dev` уводит изменения из `main` в `dev`).
- `pydantic-settings` с `env_ignore_empty`: пустое значение в окружении не перекрывает `.env` — локальные значения меняются в самом `.env`.
- Тулчейны под стеки проектов (Go, Node, …) на сервере ставятся отдельно: агент работает без root и может только запросить их через вопрос.

## Локально и на сервере

- **Локально:** `.env` с `BASE_DIR=./var` и пустым `AGENT_UNIX_USER` (агенты от текущего пользователя), `make run`. БД — `var/data/dayforge.sqlite3`.
- **Сервер:** `root@82.25.58.76`, служба `dayforge`, код `/srv/dayforge/app`, bare-репозиторий `/srv/dayforge/dayforge.git`, конфиг `/srv/dayforge/.env`, рабочие копии `/srv/dayforge/projects`, логи сессий `/srv/dayforge/data/logs/sessions/<id>.jsonl`. Устройство — [operations.md](operations.md).
- **Синхронизация:** закоммитить → `make status` (нет процессов агентов) → `make sync` → `make logs`. Перезапуск посреди задачи безопасен (задача встанет на паузу и продолжится), но прерывает работу агента.

## Известные ограничения

- Один исполнитель за раз; планировщик и чат — параллельно ему.
- Все агенты — один unix-пользователь: исполнитель теоретически может прочитать токен параллельной чат-сессии системного агента ([operations.md](operations.md#безопасность)).
- На сервере 1,8 ГБ памяти + 2 ГБ свопа — тяжёлые сборки проектов будут медленными.
- Бэкап не настроен; восстановление (`scripts/restore.sh`) не прогонялось вживую.
