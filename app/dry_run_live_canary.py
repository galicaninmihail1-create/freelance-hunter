"""RSS-only Live Canary dry-run; never calls Polza or Telegram and never writes jobs."""

from __future__ import annotations

import asyncio
import json
import sys

from app.ai import MockAIProvider
from app.collectors import FLRSSCollector
from app.config import OFFICIAL_FL_RSS_NAMES, OFFICIAL_FL_RSS_URL, OFFICIAL_FL_RSS_URLS, Settings
from app.db import JobRepository
from app.filters import FilterSettings, HardFilter
from app.scoring import ScoringEngine
from app.services.live_canary import LiveCanaryService
from app.telegram import TelegramClient


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.from_env()
    if len(settings.fl_rss_urls) != len(OFFICIAL_FL_RSS_URLS) or set(settings.fl_rss_urls) != set(OFFICIAL_FL_RSS_URLS):
        raise ValueError("Dry-run requires all approved official FL.ru RSS sources")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    count_before = len(repository.list_all())
    collector = FLRSSCollector(settings.fl_rss_urls)
    raw_jobs = await collector.collect()
    service = LiveCanaryService(
        repository,
        HardFilter(FilterSettings(settings.min_budget_rub)),
        MockAIProvider(),
        ScoringEngine(),
        TelegramClient(None, None),
        settings.telegram_score_threshold,
    )
    if repository.list_all():
        service.register_existing_feed(
            OFFICIAL_FL_RSS_NAMES[OFFICIAL_FL_RSS_URL],
            OFFICIAL_FL_RSS_URL,
        )
    onboarding = service.onboard_new_feeds(raw_jobs, collector.last_feed_stats)
    report = service.dry_run(raw_jobs, collector.last_feed_stats)
    count_after = len(repository.list_all())
    if count_before != count_after:
        raise RuntimeError("Dry-run unexpectedly modified the job database")
    print(json.dumps({
        **report.as_dict(),
        "database_jobs_before": count_before,
        "database_jobs_after": count_after,
        "polza_calls": 0,
        "telegram_messages": 0,
        "sources": list(settings.fl_rss_urls),
        "collector_errors": [issue.__dict__ for issue in collector.last_errors],
        "feed_onboarding": onboarding.as_dict(),
        "feed_baseline": repository.feed_baseline_summary(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
