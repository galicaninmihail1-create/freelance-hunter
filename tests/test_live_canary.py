import asyncio
import json
from dataclasses import replace

import httpx

from app.ai.polza import AIProviderError, PolzaStructuredAnalysis, ProviderUsage
from app.config import OFFICIAL_FL_RSS_URLS, Settings
from app.collectors.fl_rss import CollectorIssue, FeedCollectionStats
from app.db import JobRepository, dedupe_key_for
from app.filters import HardFilter
from app.main import create_app
from app.models import BudgetStatus, Job, RawJob, ResponseDraft
from app.normalizer import normalize
from app.scoring import ScoringEngine
from app.services.live_canary import LiveCanaryService, LiveRunReport, NonOverlappingScheduler, TelegramActionService
from app.services.polza_evaluation import ANALYZER_VERSION
from app.telegram import TelegramClient


class FakeProvider:
    provider_name = "polza"
    model = "qwen/qwen3.8-flash"

    def __init__(self, fail_titles: set[str] | None = None):
        self.fail_titles = fail_titles or set()
        self.completion_request_count = 0
        self.analysis_calls = 0
        self.draft_calls = 0
        self.last_usage = None
        self.last_analysis_usages = []
        self.last_structured_analysis = None

    async def analyze_job(self, job: Job):
        self.completion_request_count += 1
        self.analysis_calls += 1
        self.last_analysis_usages = []
        if job.title in self.fail_titles:
            raise AIProviderError("sanitized provider failure")
        usage = ProviderUsage(
            provider="polza", model=self.model, prompt_tokens=100,
            completion_tokens=50, total_tokens=150, reasoning_tokens=0, cost_rub=0.01,
        )
        self.last_usage = usage
        self.last_analysis_usages = [usage]
        self.last_structured_analysis = PolzaStructuredAnalysis(
            client_summary="Client needs a narrow CRM API integration",
            deliverable="Tested integration",
            required_skills=["API", "CRM"],
            complexity_score=4,
            codex_share=85,
            codex_share_reason="Integration code, mapping, automated tests, and documentation are AI-executable.",
            ai_executable_work=["API client", "Tests"],
            owner_required_work=["Acceptance"],
            external_dependency_work=["Credentials"],
            estimated_total_hours_min=2,
            estimated_total_hours_max=4,
            estimated_owner_hours_min=1.5,
            estimated_owner_hours_max=3,
            owner_communication_hours=0.5,
            owner_access_setup_hours=0.5,
            owner_review_testing_hours=1,
            owner_manual_execution_hours=0,
            requirement_clarity=8,
            technical_risk=2,
            commercial_risk=2,
            fit_score=8,
            win_probability_score=7,
            productization_score=9,
            problem_category="CRM Integration",
            technical_risks=[],
            commercial_risks=[],
            pricing_notes="",
            confidence=0.9,
        )
        return self.last_structured_analysis.to_job_analysis()

    async def generate_response(self, job: Job, analysis):
        self.draft_calls += 1
        return ResponseDraft(
            text="Предлагаю уточнить доступы и затем подготовить проверяемую интеграцию с документацией.",
            questions=["Какие API-доступы уже есть?"],
        )


class FakeTelegram:
    def __init__(self, notify_result: bool = True):
        self.notify_result = notify_result
        self.notifications: list[str] = []
        self.card_types: list[str] = []
        self.job_snapshots: list[Job] = []
        self.texts: list[str] = []

    async def notify(self, job: Job, scoring: ScoringEngine, *, fallback: bool = False) -> bool:
        self.notifications.append(job.id)
        self.card_types.append("fallback" if fallback else "enriched")
        self.job_snapshots.append(job.model_copy(deep=True))
        return self.notify_result

    async def send_text(self, text: str) -> bool:
        self.texts.append(text)
        return True


def raw_job(source_id: str, title: str = "Telegram CRM API integration") -> RawJob:
    return RawJob(
        source="fl.ru", source_job_id=source_id, title=title,
        description="Нужна автоматизация CRM через API и Telegram",
        url=f"https://example.test/{source_id}",
    )


