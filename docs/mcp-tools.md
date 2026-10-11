# MCP-инструменты

## Транспорт и авторизация

- MCP-сервер работает внутри основного процесса: **Streamable HTTP** на `127.0.0.1:8765` (только localhost), официальный Python SDK (`mcp` 2.x, `MCPServer`).
- Три эндпоинта с разными наборами инструментов: `/mcp/executor`, `/mcp/planner`, `/mcp/system`. Имя сервера в конфиге агента — `dayforge`, поэтому в Claude Code инструменты видны как `mcp__dayforge__<имя>`.
- Перед стартом сессии раннер генерирует токен (32 случайных байта), хранит его SHA-256 в `agent_sessions.mcp_token_hash` и передаёт агенту в заголовке `Authorization: Bearer …`.
- По токену сервер определяет сессию → роль, проект, задачу. Токен роли `executor` на эндпоинте `/mcp/system` отклоняется. Токен отзывается, когда сессия заканчивается.
- Исполнителю и планировщику параметр `project` не нужен: он берётся из сессии.

## Соглашения

- Ответ инструмента — JSON-объект в текстовом содержимом.
- Ошибка валидации или недопустимое действие — результат с `isError: true` и `{"error": "<код>", "message": "<понятное объяснение>"}` (SDK добавляет перед JSON префикс `Error executing tool <имя>:`). Агент должен исправить вызов.
- **Завершающие** вызовы (`complete_task`, `fail_task`, `ask_user`, `request_secret` без секрета, принятый `submit_plan`) переводят сессию в режим завершения: ответ содержит `"instruction"` с тем, что сделать перед выходом, а любые следующие вызовы MCP возвращают ошибку `session_finalizing`.

---

## Исполнитель — `/mcp/executor`

### `ask_user`

Блокирующие вопросы пользователю. Задача ставится на паузу.

```json
{
  "questions": [
    {
      "text": "Какую платёжную систему подключать?",
      "options": ["Stripe", "ЮKassa"],
      "allow_free_text": true
    }
  ],
  "context": "Задача T14 требует приёма платежей, в IDEA.md провайдер не указан"
}
```

| Параметр | Тип | Описание |
|---|---|---|
| `questions` | list, 1–5 | `text` (≤ 500), `options` (≤ 6, каждый ≤ 40 — станут кнопками), `allow_free_text` (по умолчанию `true`) |
| `context` | str, ≤ 1000 | Зачем вопрос — показывается пользователю над вопросами |

Ответ:

```json
{
  "status": "paused",
  "question_id": 42,
  "instruction": "Закоммить текущую работу как wip(T14): ждёт ответа, запиши в JOURNAL.md, на чём остановился, и заверши работу. Ответ придёт в эту же сессию."
}
```

### `request_secret`

```json
{"name": "STRIPE_API_KEY", "description": "Секретный ключ Stripe (test mode) для приёма платежей", "how_to_get": "Dashboard → Developers → API keys"}
```

| Параметр | Тип | Описание |
|---|---|---|
| `name` | str | `^[A-Z][A-Z0-9_]{1,63}$` |
| `description` | str, ≤ 500 | |
| `how_to_get` | str, ≤ 500, опц. | Подсказка пользователю |

Ответ, если секрет уже есть: `{"status": "available", "env_var": "STRIPE_API_KEY", "env_file": ".env"}` — не завершающий.
Если нет — завершающий: `{"status": "paused", "question_id": 43, "instruction": "…"}`.

### `complete_task`

```json
{
  "summary": "Добавлены команды /category add|list|remove, модель Category, миграция, 12 тестов",
  "decisions": [
    "Категории храню на пользователя, а не глобально — так в IDEA.md про личный учёт",
    "Удаление категории с расходами запрещено, а не каскадное"
  ]
}
```

| Параметр | Тип | Описание |
|---|---|---|
| `summary` | str, ≤ 2000 | Что сделано — пойдёт в отчёт |
| `decisions` | list[str], ≤ 10 | Решения, принятые самостоятельно |

Ответ: `{"status": "accepted", "instruction": "Убедись, что всё закоммичено, и заверши работу. Система прогонит тесты и смержит ветку."}`

### `fail_task`

```json
{"reason": "Задача требует API, которого у сервиса нет (см. их документацию)", "suggestion": "Заменить на импорт CSV"}
```

`reason` (≤ 2000), `suggestion` (≤ 2000, опц.). Задача → `failed`, пользователь получает уведомление с кнопками «Передать планировщику» (перепланирование с `suggestion`), «Вернуть в работу», «Отменить задачу». Завершающий.

### `propose_plan_change`

```json
{"reason": "T15 и T16 сильно связаны, по отдельности получится двойная работа", "proposal": "Объединить T15 и T16 в одну задачу в дне 6"}
```

