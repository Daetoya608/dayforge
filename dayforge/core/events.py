"""События ядра.

Ядро не знает про Telegram: оно отдаёт события Notifier, а бот решает, как их показать.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from dayforge.db.base import utcnow


@dataclass(frozen=True, kw_only=True)
class Event:
    at: datetime = field(default_factory=utcnow)


class Notifier(Protocol):
    async def send(self, event: Event) -> None: ...