def analyzed_job(source_id: str = "analyzed") -> Job:
    job = Job(
        source="fl.ru", source_job_id=source_id, title="Telegram CRM API integration",
        description="Нужна автоматизация CRM через API", source_budget_status=BudgetStatus.UNKNOWN,
        analysis_analyzer_version=ANALYZER_VERSION,
        analysis_summary="Client needs integration", analysis_deliverable="Tested integration",
        analysis_required_skills=["API"], complexity_score=4, codex_share=85,
        estimated_total_hours=3, estimated_owner_hours=2,
        estimated_total_hours_min=2, estimated_total_hours_max=4,
        estimated_owner_hours_min=1.5, estimated_owner_hours_max=3,
        fit_score=80, win_probability_score=70, productization_score=90,
        analysis_requirement_clarity_score=8, technical_risk_score=2,
        problem_category="CRM Integration", analysis_confidence=0.9,
    )
    return job


async def test_scheduler_overlap_protection() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run():
        started.set()
        await release.wait()

    scheduler = NonOverlappingScheduler(slow_run, interval_seconds=60)
    first = asyncio.create_task(scheduler.trigger())
    await started.wait()
    assert await scheduler.trigger() is False
    release.set()
    assert await first is True


def test_persistent_dedupe_across_repository_restart(settings: Settings) -> None:
    first = JobRepository(settings.database_url)
    first.initialize()
    job = normalize(raw_job("restart-dedupe"))
    first.save(job, dedupe_key_for(job))

    restarted = JobRepository(settings.database_url)
    restarted.initialize()
    assert restarted.exists(job.source, dedupe_key_for(job))


def test_dry_run_is_read_only_and_reports_new_notification_candidates(repository) -> None:
    existing = analyzed_job("dry-existing")
    existing.final_score = 72
    existing.score_is_provisional = True
    repository.save(existing, dedupe_key_for(existing))
    service = LiveCanaryService(
        repository, HardFilter(), FakeProvider(), ScoringEngine(), FakeTelegram(), 50
    )
    report = service.dry_run([
        raw_job("dry-existing"),
        raw_job("dry-new"),
        RawJob(source="fl.ru", source_job_id="irrelevant", title="Нужен курьер", description="Разовая доставка документов"),
    ])
    assert report.rss_items_seen == 3
    assert report.duplicates == 1
    assert report.existing_analysis_telegram_eligible == 1
    assert report.new_jobs == 2
    assert report.jobs_requiring_ai == 2
    assert report.hard_filtered == 1
    assert report.new_notification_candidates == 2
    assert len(repository.list_all()) == 1


async def test_threshold_accepts_provisional_score_and_rejects_below_threshold(repository) -> None:
    provider = FakeProvider()
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)
    report = await service.process_many([raw_job("provisional")])
    saved = repository.get_by_dedupe("fl.ru", dedupe_key_for(normalize(raw_job("provisional"))))
    assert saved is not None and saved.score_is_provisional
    assert report.telegram_eligible == 1
    assert report.telegram_sent == 1

    high_threshold = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 90)
    second = await high_threshold.process_many([raw_job("below")])
    assert second.analyzed == 1
    assert second.telegram_eligible == 1
    assert second.telegram_sent == 1
    below = repository.get_by_dedupe("fl.ru", dedupe_key_for(normalize(raw_job("below"))))
    assert below is not None and below.status.value == "REJECTED"
    assert telegram.card_types == ["enriched", "enriched"]


async def test_hard_filtered_job_keeps_classification_and_is_still_notified(repository) -> None:
    provider = FakeProvider()
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)
    rejected = RawJob(
        source="fl.ru", source_job_id="hard-rejected", title="Нужен курьер",
        description="Разовая доставка бумажных документов по городу",
    )

    report = await service.process_many([rejected])

    saved = repository.get_by_dedupe("fl.ru", dedupe_key_for(normalize(rejected)))
    assert report.hard_filtered == 1
    assert report.new_notification_candidates == 1
    assert report.telegram_sent == 1
    assert provider.analysis_calls == 1
    assert telegram.card_types == ["enriched"]
    assert saved is not None and saved.status.value == "REJECTED"
    assert "outside_target_profile" in saved.commercial_risks


async def test_cross_feed_duplicate_is_analyzed_and_notified_once(repository) -> None:
    provider = FakeProvider()
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)
    python_feed = raw_job("same-guid")
    python_feed.metadata = {"feed_name": "Python", "feed_url": "https://www.fl.ru/rss/python"}
    api_feed = raw_job("same-guid")
    api_feed.metadata = {"feed_name": "Интеграция по API", "feed_url": "https://www.fl.ru/rss/api"}

    report = await service.process_many([python_feed, api_feed])

    assert len(repository.list_all()) == 1
    assert provider.analysis_calls == 1
    assert len(telegram.notifications) == 1
    assert report.new_jobs == 1
    assert report.duplicates == 1
    assert report.cross_feed_duplicates == 1
    by_name = {item.feed_name: item for item in report.feeds}
    assert by_name["Python"].new_unique_jobs == 1
    assert by_name["Интеграция по API"].new_unique_jobs == 0
    assert by_name["Интеграция по API"].cross_feed_duplicates == 1


