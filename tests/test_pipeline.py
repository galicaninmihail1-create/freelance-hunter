from app.ai import MockAIProvider
from app.filters import HardFilter
from app.models import RawJob
from app.scoring import ScoringEngine
from app.services import JobPipeline
from app.telegram import TelegramClient


async def test_complete_pipeline_persists_and_deduplicates(repository) -> None:
    pipeline = JobPipeline(repository, HardFilter(), MockAIProvider(), ScoringEngine(), TelegramClient(None, None), 60)
    raw = RawJob(source="fl.ru", source_job_id="abc", title="Telegram bot + CRM", description="Нужна интеграция API CRM Telegram", budget_text="40 000 ₽")
    first = await pipeline.process(raw)
    second = await pipeline.process(raw)
    assert first is not None
    assert first.response_draft
    assert first.final_score >= 60
    assert second is None
