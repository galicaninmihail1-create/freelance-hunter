from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Job


class JobEnricher(ABC):
    """Optional source-specific metadata enrichment after an explicit permission review."""

    @abstractmethod
    async def enrich(self, job: Job) -> Job:
        """Return an enriched job without inventing unavailable source data."""
