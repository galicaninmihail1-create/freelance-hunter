from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import RawJob


class BaseCollector(ABC):
    @abstractmethod
    async def collect(self) -> list[RawJob]:
        """Return best-effort source entries; one malformed entry must not abort a poll."""
