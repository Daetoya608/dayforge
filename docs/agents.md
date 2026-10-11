# Агенты

## Роли

| Роль | Когда | Рабочая директория | Доступ к файлам | MCP |
|---|---|---|---|---|
| Планировщик | Создание проекта, перепланирование | `projects/<slug>/` | Полный | `/mcp/planner` |
| Исполнитель | Одна задача | `projects/<slug>/`, ветка задачи | Полный | `/mcp/executor` |
| Системный агент | `/agent` и свободный текст в боте | `projects/` | Только чтение | `/mcp/system` |

Агент и модель: у планировщика и исполнителя — из настроек проекта (`planner_agent`, `executor_agent`), у системного агента — глобальная настройка `system_agent`.

## `AgentAdapter`

Код интерфейса — [dayforge/agents/base.py](../dayforge/agents/base.py).

```python
@dataclass
class SessionRequest:
    prompt: str  # пользовательская часть (задача, ответы)
    system_prompt: str  # роль (добавляется к системному промпту CLI)
    cwd: Path
    model: str
    effort: str | None
    mcp_url: str  # http://127.0.0.1:8765/mcp/<role>
    mcp_token: str
    resume_id: str | None = None  # cli_session_id для продолжения
    read_only: bool = False
    log_path: Path


@dataclass
class AgentEvent:
    ts: datetime
    kind: Literal["init", "text", "tool_call", "tool_result", "usage", "error", "result"]
    data: dict


@dataclass
class SessionResult:
    exit_code: int
    cli_session_id: str | None
    error_kind: Literal["none", "limit", "auth", "crash"]
    limit_resets_at: datetime | None
    usage: dict
    final_text: str | None  # последний ответ агента (нужен системному агенту)


class AgentAdapter(Protocol):
    name: str

    async def start(self, req: SessionRequest) -> "AgentProcess": ...


class AgentProcess(Protocol):
    def events(self) -> AsyncIterator[AgentEvent]: ...
    async def wait(self) -> SessionResult: ...
    async def terminate(self) -> None: ...  # SIGTERM группе, затем SIGKILL
```

