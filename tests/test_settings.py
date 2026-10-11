import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dayforge.core.settings import DEFAULTS, SettingsService


@pytest.fixture
def settings(session_factory: async_sessionmaker[AsyncSession]) -> SettingsService:
    return SettingsService(session_factory)


async def test_returns_default_when_not_set(settings: SettingsService) -> None:
    assert await settings.get("run_time") == "03:00"


async def test_stored_value_overrides_default(settings: SettingsService) -> None:
    await settings.set("run_time", "04:30")
    await settings.set("run_time", "05:00")

    assert await settings.get("run_time") == "05:00"
    assert (await settings.get_all()) == DEFAULTS | {"run_time": "05:00"}


async def test_unknown_key_is_rejected(settings: SettingsService) -> None:
    with pytest.raises(KeyError):
        await settings.get("no_such_setting")
    with pytest.raises(KeyError):
        await settings.set("no_such_setting", 1)
