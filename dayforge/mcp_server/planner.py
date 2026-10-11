"""Инструменты планировщика (docs/mcp-tools.md#планировщик--mcpplanner)."""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from dayforge.core.enums import SessionOutcome
from dayforge.core.events import PlanProposed, ProjectRef
from dayforge.core.plan_format import PlanDay, PlanMilestone, PlanValidationError
from dayforge.core.plans import PlanError
from dayforge.mcp_server.auth import current_session, tool_error
from dayforge.mcp_server.params import Questions
from dayforge.services import Services


def register(server: MCPServer, services: Services) -> None:
    async def project_session(ctx: Context) -> Any:
        session = await current_session(ctx, services.sessions)
        if session.project_id is None:
            raise tool_error("no_project", "сессия не привязана к проекту")
        return session

    @server.tool()
    async def ask_user(
        questions: Questions,
        ctx: Context,
        context: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> dict[str, Any]:
        """Задать пользователю вопросы — в том числе раунд уточняющих вопросов по идее."""
        session = await project_session(ctx)
        question_id = await services.questions.ask(
            project_id=session.project_id,
            task_id=None,
            session_id=session.id,
            questions=[q.model_dump() for q in questions],
            context=context,
        )
        await services.sessions.record_outcome(session.id, SessionOutcome.QUESTION)
        return {
            "status": "paused",
            "question_id": question_id,
            "instruction": "Заверши работу: ответы придут в эту же сессию.",
        }

    @server.tool()
    async def submit_plan(
        summary: Annotated[str, Field(description="1–3 предложения: что строим и на чём")],
        estimate_comment: Annotated[
            str, Field(description="Обоснование числа дней, риски; если не укладываешься — почему")
        ],
        test_command: Annotated[str, Field(description="Команда тестов; код возврата 0 — успех")],
        milestones: list[PlanMilestone],
        days: list[PlanDay],
        ctx: Context,
        change_summary: Annotated[
            str | None, Field(description="Обязательно при перепланировании: что и почему")
        ] = None,
    ) -> dict[str, Any]:
        """Отправить план на согласование (формат — docs/plan-format.md в Dayforge).

        Ошибки проверки возвращаются списком — исправь и отправь снова.
        """
        session = await project_session(ctx)
        raw = {
            "summary": summary,
            "change_summary": change_summary,
            "estimate_comment": estimate_comment,
            "test_command": test_command,
            "milestones": [m.model_dump() for m in milestones],
            "days": [d.model_dump() for d in days],
        }
        try:
            result = await services.plans.submit(session.project_id, raw)
        except PlanValidationError as exc:
            raise tool_error(
                "invalid_plan", "План не прошёл проверку", details=exc.details
            ) from None
        except PlanError as exc:
            raise tool_error("plan_rejected", str(exc)) from None

        await services.sessions.record_outcome(session.id, SessionOutcome.PLAN_SUBMITTED)
        project = await services.projects.get_by_id(session.project_id)
        await services.notifier.send(
            PlanProposed(
                project=ProjectRef.of(project),
                plan_id=result.plan_id,
                version=result.version,
                summary=summary,
                change_summary=change_summary,
                days=days[-1].day,
                milestones=[(m.key, m.title, m.version) for m in milestones],
                warnings=result.warnings,
                diff=result.diff,
                forecast_end=(
                    result.forecast.end_date.isoformat() if result.forecast.end_date else None
                ),
                missing_days=result.forecast.missing_days,
            )
        )
        return {
            "status": "proposed",
            "plan_version": result.version,
            "warnings": result.warnings,
            "instruction": (
                "План отправлен пользователю. Заверши работу; правки, если будут, "
                "придут в эту же сессию."
            ),
        }
