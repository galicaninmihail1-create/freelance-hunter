from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request

from app.ai import MockAIProvider, PolzaAIProvider
from app.collectors import FLRSSCollector
from app.config import OFFICIAL_FL_RSS_NAMES, OFFICIAL_FL_RSS_URL, Settings
from app.db import JobRepository
from app.filters import FilterSettings, HardFilter
from app.scoring import ScoringEngine
from app.services import LiveCanaryService, NonOverlappingScheduler, TelegramActionService
from app.telegram import TelegramClient


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    scoring = ScoringEngine()
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_allowed_chat_id)
    ai = (
        PolzaAIProvider(settings.polza_api_key, settings.polza_model, settings.polza_base_url)
        if settings.ai_provider == "polza"
        else MockAIProvider()
    )
    hard_filter = HardFilter(FilterSettings(settings.min_budget_rub))
    collector = FLRSSCollector(settings.fl_rss_urls)
    live_canary = LiveCanaryService(
        repository, hard_filter, ai, scoring, telegram, settings.telegram_score_threshold
    )
    actions = TelegramActionService(repository, ai, telegram)

    def prepare_feed_onboarding(raw_jobs):
        # This installation already ran the original Automation feed before feed
        # onboarding state existed. A fresh empty installation baselines every feed.
        if repository.list_all():
            live_canary.register_existing_feed(
                OFFICIAL_FL_RSS_NAMES[OFFICIAL_FL_RSS_URL],
                OFFICIAL_FL_RSS_URL,
            )
        return live_canary.onboard_new_feeds(raw_jobs, collector.last_feed_stats)

    async def poll_once():
        try:
            raw_jobs = await collector.collect()
        except Exception as exc:
            repository.record_processing_error(
                stage="rss", error_kind=exc.__class__.__name__, message=f"{exc.__class__.__name__}: operation failed"
            )
            logging.getLogger(__name__).warning("RSS collection failed", exc_info=True)
            return None
        for issue in collector.last_errors:
            repository.record_processing_error(
                stage=issue.stage, error_kind=issue.error_kind, message=issue.message
            )
        onboarding = prepare_feed_onboarding(raw_jobs)
        report = await live_canary.process_many(raw_jobs, collector.last_feed_stats)
        logging.getLogger(__name__).info(
            "Live poll telemetry: %s",
            json.dumps({"onboarding": onboarding.as_dict(), "run": report.as_dict()}, ensure_ascii=False),
        )
        return report

    scheduler = NonOverlappingScheduler(poll_once, settings.poll_interval_minutes * 60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task: asyncio.Task[None] | None = None
        if settings.hunter_mode == "live":
            baseline_count = live_canary.establish_pre_live_baseline()
            logging.getLogger(__name__).info(
                "Live notification baseline established for %d existing jobs",
                baseline_count,
            )
            task = asyncio.create_task(scheduler.run_forever())
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Freelance Hunter Live Canary v1", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.repository = repository
    app.state.live_canary = live_canary
    app.state.collector = collector
    app.state.scheduler = scheduler
    app.state.telegram_actions = actions

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": "freelance-hunter",
            "hunter_mode": settings.hunter_mode,
            "scheduler_enabled": settings.hunter_mode == "live",
            "telegram_enabled": telegram.enabled,
        }

    @app.post("/runs/dry-run")
    async def dry_run() -> dict:
        raw_jobs = await collector.collect()
        return live_canary.dry_run(raw_jobs, collector.last_feed_stats).as_dict()

    @app.post("/runs/collect")
    async def collect_now() -> dict:
        if settings.ai_provider != "polza":
            raise HTTPException(status_code=409, detail="AI_PROVIDER=polza is required for analyzer_v1")
        raw_jobs = await collector.collect()
        prepare_feed_onboarding(raw_jobs)
        return (await live_canary.process_many(raw_jobs, collector.last_feed_stats)).as_dict()

    @app.get("/jobs/shortlist")
    async def shortlist() -> list[dict]:
        return [job.model_dump(mode="json") for job in repository.list_shortlisted()]

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict:
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not settings.telegram_webhook_secret or not secrets.compare_digest(
            supplied_secret, settings.telegram_webhook_secret
        ):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
        update = await request.json()
        callback = update.get("callback_query") or {}
        if not callback:
            return {"ok": True}
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not telegram.enabled or chat_id != str(settings.telegram_allowed_chat_id):
            raise HTTPException(status_code=403, detail="Unauthorized Telegram chat")
        try:
            action, job_id = str(callback.get("data", "")).split(":", 1)
            result = await actions.handle(action, job_id)
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail="Invalid Telegram callback") from None
        return {"ok": True, "result": result}

    return app


app = create_app()
