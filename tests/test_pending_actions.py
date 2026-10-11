from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.enums import PendingActionStatus
from dayforge.core.events import PendingActionCreated, RecordingNotifier
from dayforge.core.pending_actions import PendingActionError, PendingActionService
from dayforge.db.base import utcnow


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def actions(
    session_factory: async_sessionmaker[AsyncSession], notifier: RecordingNotifier
) -> PendingActionService:
    service = PendingActionService(session_factory, notifier, ttl=timedelta(hours=24))
    executed: list[dict[str, Any]] = []

    async def delete_project(payload: dict[str, Any]) -> str:
        executed.append(payload)
        return f"проект {payload['project']} удалён"

    service.register("delete_project", delete_project)
    service.executed = executed  # type: ignore[attr-defined]
    return service


async def test_confirm_runs_handler(
    actions: PendingActionService, notifier: RecordingNotifier
) -> None:
    action_id = await actions.create(
        "delete_project", {"project": "old"}, description="Удалить old"
    )
    (event,) = notifier.of_type(PendingActionCreated)
    assert event.description == "Удалить old"

    decision = await actions.decide(action_id, confirm=True)

    assert decision.status is PendingActionStatus.CONFIRMED
    assert decision.result == "проект old удалён"
    assert actions.executed == [{"project": "old", "description": "Удалить old"}]  # type: ignore[attr-defined]
    with pytest.raises(PendingActionError, match="уже"):
        await actions.decide(action_id, confirm=True)


async def test_reject_does_not_run_handler(actions: PendingActionService) -> None:
    action_id = await actions.create("delete_project", {"project": "old"}, description="x")

    decision = await actions.decide(action_id, confirm=False)

    assert decision.status is PendingActionStatus.REJECTED
    assert actions.executed == []  # type: ignore[attr-defined]


async def test_unknown_kind_is_rejected(actions: PendingActionService) -> None:
    with pytest.raises(PendingActionError):
        await actions.create("format_disk", {}, description="x")


async def test_expiry(actions: PendingActionService) -> None:
    action_id = await actions.create("delete_project", {"project": "old"}, description="x")

    expired = await actions.expire_due(utcnow() + timedelta(hours=25))

    assert [a.id for a in expired] == [action_id]
    with pytest.raises(PendingActionError):
        await actions.decide(action_id, confirm=True)
    assert actions.executed == []  # type: ignore[attr-defined]