async def test_new_feed_onboarding_suppresses_77_historical_jobs_and_allows_future_job(repository) -> None:
    existing = analyzed_job("existing-database-history")
    repository.save(existing, dedupe_key_for(existing))
    existing_snapshot = repository.get(existing.id).model_dump_json()
    provider = FakeProvider()
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)
    feed_a = ("Python", "https://www.fl.ru/rss/python")
    feed_b = ("Разработка чат-ботов", "https://www.fl.ru/rss/chatbots")

    baseline_raw: list[RawJob] = []
    for index in range(77):
        for feed_name, feed_url in (feed_a, feed_b):
            item = raw_job(f"historical-{index}")
            item.metadata = {"feed_name": feed_name, "feed_url": feed_url}
            baseline_raw.append(item)
    stats = [
        FeedCollectionStats("fl.ru", feed_a[0], feed_a[1], 77, 77),
        FeedCollectionStats("fl.ru", feed_b[0], feed_b[1], 77, 77),
    ]

    onboarding = service.onboard_new_feeds(baseline_raw, stats)
    baseline_report = await service.process_many(baseline_raw, stats)

    assert onboarding.baseline_identity_associations_added == 154
    assert onboarding.baseline_unique_identities_added == 77
    assert onboarding.identical_identity_sets == [["Python", "Разработка чат-ботов"]]
    assert baseline_report.historical_suppressed == 77
    assert baseline_report.cross_feed_duplicates == 77
    assert provider.analysis_calls == 0
    assert telegram.notifications == []
    assert len(repository.list_all()) == 1
    assert repository.get(existing.id).model_dump_json() == existing_snapshot

    future = raw_job("genuinely-new-after-baseline")
    future.metadata = {"feed_name": feed_a[0], "feed_url": feed_a[1]}
    future_report = await service.process_many([future])

    assert future_report.new_jobs == 1
    assert future_report.analyzed == 1
    assert future_report.telegram_sent == 1
    assert provider.analysis_calls == 1
    assert len(telegram.notifications) == 1


async def test_feed_baseline_survives_repository_restart(settings: Settings) -> None:
    first_repository = JobRepository(settings.database_url)
    first_repository.initialize()
    first_service = LiveCanaryService(
        first_repository, HardFilter(), FakeProvider(), ScoringEngine(), FakeTelegram(), 50
    )
    feed_url = "https://www.fl.ru/rss/restart-baseline"
    historical = raw_job("restart-historical")
    historical.metadata = {"feed_name": "Python", "feed_url": feed_url}
    stats = [FeedCollectionStats("fl.ru", "Python", feed_url, 1, 1)]
    first_service.onboard_new_feeds([historical], stats)

    restarted_repository = JobRepository(settings.database_url)
    restarted_repository.initialize()
    provider = FakeProvider()
    telegram = FakeTelegram()
    restarted_service = LiveCanaryService(
        restarted_repository, HardFilter(), provider, ScoringEngine(), telegram, 50
    )
    assert json.dumps(restarted_repository.feed_baseline_summary())
    report = await restarted_service.process_many([historical], stats)

    assert report.historical_suppressed == 1
    assert provider.analysis_calls == 0
    assert telegram.notifications == []
    assert restarted_repository.list_all() == []


async def test_notification_is_not_duplicated_after_restart(settings: Settings) -> None:
    first_repo = JobRepository(settings.database_url)
    first_repo.initialize()
    first_telegram = FakeTelegram()
    first = LiveCanaryService(first_repo, HardFilter(), FakeProvider(), ScoringEngine(), first_telegram, 50)
    await first.process_many([raw_job("notify-once")])
    assert len(first_telegram.notifications) == 1

    restarted_repo = JobRepository(settings.database_url)
    restarted_repo.initialize()
    restarted_telegram = FakeTelegram()
    restarted = LiveCanaryService(restarted_repo, HardFilter(), FakeProvider(), ScoringEngine(), restarted_telegram, 50)
    report = await restarted.process_many([raw_job("notify-once")])
    assert report.duplicates == 1
    assert restarted_telegram.notifications == []


