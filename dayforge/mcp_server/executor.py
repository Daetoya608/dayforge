"""Инструменты исполнителя (docs/mcp-tools.md#исполнитель--mcpexecutor)."""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from dayforge.core.enums import SessionOutcome
from dayforge.core.events import PlanChangeProposed, ProjectRef, TaskFailed
from dayforge.core.questions import QuestionError
from dayforge.mcp_server.auth import current_session, tool_error
from dayforge.mcp_server.params import Questions
from dayforge.services import Services


def register(server: MCPServer, services: Services) -> None:
    async def task_context(ctx: Context) -> tuple[Any, Any]:
        session = await current_session(ctx, services.sessions)
        if session.task_id is None or session.project_id is None:
            raise tool_error("no_task", "сессия не привязана к задаче")
        task = await services.tasks.get(session.task_id)
        return session, task

    @server.tool()
    async def ask_user(
        questions: Questions,
        ctx: Context,
        context: Annotated[
            str | None, Field(max_length=1000, description="Зачем вопрос — увидит пользователь")
        ] = None,
    ) -> dict[str, Any]:
        """Задать пользователю блокирующие вопросы. Задача встанет на паузу до ответа.

        Спрашивай только то, без чего продолжать нельзя; остальное решай сам и записывай
        решение в docs/JOURNAL.md.
        """
        session, task = await task_context(ctx)
        question_id = await services.questions.ask(
            project_id=session.project_id,
            task_id=task.id,
            session_id=session.id,
            questions=[q.model_dump() for q in questions],
            context=context,
        )
        await services.sessions.record_outcome(session.id, SessionOutcome.QUESTION)
        return {
            "status": "paused",
            "question_id": question_id,
            "instruction": (
                f"Закоммить текущую работу как wip({task.key}): ждёт ответа, запиши в "
                "docs/JOURNAL.md, на чём остановился, и заверши работу. "
                "Ответ придёт в эту же сессию."
            ),
        }

    @server.tool()
    async def request_secret(
        name: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")],
        description: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: Context,
        how_to_get: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        """Запросить секрет (API-ключ, токен). Если он уже есть в .env — сразу ответ «available»."""
        session, task = await task_context(ctx)
        try:
            question_id = await services.questions.request_secret(
                project_id=session.project_id,
                task_id=task.id,
                session_id=session.id,
                name=name,
                description=description,
                how_to_get=how_to_get,
            )
        except QuestionError as exc:
            raise tool_error("invalid_secret", str(exc)) from None
        if question_id is None:
            return {"status": "available", "env_var": name, "env_file": ".env"}
        await services.sessions.record_outcome(session.id, SessionOutcome.SECRET)
        return {
            "status": "paused",
            "question_id": question_id,
            "instruction": (
                f"Закоммить текущую работу как wip({task.key}): ждёт секрета {name} и заверши "
                "работу. Когда пользователь добавит секрет в .env, работа продолжится "
                "в этой сессии."
            ),
        }

    @server.tool()
    async def complete_task(
        summary: Annotated[str, Field(min_length=1, max_length=2000, description="Что сделано")],
        ctx: Context,
        decisions: Annotated[
            list[Annotated[str, Field(max_length=500)]],
            Field(max_length=10, description="Решения, принятые самостоятельно"),
        ] = [],  # noqa: B006 — неизменяемое значение для схемы
    ) -> dict[str, Any]:
        """Задача готова. Дальше система прогонит тесты и смержит ветку."""
        session, task = await task_context(ctx)
        await services.tasks.mark_verifying(task.id, summary=summary, decisions=decisions)
        await services.sessions.record_outcome(session.id, SessionOutcome.COMPLETED)
        return {
            "status": "accepted",
            "instruction": (
                "Убедись, что всё закоммичено, и заверши работу. "
                "Система прогонит тесты и смержит ветку."
            ),
        }

    @server.tool()
    async def fail_task(
        reason: Annotated[str, Field(min_length=1, max_length=2000)],
        ctx: Context,
        suggestion: Annotated[str | None, Field(max_length=2000)] = None,
    ) -> dict[str, Any]:
        """Задачу нельзя выполнить не из-за вопроса (например, ошибка в плане)."""
        session, task = await task_context(ctx)
        await services.tasks.fail(task.id, reason=reason)
        await services.sessions.record_outcome(session.id, SessionOutcome.FAILED)
        project = await services.projects.get_by_id(task.project_id)
        await services.notifier.send(
            TaskFailed(
                project=ProjectRef.of(project),
                task_id=task.id,
                task_key=task.key,
                reason=reason,
                suggestion=suggestion,
            )
        )
        return {
            "status": "failed",
            "instruction": (
                f"Закоммить сделанное как wip({task.key}): не выполнена и заверши работу."
            ),
        }

    @server.tool()
    async def propose_plan_change(
        reason: Annotated[str, Field(min_length=1, max_length=2000)],
        proposal: Annotated[str, Field(min_length=1, max_length=2000)],
        ctx: Context,
    ) -> dict[str, str]:
        """Предложить пользователю изменить план. Работу не блокирует."""
        _, task = await task_context(ctx)
        project = await services.projects.get_by_id(task.project_id)
        await services.notifier.send(
            PlanChangeProposed(
                project=ProjectRef.of(project), task_key=task.key, reason=reason, proposal=proposal
            )
        )
        return {"status": "sent"}