Адаптер запускает CLI от пользователя `dayforge-agent` (`sudo -n -u dayforge-agent /usr/local/bin/dayforge-agent-exec claude …`, см. [operations.md](operations.md#пользователи)), в своей группе процессов, пишет каждое событие в `log_path` (JSONL) и отдаёт раннеру.

## Claude Code

Запуск в headless-режиме:

```bash
claude -p "<prompt>" \
  --output-format stream-json --verbose \
  --model <model> \
  --append-system-prompt "<system_prompt>" \
  --mcp-config <путь к JSON> --strict-mcp-config \
  --permission-mode bypassPermissions \
  [--resume <cli_session_id>]
```

- **MCP-конфиг** — временный файл с сервером типа `http`, URL роли и заголовком `Authorization: Bearer ${DAYFORGE_MCP_TOKEN}`: сам токен приходит только в переменной окружения процесса. Файл удаляется после сессии. *Уточнить при реализации*, что подстановка переменных работает и для `--mcp-config`.
- **`cli_session_id`** берётся из первого события `system/init`.
- **Только чтение** (системный агент): вместо `bypassPermissions` — `--allowedTools` со списком `Read`, `Grep`, `Glob`, `Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `mcp__dayforge__*` и `--disallowedTools Edit Write`.
- **Уровень reasoning** — через документированную переменную `MAX_THINKING_TOKENS` (бюджет размышлений): `low` — 4000, `medium` — 16000, `high` — 32000, `max` — 64000.
- **Лимит подписки** распознаётся по событию результата с ошибкой лимита; время сброса — из текста ошибки. *Уточнить при реализации* формат.
- Авторизация: один раз `claude` → `/login` под `dayforge-agent` на сервере (подписка).

## Codex

```bash
codex exec [resume <cli_session_id>] --json \
  --model <model> \
  -c 'mcp_servers.dayforge.url="http://127.0.0.1:8765/mcp/<role>"' \
  -c 'mcp_servers.dayforge.bearer_token_env_var="DAYFORGE_MCP_TOKEN"' \
  --cd <рабочая директория> --skip-git-repo-check \
  -c 'model_reasoning_effort="<effort>"' \
  --dangerously-bypass-approvals-and-sandbox \
  "<prompt>"
```

- Системный промпт роли передаётся в начале `prompt` (отделён `---`): отдельного флага у `codex exec` нет.
- Только чтение: `--sandbox read-only` вместо обхода песочницы.
- Если HTTP-транспорт MCP недоступен в установленной версии — запасной вариант: stdio-прокси `dayforge-mcp-proxy`, который пробрасывает stdio в HTTP-эндпоинт с токеном.
- Авторизация: `codex login` (device code) под `dayforge-agent`.

> Флаги CLI меняются от версии к версии. Адаптеры — единственное место, где они упоминаются; при обновлении CLI проверять только их. В репозитории — интеграционный тест адаптера на простом промпте.

## Сессии

### Планировщик

```
создание проекта:
  сессия 1: промпт planner_clarify (идея, пожелания по стеку, число дней)
     → ask_user([вопросы]) → пауза
     (если вопросов нет — сразу к плану)
  resume: ответы → промпт «напиши ARCHITECTURE.md и вызови submit_plan»
     → submit_plan → ошибки валидации возвращаются в результате вызова → исправить
     → принят → «план отправлен на согласование, заверши сессию»
  правки пользователя → resume с текстом правок → submit_plan …
  утверждение → сессия закрыта

перепланирование (/replan, request_replan, propose_plan_change → «передать»):
  новая сессия: промпт planner_replan (инструкции, текущий прогресс)
     → submit_plan (с change_summary) → согласование → как выше
```

Ответы на уточняющие вопросы система дописывает в `docs/IDEA.md` — чтобы исполнители их видели.

### Исполнитель

Одна сессия на задачу (продолжения после паузы — в той же сессии). Варианты промпта:

| Промпт | Когда | Что передаётся |
|---|---|---|
| `executor_task` | Первая попытка | Задача целиком (ключ, описание, критерии, зависимости), ветка, `test_command` |
| `executor_resume` | После ответа или секрета | Ответы / «секрет `NAME` добавлен в `.env`» |
| `executor_continue` | После лимита, watchdog, рестарта | «Сессия была прервана: <причина>. Проверь состояние и продолжай» |
| `executor_fix_tests` | Шлюз: тесты красные | Хвост вывода тестов |
| `executor_merge_conflict` | Шлюз: конфликт с `dev` | Список файлов с конфликтами |
| `executor_need_result` | Сессия закончилась без завершающего вызова | «Вызови complete_task, ask_user, request_secret или fail_task» |

Если `resume` невозможен (CLI не нашёл сессию), вместо него — новая сессия с `executor_task` и дополнением «задача уже начиналась, см. ветку и `JOURNAL.md`».

### Системный агент

- Каждое сообщение пользователя (`/agent <текст>` или свободный текст вне диалогов) — новый вызов CLI с `--resume` текущей чат-сессии.
- Новая чат-сессия — после `agent_chat_idle_hours` без сообщений или по `/agent_reset`.
- Ответ пользователю — `final_text` сессии. Пока сессия идёт, бот показывает «печатает…».
- Сессия системного агента не берёт блокировок проектов; одновременно может идти только одна чат-сессия (следующее сообщение ждёт окончания предыдущего).

## Промпты

Шаблоны — `prompts/*.md` (Jinja2). Системная часть роли:

**`planner_system.md`** — ты архитектор; пиши `docs/ARCHITECTURE.md` (стек, модули, ключевые решения, как запускать тесты); план — только через `submit_plan`; раскладывай задачи так, чтобы каждый день заканчивался работающим состоянием с зелёными тестами; дни планируй с запасом; задача — атомарная фича на одну сессию; если в N дней не укладывается — скажи в `estimate_comment` и предложи своё число.

**`executor_system.md`** — ты разработчик; работай только в текущей ветке; не трогай `docs/PLAN.md`; пиши тесты; перед завершением прогони `test_command`; коммить по Conventional Commits с ключом задачи; веди `docs/JOURNAL.md` (что сделано, принятые решения); вопросы, которые можешь решить сам, решай сам и записывай решение; спрашивай (`ask_user`) только то, без чего продолжать нельзя; закончи вызовом `complete_task`, `ask_user`, `request_secret` или `fail_task`.

**`system_agent_system.md`** — ты менеджер системы; отвечай кратко; читай код проектов, но не меняй — изменения кода только через `add_task`; состояние системы меняй только инструментами; перед действием, которое трудно откатить, объясни, что произойдёт — система всё равно попросит подтверждение.

**`AGENTS.md` проекта** (шаблон `prompts/project_agents.md`) — правила, общие для всех сессий в репозитории: структура `docs/`, запреты (ветки, push, `PLAN.md`), формат коммитов и журнала, где лежат секреты (`.env`, не коммитить). Агент может дополнять его разделом «Соглашения проекта».
