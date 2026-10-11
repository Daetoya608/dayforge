# Эксплуатация

## Требования

- VPS с Ubuntu 24.04 LTS (или аналог).
- Минимум 2 vCPU, 4 ГБ RAM, 40 ГБ диска. Агенты сами по себе лёгкие, но собирают и тестируют проекты: для тяжёлых стеков нужно больше.
- Python 3.12+, [uv](https://github.com/astral-sh/uv), git, sqlite3, [restic](https://restic.net/).
- Claude Code CLI и/или Codex CLI (+ Node.js, если требуется способом установки).
- Инструменты под стеки создаваемых проектов (Node.js, Docker, компиляторы — по мере надобности; агент запросит недостающее через вопрос).

## Пользователи

| Пользователь | Что от него работает | Что ему доступно |
|---|---|---|
| `dayforge` | Процесс системы: бот, раннер, MCP, git-операции раннера | `/srv/dayforge` целиком, `.env` системы |
| `dayforge-agent` | CLI агентов | Только `projects/` и свой `$HOME` (логин в Claude/Codex) |

- Общая группа `dayforge-work` (участники `dayforge` и `dayforge-agent`) — **только** для `projects/`: `dayforge-agent:dayforge-work`, права `2770` (setgid). Оба пользователя работают с umask `002`, поэтому файлы рабочих копий доступны обоим.
- `dayforge-agent` **не** входит в группу `dayforge`. `/srv/dayforge` — `0711` (можно пройти к `projects/`, нельзя прочитать список), `data/` — `0700`, `.env` — `0600`.
- `dayforge` запускает агентов через обёртку `/usr/local/bin/dayforge-agent-exec` (из `deploy/`): она выставляет umask `002` и запускает `claude`, `codex` или — в режиме `test` — команду тестов шлюза. Тесты выполняют код проекта, поэтому тоже идут от `dayforge-agent`, а не от пользователя системы. Правило sudoers разрешает только её и пробрасывает переменную с токеном MCP:

  ```
  Defaults:dayforge env_keep += "DAYFORGE_MCP_TOKEN"
  dayforge ALL=(dayforge-agent) NOPASSWD: /usr/local/bin/dayforge-agent-exec
  ```

- Для `dayforge` в git: `safe.directory = /srv/dayforge/projects/*`.

## Установка

```bash
# пользователи и директории
sudo groupadd dayforge-work
sudo useradd -r -m -d /srv/dayforge -s /bin/bash -G dayforge-work dayforge
sudo useradd -r -m -s /bin/bash -G dayforge-work dayforge-agent
sudo chmod 0711 /srv/dayforge
sudo -u dayforge mkdir -p -m 0700 /srv/dayforge/data
sudo -u dayforge mkdir -p /srv/dayforge/data/{logs/sessions,backup}
sudo mkdir -p /srv/dayforge/projects
sudo chown dayforge-agent:dayforge-work /srv/dayforge/projects && sudo chmod 2770 /srv/dayforge/projects

# код
sudo -u dayforge git clone <repo> /srv/dayforge/app
cd /srv/dayforge/app && sudo -u dayforge uv sync --frozen
sudo install -m 0755 deploy/dayforge-agent-exec /usr/local/bin/dayforge-agent-exec
sudo install -m 0440 deploy/sudoers /etc/sudoers.d/dayforge

# логин агентов (один раз, интерактивно)
sudo -iu dayforge-agent claude        # затем /login — подписка
sudo -iu dayforge-agent codex login   # device code

# конфиг и БД
sudo -u dayforge cp .env.example /srv/dayforge/.env && sudo chmod 600 /srv/dayforge/.env
sudo -u dayforge uv run alembic upgrade head

# сервис
sudo cp deploy/dayforge.service /etc/systemd/system/
sudo systemctl enable --now dayforge
```

## Конфигурация

`/srv/dayforge/.env`:

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `OWNER_TELEGRAM_ID` | Числовой id владельца |
| `GITHUB_TOKEN` | Fine-grained PAT ([git-workflow.md](git-workflow.md#vcsprovider)) |
| `GITHUB_OWNER` | Пользователь или организация для новых репозиториев |
| `BASE_DIR` | `/srv/dayforge` |
| `AGENT_UNIX_USER` | `dayforge-agent` |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8765` |
| `RESTIC_REPOSITORY` | Например `s3:https://<endpoint>/<bucket>/dayforge` |
| `RESTIC_PASSWORD_FILE` | Файл с паролем шифрования (600) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Ключи S3-совместимого хранилища |

Остальное — в таблице `settings` через `/settings` ([data-model.md](data-model.md#settings)).

## systemd

```ini
[Unit]
Description=Dayforge
After=network-online.target
Wants=network-online.target

[Service]
User=dayforge
Group=dayforge
WorkingDirectory=/srv/dayforge/app
EnvironmentFile=/srv/dayforge/.env
UMask=0002
ExecStart=/srv/dayforge/app/.venv/bin/python -m dayforge
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

При остановке (`SIGTERM`) процесс: перестаёт брать элементы очереди, завершает сессии агентов (SIGTERM группам), делает wip-коммиты, помечает задачи `paused(interrupted)` и выходит. После старта — восстановление ([architecture.md](architecture.md#восстановление-после-рестарта)).

## Логи

- Процесс системы — journald (`journalctl -u dayforge`). Уровень — `LOG_LEVEL` в `.env`.
- Сессии агентов — `data/logs/sessions/<session_id>.jsonl`. Хранятся 30 дней (чистка — ежедневным заданием).
- Секреты в логах маскируются: значения из `.env` проектов заменяются на `***` при записи логов сессий.

## Обновление системы

```bash
cd /srv/dayforge/app
sudo -u dayforge git pull
sudo -u dayforge uv sync --frozen
sudo -u dayforge uv run alembic upgrade head
sudo systemctl restart dayforge
```

Лучше обновлять, когда нет запуска (`/status` или `/projects`). Если запуск идёт — прерванные задачи продолжатся через уведомление после рестарта.

Обновление CLI агентов — `sudo -iu dayforge-agent claude update` / по инструкции Codex. После обновления CLI — прогнать интеграционные тесты адаптеров (`uv run pytest -m adapters`).

## Бэкапы

**Что и куда:**

| Данные | Как сохраняется |
|---|---|
| Код проектов | GitHub: `dev`, `main`, теги, ветки задач с WIP |
| БД | Ежедневный снапшот → restic |
| `.env` проектов, `.env` системы | Ежедневно → restic |
| Сессии агентов, логи | Не бэкапятся |

**Ежедневное задание** (`backup_time`; если идёт запуск — после его окончания):

1. Повторный push всех проектов (на случай упавших push).
2. Снапшот БД через SQLite Online Backup API (`sqlite3.Connection.backup` — тот же механизм, что `.backup` в CLI) — консистентный на работающей базе.
3. Копии `.env` всех проектов в `data/backup/env/<slug>.env`.
4. `restic backup data/backup /srv/dayforge/.env --tag daily`.
5. `restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune`.
6. По воскресеньям — `restic check`.
7. Очистка `data/backup/`. При ошибке на любом шаге — уведомление в Telegram.

restic шифрует данные паролем из `RESTIC_PASSWORD_FILE`. **Пароль хранить отдельно от сервера** (менеджер паролей) — без него бэкап не восстановить.

## Восстановление

На новом сервере:

1. Установка по разделу выше, **без** `alembic upgrade` и запуска сервиса.
2. Восстановить последний снапшот:
   ```bash
   restic restore latest --target /tmp/restore
   ```
3. `dayforge.sqlite3` → `/srv/dayforge/data/`, системный `.env` → `/srv/dayforge/.env`.
4. Клонировать проекты с GitHub в `projects/<slug>/` (список — `sqlite3 data/dayforge.sqlite3 "select slug, remote_url from projects where status != 'archived'"`), выставить локальный git-конфиг агента ([git-workflow.md](git-workflow.md#инициализация-проекта)), вернуть `.env` проектов из `env/<slug>.env`.
5. `uv run alembic upgrade head` (если версия кода новее), запустить сервис.

Задачи, которые шли в момент последнего бэкапа, будут помечены прерванными и продолжатся с новой сессии по WIP-коммиту и журналу. Скрипт `scripts/restore.sh` автоматизирует шаги 3–4.

## Безопасность

- Бот отвечает только владельцу; входящих портов нет (long polling); MCP слушает только `127.0.0.1` и требует токен сессии.
- Агенты работают от `dayforge-agent`: нет доступа к `.env` системы, токену GitHub, БД. Push делает только раннер от `dayforge`, токен передаётся только командам push — заголовком `http.extraheader` через переменные окружения процесса git (`GIT_CONFIG_*`), на диск не пишется.
- Токен MCP у каждой сессии свой и даёт только инструменты её роли; системные инструменты есть только у сессий системного агента.
- Токен MCP передаётся агенту только переменной окружения `DAYFORGE_MCP_TOKEN` (конфиг MCP ссылается на неё), не аргументом командной строки: аргументы видны всем через `ps`. Окружение процесса читает только его пользователь — но все агенты работают от одного `dayforge-agent`, поэтому исполнитель теоретически может прочитать токен параллельной чат-сессии системного агента. Риск ограничен: системный агент только читает файлы, а необратимые действия требуют подтверждения в Telegram.
- Секреты проектов: `.env` (660 — система пишет, агент читает через группу `dayforge-work`; в `.gitignore`), сообщения со значениями удаляются из чата, значения маскируются в логах.
- Агенты внутри рабочей директории всемогущи (могут ставить пакеты, запускать код из интернета). Изоляция в Docker — в планах после MVP; до тех пор на сервере не должно быть ничего, кроме этой системы.