async def test_first_live_startup_suppresses_existing_job_and_notifies_new_job(
    settings: Settings,
    monkeypatch,
) -> None:
    app = create_app(replace(settings, hunter_mode="live"))
    existing = analyzed_job("pre-live-eligible")
    existing.final_score = 99
    app.state.repository.save(existing, dedupe_key_for(existing))

    provider = FakeProvider()
    telegram = FakeTelegram()
    app.state.live_canary.ai = provider
    app.state.live_canary.telegram = telegram

    async def first_live_poll():
        return [raw_job("pre-live-eligible"), raw_job("new-after-live")]

    monkeypatch.setattr(app.state.collector, "collect", first_live_poll)
    run_finished = asyncio.Event()

    async def run_once_then_stop():
        await app.state.scheduler.trigger()
        run_finished.set()

    monkeypatch.setattr(app.state.scheduler, "run_forever", run_once_then_stop)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(run_finished.wait(), timeout=2)

    new_job = app.state.repository.get_by_dedupe(
        "fl.ru",
        dedupe_key_for(normalize(raw_job("new-after-live"))),
    )
    assert new_job is not None
    assert provider.analysis_calls == 1
    assert telegram.notifications == [new_job.id]
    assert app.state.repository.notification_status(existing.id, ANALYZER_VERSION) == "suppressed_pre_live"
    assert app.state.repository.notification_status(new_job.id, ANALYZER_VERSION) == "sent"


async def test_feedback_actions_are_persisted(repository) -> None:
    job = analyzed_job("feedback")
    repository.save(job, dedupe_key_for(job))
    actions = TelegramActionService(repository, FakeProvider(), FakeTelegram())
    assert await actions.handle("interested", job.id) == "interested"
    assert await actions.handle("rejected", job.id) == "rejected"
    assert {item["action"] for item in repository.list_actions(job.id)} == {"interested", "rejected"}
    assert all(item["analyzer_version"] == "v1" for item in repository.list_actions(job.id))


async def test_draft_request_is_idempotent(repository) -> None:
    job = analyzed_job("draft")
    repository.save(job, dedupe_key_for(job))
    provider = FakeProvider()
    telegram = FakeTelegram()
    actions = TelegramActionService(repository, provider, telegram)
    assert await actions.handle("draft_requested", job.id) == "draft_completed"
    assert await actions.handle("draft_requested", job.id) == "draft_cached"
    assert provider.draft_calls == 1
    assert len(repository.list_actions(job.id)) == 1
    assert repository.get_draft_request(job.id, "v1")["status"] == "completed"


async def test_polza_failure_isolated_and_next_job_continues(repository) -> None:
    provider = FakeProvider(fail_titles={"Fail Telegram CRM API"})
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)
    report = await service.process_many([
        raw_job("fail", "Fail Telegram CRM API"),
        raw_job("success", "Success Telegram CRM API"),
    ])
    assert report.analysis_failed == 1
    assert report.analyzed == 1
    assert provider.analysis_calls == 2
    assert report.telegram_sent == 2
    assert telegram.card_types == ["fallback", "enriched"]
    failed = repository.get_by_dedupe("fl.ru", dedupe_key_for(normalize(raw_job("fail", "Fail Telegram CRM API"))))
    assert failed is not None and failed.status.value == "NEW"
    assert repository.list_processing_errors()[0]["stage"] == "analysis"


async def test_invalid_structured_analysis_uses_fallback(repository) -> None:
    provider = FakeProvider()
    original_analyze = provider.analyze_job

    async def invalid_structured(job: Job):
        analysis = await original_analyze(job)
        provider.last_structured_analysis = None
        return analysis

    provider.analyze_job = invalid_structured
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)

    report = await service.process_many([raw_job("invalid-structured")])

    assert report.analysis_failed == 1
    assert report.telegram_sent == 1
    assert telegram.card_types == ["fallback"]
    assert repository.list_processing_errors()[0]["stage"] == "analysis"


