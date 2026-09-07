from app.collectors.base import BaseCollector
from app.models import RawJob


class KworkEmailCollector(BaseCollector):
    """Future extension point for user-authorized Kwork notification emails; no scraping."""
    async def collect(self) -> list[RawJob]:
        return []