Не завершающий, работу не блокирует. Пользователь получает сообщение с кнопками «Передать планировщику» / «Отклонить». Ответ: `{"status": "sent"}`.

---

## Планировщик — `/mcp/planner`

### `ask_user`

Как у исполнителя. При создании проекта — раунд уточняющих вопросов. Пауза — проект остаётся в `clarifying`.

### `submit_plan`

Параметр — объект плана целиком ([plan-format.md](plan-format.md)).

Ответ с ошибками (не завершающий, `isError: true`):

```json
{
  "error": "invalid_plan",
  "message": "План не прошёл проверку",
  "details": [
    "days[2].tasks[0].depends_on: задача T9 находится в более позднем дне",
    "milestones: у вехи M2 нет задач"
  ]
}
```

Принят (завершающий):

```json
{
  "status": "proposed",
  "plan_version": 1,
  "warnings": ["План на 12 дней, заказано 10 — пользователь увидит предупреждение"],
  "instruction": "План отправлен пользователю. Заверши работу; правки, если будут, придут в эту же сессию."
}
```

---

## Системный агент — `/mcp/system`

Системный агент не ограничен одним проектом: там, где нужен проект, передаётся `project` (slug).

### Чтение

| Инструмент | Параметры | Возвращает |
|---|---|---|
| `list_projects` | `include_archived=false` | slug, название, статус, приоритет, день `current_day/planned_days`, прогноз окончания, открытые вопросы |
| `get_project` | `project` | Настройки, текущий план (структура), задачи со статусами, последние 10 коммитов, вехи, путь к рабочей директории |
| `get_calendar` | `date_from`, `date_to` (даты, ≤ 93 дней; по умолчанию — 4 недели с сегодня) | По датам: какие проекты, использован ли слот, общие выходные; недельные шаблоны |
| `get_runs` | `project?`, `limit=20` | Запуски: режим, статус, день, время, задачи, ошибки |
| `get_run_log` | `run_id`, `tail=200` | Хвост логов сессий запуска (события, ошибки) |
| `get_pending_questions` | — | Открытые вопросы: id, проект, задача, текст, варианты |

### Изменения

| Инструмент | Параметры | Подтверждение |
|---|---|:-:|
| `create_project` | `idea`, `days`, `name?`, `slug?`, `priority?`, `stack?`, `planner_agent?`, `executor_agent?`, `weekdays?` | |
| `request_replan` | `project`, `instructions` | результат — diff плана |
| `add_task` | `project`, `title`, `description`, `acceptance_criteria?`, `when: now\|next` | |
| `update_calendar` | `changes[]` (см. ниже) | при массовых изменениях |
| `set_priority` | `order: [slug, …]` | |
| `update_project_settings` | `project`, `settings` (`name`, `planner_agent`, `executor_agent`, `badge`) | |
| `pause_project` / `resume_project` | `project` | |
| `answer_question` | `question_id`, `answers: [str]` | |
| `start_run` | `projects: [slug]`, `mode: continue\|next` | |
| `release` | `project` | ✓ |
| `archive_project` | `project` | ✓ |
| `delete_project` | `project`, `delete_remote=false` | ✓ |

Число дней проекта (`planned_days`) напрямую не меняется — только через `request_replan` (план должен соответствовать числу дней).

**`update_calendar.changes[]`** — список операций:

```json
[
  {"op": "set_weekdays", "project": "todo-bot", "weekdays": [0, 2, 4]},
  {"op": "include", "project": "todo-bot", "dates": ["2026-11-07"]},
  {"op": "exclude", "project": "todo-bot", "dates": ["2026-11-09"]},
  {"op": "day_off", "dates": ["2026-12-31", "2027-01-01"]},
  {"op": "clear", "project": "todo-bot", "dates": ["2026-11-07"]}
]
```

Подтверждение нужно, если затронуто больше 5 дат или меняется недельный шаблон.

**Ответы:**

- Выполнено сразу: `{"status": "done", …результат}`.
- Нужно подтверждение: `{"status": "pending_confirmation", "action_id": 17, "message": "Пользователю отправлен запрос на подтверждение"}`. Агенту следует сказать пользователю, что нужно нажать кнопку.
- Асинхронно (`request_replan`, `start_run`, `create_project`): `{"status": "started", …}` — результат придёт пользователю отдельным сообщением.

---

## Сводная таблица

| Инструмент | Исполнитель | Планировщик | Системный |
|---|:-:|:-:|:-:|
| `ask_user` | ✓ | ✓ | |
| `request_secret` | ✓ | | |
| `complete_task`, `fail_task` | ✓ | | |
| `propose_plan_change` | ✓ | | |
| `submit_plan` | | ✓ | |
| чтение (`list_projects`, `get_*`) | | | ✓ |
| изменения (`create_project` … `delete_project`) | | | ✓ |

Системному агенту `ask_user` не нужен: он в диалоге с пользователем и спрашивает обычным ответом.