async def test_unexpected_enrichment_exception_uses_fallback(repository) -> None:
    provider = FakeProvider()

    async def unexpected(job: Job):
        provider.analysis_calls += 1
        raise RuntimeError("unexpected enrichment failure")

    provider.analyze_job = unexpected
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), provider, ScoringEngine(), telegram, 50)

    report = await service.process_many([raw_job("unexpected-enrichment")])

    assert report.analysis_failed == 1
    assert report.telegram_sent == 1
    assert telegram.card_types == ["fallback"]
    assert repository.list_processing_errors()[0]["error_kind"] == "RuntimeError"


async def test_existing_notification_reservation_blocks_second_attempt(repository) -> None:
    job = analyzed_job("reserved")
    repository.save(job, dedupe_key_for(job))
    assert repository.reserve_notification(job.id, ANALYZER_VERSION)
    telegram = FakeTelegram()
    service = LiveCanaryService(repository, HardFilter(), FakeProvider(), ScoringEngine(), telegram, 50)
    report = LiveRunReport()

    await service._notify(job, report)

    assert report.duplicate_notifications_skipped == 1
    assert telegram.notifications == []


async def test_telegram_failure_isolated_and_next_job_continues(repository) -> None:
    telegram = FakeTelegram(notify_result=False)
    service = LiveCanaryService(repository, HardFilter(), FakeProvider(), ScoringEngine(), telegram, 50)
    report = await service.process_many([raw_job("tg-fail-1"), raw_job("tg-fail-2")])
    assert report.analyzed == 2
    assert report.telegram_failed == 2
    assert len(telegram.notifications) == 2
    for source_id in ("tg-fail-1", "tg-fail-2"):
        saved = repository.get_by_dedupe("fl.ru", dedupe_key_for(normalize(raw_job(source_id))))
        assert saved is not None
        assert repository.notification_status(saved.id, ANALYZER_VERSION) == "failed"
    assert len([item for item in repository.list_processing_errors() if item["stage"] == "telegram"]) == 2


async def test_telegram_token_is_not_persisted_when_delivery_fails(repository, caplog) -> None:
    token = "123456:RAW-PERSISTENCE-SECRET"

    async def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as http_client:
        service = LiveCanaryService(
            repository,
            HardFilter(),
            FakeProvider(),
            ScoringEngine(),
            TelegramClient(token, "42", client=http_client),
            50,
        )
        report = await service.process_many([raw_job("telegram-secret-redaction")])

    assert report.telegram_failed == 1
    assert token not in caplog.text
    assert token not in repr(repository.list_processing_errors())


async def test_manual_mode_does_not_start_scheduler(settings: Settings, monkeypatch) -> None:
    app = create_app(settings)
    started = False

    async def forbidden_start():
        nonlocal started
        started = True

    monkeypatch.setattr(app.state.scheduler, "run_forever", forbidden_start)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert settings.hunter_mode == "manual"
    assert started is False


async def test_poll_persists_collector_issues_without_crashing(settings: Settings, monkeypatch) -> None:
    app = create_app(settings)

    async def collect_with_issue():
        app.state.collector.last_errors = [
            CollectorIssue(stage="rss_item", error_kind="ValueError", message="malformed RSS entry skipped")
        ]
        return []

    monkeypatch.setattr(app.state.collector, "collect", collect_with_issue)
    assert await app.state.scheduler.trigger() is True
    errors = app.state.repository.list_processing_errors()
    assert len(errors) == 1
    assert errors[0]["stage"] == "rss_item"


async def test_webhook_requires_secret_and_authorized_chat(settings: Settings) -> None:
    secured = replace(
        settings,
        telegram_bot_token="test-token",
        telegram_allowed_chat_id="42",
        telegram_webhook_secret="test_webhook_secret",
    )
    app = create_app(secured)
    job = analyzed_job("webhook-feedback")
    app.state.repository.save(job, dedupe_key_for(job))
    payload = {
        "callback_query": {
            "data": f"interested:{job.id}",
            "message": {"chat": {"id": 42}},
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.post("/telegram/webhook", json=payload)
        accepted = await client.post(
            "/telegram/webhook", json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_webhook_secret"},
        )
    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert app.state.repository.list_actions(job.id)[0]["action"] == "interested"


def test_local_live_configuration_does_not_require_a_webhook(settings: Settings) -> None:
    local_live = replace(
        settings,
        hunter_mode="live",
        ai_provider="polza",
        polza_api_key="present",
        fl_rss_urls=OFFICIAL_FL_RSS_URLS,
        telegram_bot_token="present",
        telegram_allowed_chat_id="42",
        telegram_webhook_secret=None,
    )
    local_live.validate()
