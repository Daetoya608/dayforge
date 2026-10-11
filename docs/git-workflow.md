# Git-процесс

## Ветки

| Ветка | Кто пишет | Назначение |
|---|---|---|
| `main` | Система (релизы) | Стабильные версии, теги `vX.Y.Z` |
| `dev` | Система (мержи, `PLAN.md`) | Интеграционная ветка, всегда с зелёными тестами |
| `feature/<key>-<slug>` | Агент | Одна задача; `slug` — латиница из названия, ≤ 40 символов |

Агент **никогда** не переключает ветки, не мержит и не пушит. Это прописано в `AGENTS.md` проекта и проверяется шлюзом (ссылки `dev`/`main` до и после сессии должны совпадать). Пушить агент и не может: учётных данных GitHub в его окружении нет ([operations.md](operations.md#безопасность)).

## Инициализация проекта

При `/new` (до плана):

1. `projects/<slug>/` → `git init -b main`, `user.name = "Dayforge Agent"`, `user.email = "agent@dayforge.local"` (локальный конфиг репозитория — по автору потом отличаем коммиты агента от ручных).
2. `docs/IDEA.md` — идея, пожелания по стеку, позже — ответы на уточняющие вопросы.
3. `AGENTS.md` из шаблона, `CLAUDE.md` с единственной строкой `@AGENTS.md`, `.gitignore` (`.env`, плюс то, что добавит агент под стек).

Планировщик пишет `docs/ARCHITECTURE.md` в рабочую директорию. Ничего не коммитится до утверждения.

При утверждении плана:

1. Генерация `docs/PLAN.md`, пустой `docs/JOURNAL.md`.
2. Первый коммит в `main`: `chore: project init (plan v1)`.
3. `git branch dev`.
4. `VcsProvider.create_repo(slug, private=True)` → `remote add origin` → `push main dev`.

## Коммиты

- Формат — Conventional Commits с ключом задачи в scope: `feat(T12): команда /category`, `test(T12): …`, `fix(T12): …`.
- WIP при паузе: `wip(T12): <причина>` (агент или система).
- Системные: `chore(plan): v3 — <change_summary>`, `chore: release v0.2.0`.
- Merge в `dev`: `merge(T12): <название задачи>`.

## Шлюз перед мержем

Подробно — в [run-lifecycle.md](run-lifecycle.md#шлюз). Кратко: проверка ссылок → wip-коммит, если надо → `merge dev` в ветку → `test_command` → `merge --no-ff` в `dev` → коммиты в БД → `PLAN.md` → `push dev` → удаление ветки задачи.

Почему `--no-ff`, а не squash: сохраняются все коммиты агента (с авторством) и видно границы задач. История `dev` читается по merge-коммитам: `git log --first-parent dev`.

## Релизы

Веха выпускается, когда все её задачи в `done`:

1. `git checkout main && git merge --no-ff dev -m "chore: release v0.2.0"`.
2. Аннотированный тег `v0.2.0` с описанием вехи и списком задач.
3. `push main --tags`.
4. `VcsProvider.create_release(tag, notes)` — заметки собираются из `summary` задач вехи.
5. `releases`, `milestones.status = released`, уведомление.

Ручной релиз — `/release <проект>` или системный агент: то же самое с ближайшей минорной версией без вехи. Тесты перед релизом не перезапускаются — `dev` и так зелёный.

## Удалённые ветки

- Ветки задач пушатся при каждой паузе (резервная копия WIP) и удаляются после мержа.
- `dev` пушится после каждого мержа, `main` — при релизе.
- Если push упал (сеть, GitHub недоступен) — локальная работа продолжается, push повторяется в конце запуска и перед бэкапом; ошибка попадает в отчёт.

## `VcsProvider`

Код интерфейса — [dayforge/vcs/base.py](../dayforge/vcs/base.py).

```python
class VcsProvider(Protocol):
    name: str

    async def create_repo(self, name: str, private: bool, description: str) -> RepoInfo: ...
    def remote_url(self, repo: RepoInfo) -> str: ...  # для git remote
    def web_url(self, repo: RepoInfo) -> str: ...
    def commit_url(self, repo: RepoInfo, sha: str) -> str: ...
    def branch_url(self, repo: RepoInfo, branch: str) -> str: ...
    async def create_release(self, repo: RepoInfo, tag: str, notes: str) -> str: ...
```

Сами git-операции (commit, merge, push) выполняются локальным `git` и от провайдера не зависят. Провайдер отвечает только за API хостинга и URL.

**GitHub:** REST API через `httpx`; fine-grained personal access token с правами `Administration: write` (создание репозиториев) и `Contents: write`. Push по HTTPS: токен передаётся только командам `git push` раннера — заголовком `http.extraheader` через переменные окружения процесса git (`GIT_CONFIG_COUNT`/`KEY`/`VALUE`). В конфиг репозитория и на диск он не попадает.

Без `GITHUB_TOKEN` система работает локально: репозитории не публикуются, ссылки в отчётах не строятся.

## Будущее: ручные изменения пользователя

Не входит в MVP, но всё готово: все коммиты агента и системы есть в `commits`, автор агента фиксирован. Перед запуском достаточно сравнить `git log origin/dev` с таблицей — всё, чего там нет, сделано человеком.
