"""Safe live-canary orchestration around the frozen analyzer_v1."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from app.ai import AIProvider
from app.analysis_version import ANALYSIS_PROMPT_VERSION, ANALYZER_VERSION
from app.collectors.fl_rss import FeedCollectionStats
from app.db import JobRepository, dedupe_key_for
from app.filters import HardFilter
from app.models import Job, JobAnalysis, JobStatus, RawJob
from app.normalizer import normalize
from app.scoring import ScoringEngine
from app.services.polza_evaluation import ControlledPolzaEvaluation
from app.telegram import TelegramClient

logger = logging.getLogger(__name__)


@dataclass
class FeedRunReport:
    source: str
    feed_name: str
    feed_url: str
    items_seen: int = 0
    new_unique_jobs: int = 0
    cross_feed_duplicates: int = 0
    historical_suppressed: int = 0


@dataclass
class FeedOnboardingReport:
    newly_onboarded_feeds: list[str] = field(default_factory=list)
    baseline_identity_associations_added: int = 0
    baseline_unique_identities_added: int = 0
    identical_identity_sets: list[list[str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class LiveRunReport:
    rss_items_seen: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    hard_filtered: int = 0
    ai_requested: int = 0
    analyzed: int = 0
    analysis_failed: int = 0
    telegram_eligible: int = 0
    telegram_sent: int = 0
    telegram_failed: int = 0
    duplicate_notifications_skipped: int = 0
    malformed_items: int = 0
    cross_feed_duplicates: int = 0
    historical_suppressed: int = 0
    feeds: list[FeedRunReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class DryRunReport:
    rss_items_seen: int = 0
    new_jobs: int = 0
    duplicates: int = 0
    hard_filtered: int = 0
    jobs_requiring_ai: int = 0
    existing_analysis_telegram_eligible: int = 0
    malformed_items: int = 0
    cross_feed_duplicates: int = 0
    historical_suppressed: int = 0
    feeds: list[FeedRunReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _feed_reports(
    raw_jobs: list[RawJob],
    collection_stats: list[FeedCollectionStats] | None,
) -> dict[str, FeedRunReport]:
    reports: dict[str, FeedRunReport] = {}
    for stats in collection_stats or []:
        reports[stats.feed_url] = FeedRunReport(
            source=stats.source,
            feed_name=stats.feed_name,
            feed_url=stats.feed_url,
            items_seen=stats.items_seen,
        )
    if collection_stats is None:
        for raw in raw_jobs:
            url = str(raw.metadata.get("feed_url") or raw.source)
            name = str(raw.metadata.get("feed_name") or url)
            report = reports.setdefault(url, FeedRunReport(raw.source, name, url))
            report.items_seen += 1
    return reports


def _feed_report(raw: RawJob, reports: dict[str, FeedRunReport]) -> FeedRunReport:
    url = str(raw.metadata.get("feed_url") or raw.source)
    name = str(raw.metadata.get("feed_name") or url)
    return reports.setdefault(url, FeedRunReport(raw.source, name, url))


def _safe_failure_message(error: object) -> str:
    # Provider exceptions are already sanitized; other exception messages are not
    # persisted because third-party libraries may embed sensitive request data.
    if error.__class__.__name__ == "AIProviderError":
        return str(error)[:300]
    return f"{error.__class__.__name__}: operation failed"


class LiveCanaryService:
    def __init__(
        self,
        repository: JobRepository,
        hard_filter: HardFilter,
        ai: AIProvider,
        scoring: ScoringEngine,
        telegram: TelegramClient,
        telegram_score_threshold: float,
    ):
        self.repository = repository
        self.hard_filter = hard_filter
        self.ai = ai
        self.scoring = scoring
        self.telegram = telegram
        self.telegram_score_threshold = telegram_score_threshold

    def establish_pre_live_baseline(self) -> int:
        """Classify jobs already in SQLite as historical before live polling begins."""
        return self.repository.baseline_existing_notifications(ANALYZER_VERSION)

    def register_existing_feed(self, feed_name: str, feed_url: str) -> bool:
        return self.repository.register_existing_feed("fl.ru", feed_name, feed_url)

    def onboard_new_feeds(
        self,
        raw_jobs: list[RawJob],
        collection_stats: list[FeedCollectionStats],
    ) -> FeedOnboardingReport:
        identities_by_feed: dict[str, set[str]] = {
            item.feed_url: set() for item in collection_stats if item.error_kind is None
        }
        feed_details = {
            item.feed_url: (item.source, item.feed_name) for item in collection_stats
            if item.error_kind is None
        }
        for raw in raw_jobs:
            feed_url = str(raw.metadata.get("feed_url") or raw.source)
            # Only baseline feeds that the collector explicitly reported as a
            # successful fetch. This avoids treating synthetic/manual inputs as
            # a newly onboarded RSS feed and prevents failed feeds from being
            # marked ready with a partial or empty baseline.
            if feed_url not in identities_by_feed:
                continue
            try:
                job = normalize(raw)
                identities_by_feed[feed_url].add(dedupe_key_for(job))
            except Exception:
                continue

        report = FeedOnboardingReport()
        unique_added: set[str] = set()
        for feed_url, identities in identities_by_feed.items():
            if self.repository.feed_is_onboarded(feed_url):
                continue
            source, feed_name = feed_details.get(feed_url, ("fl.ru", feed_url))
            added = self.repository.onboard_feed(source, feed_name, feed_url, identities)
            report.newly_onboarded_feeds.append(feed_name)
            report.baseline_identity_associations_added += added
            unique_added.update(identities)
        report.baseline_unique_identities_added = len(unique_added)

        feeds_by_identity_set: dict[frozenset[str], list[str]] = {}
        for feed_url, identities in identities_by_feed.items():
            if not identities:
                continue
            _, feed_name = feed_details.get(feed_url, ("fl.ru", feed_url))
            feeds_by_identity_set.setdefault(frozenset(identities), []).append(feed_name)
        report.identical_identity_sets = [
            sorted(feed_names)
            for feed_names in feeds_by_identity_set.values()
            if len(feed_names) > 1
        ]
        return report

    def dry_run(
        self,
        raw_jobs: list[RawJob],
        collection_stats: list[FeedCollectionStats] | None = None,
    ) -> DryRunReport:
        feed_reports = _feed_reports(raw_jobs, collection_stats)
        report = DryRunReport(
            rss_items_seen=sum(item.items_seen for item in feed_reports.values()),
            feeds=list(feed_reports.values()),
        )
        first_feed_by_identity: dict[tuple[str, str], str] = {}
        for raw in raw_jobs:
            try:
                job = normalize(raw)
                key = dedupe_key_for(job)
                identity = (job.source, key)
                current_feed = _feed_report(raw, feed_reports)
                if identity in first_feed_by_identity:
                    report.duplicates += 1
                    if first_feed_by_identity[identity] != current_feed.feed_url:
                        report.cross_feed_duplicates += 1
                        current_feed.cross_feed_duplicates += 1
                    continue
                first_feed_by_identity[identity] = current_feed.feed_url
                if self.repository.is_suppressed_feed_identity(job.source, key):
                    report.historical_suppressed += 1
                    current_feed.historical_suppressed += 1
                    continue
                existing = self.repository.get_by_dedupe(job.source, key)
                if existing is not None:
                    report.duplicates += 1
                    if (
                        existing.analysis_analyzer_version == ANALYZER_VERSION
                        and existing.final_score is not None
                        and existing.final_score >= self.telegram_score_threshold
                    ):
                        report.existing_analysis_telegram_eligible += 1
                    continue
                report.new_jobs += 1
                current_feed.new_unique_jobs += 1
                if not self.hard_filter.evaluate(job).accepted:
                    report.hard_filtered += 1
                else:
                    report.jobs_requiring_ai += 1
            except Exception:
                report.malformed_items += 1
        return report

    async def process_many(
        self,
        raw_jobs: list[RawJob],
        collection_stats: list[FeedCollectionStats] | None = None,
    ) -> LiveRunReport:
        feed_reports = _feed_reports(raw_jobs, collection_stats)
        report = LiveRunReport(
            rss_items_seen=sum(item.items_seen for item in feed_reports.values()),
            feeds=list(feed_reports.values()),
        )
        first_feed_by_identity: dict[tuple[str, str], str] = {}
        for raw in raw_jobs:
            try:
                job = normalize(raw)
                key = dedupe_key_for(job)
                identity = (job.source, key)
                current_feed = _feed_report(raw, feed_reports)
                if identity in first_feed_by_identity:
                    report.duplicates += 1
                    if first_feed_by_identity[identity] != current_feed.feed_url:
                        report.cross_feed_duplicates += 1
                        current_feed.cross_feed_duplicates += 1
                    continue
                first_feed_by_identity[identity] = current_feed.feed_url
                if self.repository.is_suppressed_feed_identity(job.source, key):
                    report.historical_suppressed += 1
                    current_feed.historical_suppressed += 1
                    continue
                if self.repository.get_by_dedupe(job.source, key) is None:
                    current_feed.new_unique_jobs += 1
                await self._process_one(raw, report)
            except Exception as exc:
                report.malformed_items += 1
                self.repository.record_processing_error(
                    stage="item", error_kind=exc.__class__.__name__, message=_safe_failure_message(exc)
                )
                logger.warning("Live Canary isolated an item failure of type %s", exc.__class__.__name__)
        return report

    async def _process_one(self, raw: RawJob, report: LiveRunReport) -> None:
        job = normalize(raw)
        key = dedupe_key_for(job)
        existing = self.repository.get_by_dedupe(job.source, key)
        if existing is not None:
            report.duplicates += 1
            # A notification is only created on the new-job path below. Historical
            # and previously processed jobs are dedupe-only, regardless of score.
            return

        report.new_jobs += 1
        self.repository.save(job, key)
        filter_result = self.hard_filter.evaluate(job)
        if not filter_result.accepted:
            report.hard_filtered += 1
            job.status = JobStatus.REJECTED
            job.commercial_risks = [filter_result.reason or "hard_filter"]
            self.repository.save(job, key)
            return

        report.ai_requested += 1
        evaluator = ControlledPolzaEvaluation(
            self.repository,
            self.ai,  # type: ignore[arg-type]
            self.scoring,
            self.telegram_score_threshold,
            analysis_prompt_version=ANALYSIS_PROMPT_VERSION,
            analyzer_version=ANALYZER_VERSION,
            preserve_previous_analysis=True,
        )
        result = await evaluator.run([job])
        if result.failures:
            report.analysis_failed += 1
            failure = result.failures[0]
            self.repository.record_processing_error(
                stage="analysis", error_kind="AIProviderError", message=failure.error, job_id=job.id
            )
            return

        analyzed_job = result.jobs[0]
        report.analyzed += 1
        if analyzed_job.final_score is None or analyzed_job.final_score < self.telegram_score_threshold:
            return

        await self._notify(analyzed_job, report)

    async def _notify(self, analyzed_job: Job, report: LiveRunReport) -> None:
        report.telegram_eligible += 1
        if not self.repository.reserve_notification(analyzed_job.id, ANALYZER_VERSION):
            report.duplicate_notifications_skipped += 1
            return
        try:
            sent = await self.telegram.notify(analyzed_job, self.scoring)
        except Exception as exc:
            sent = False
            logger.warning("Telegram failure isolated for job %s (%s)", analyzed_job.id, exc.__class__.__name__)
        if sent:
            self.repository.mark_notification(analyzed_job.id, ANALYZER_VERSION, "sent")
            report.telegram_sent += 1
        else:
            self.repository.mark_notification(
                analyzed_job.id, ANALYZER_VERSION, "failed", "Telegram notify returned false"
            )
            self.repository.record_processing_error(
                stage="telegram", error_kind="TelegramDeliveryError",
                message="Telegram notify returned false", job_id=analyzed_job.id,
            )
            report.telegram_failed += 1


def persisted_analysis(job: Job) -> JobAnalysis:
    required = (
        job.analysis_summary,
        job.analysis_deliverable,
        job.complexity_score,
        job.estimated_total_hours,
        job.estimated_owner_hours,
        job.codex_share,
        job.fit_score,
        job.win_probability_score,
        job.productization_score,
        job.problem_category,
    )
    if job.analysis_analyzer_version != ANALYZER_VERSION or any(value is None for value in required):
        raise ValueError("A current analyzer_v1 result is required before drafting")
    return JobAnalysis(
        summary=job.analysis_summary,
        expected_deliverable=job.analysis_deliverable,
        required_technologies=job.analysis_required_skills,
        technical_complexity=job.complexity_score,
        estimated_total_hours=job.estimated_total_hours,
        estimated_owner_hours=job.estimated_owner_hours,
        codex_share=job.codex_share,
        technical_risks=job.technical_risks,
        commercial_risks=job.commercial_risks,
        requirement_clarity=(job.analysis_requirement_clarity_score or 0) * 10,
        hidden_complexity=(job.technical_risk_score or 0) * 10,
        capability_fit=job.fit_score,
        win_probability=job.win_probability_score,
        productization_potential=job.productization_score,
        strategic_value=job.productization_score,
        problem_category=job.problem_category,
        recommended_price=None,
        pricing_notes=job.analysis_pricing_notes or "",
        uncertainty_notes=f"Provider confidence: {job.analysis_confidence or 0:.2f}",
    )


class TelegramActionService:
    def __init__(self, repository: JobRepository, ai: AIProvider, telegram: TelegramClient):
        self.repository, self.ai, self.telegram = repository, ai, telegram

    async def handle(self, action: str, job_id: str) -> str:
        job = self.repository.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if action in {"interested", "rejected"}:
            self.repository.record_action(job_id, action, ANALYZER_VERSION)
            return action
        if action != "draft_requested":
            raise ValueError("unsupported action")

        existing = self.repository.get_draft_request(job_id, ANALYZER_VERSION)
        if existing is not None:
            if existing["status"] == "completed" and existing["draft"]:
                await self.telegram.send_text(_editable_draft_message(str(existing["draft"])))
                return "draft_cached"
            return f"draft_{existing['status']}"

        if not self.repository.reserve_draft_request(job_id, ANALYZER_VERSION):
            return "draft_reserved"
        self.repository.record_action(job_id, "draft_requested", ANALYZER_VERSION)
        try:
            draft = await self.ai.generate_response(job, persisted_analysis(job))
            self.repository.finish_draft_request(job_id, ANALYZER_VERSION, draft=draft.text)
            job.response_draft = draft.text
            self.repository.save(job, dedupe_key_for(job))
            sent = await self.telegram.send_text(_editable_draft_message(draft.text))
            if not sent:
                self.repository.record_processing_error(
                    stage="telegram_draft", error_kind="TelegramDeliveryError",
                    message="Telegram draft delivery returned false", job_id=job_id,
                )
            return "draft_completed"
        except Exception as exc:
            self.repository.finish_draft_request(
                job_id, ANALYZER_VERSION, error=_safe_failure_message(exc)
            )
            self.repository.record_processing_error(
                stage="draft", error_kind=exc.__class__.__name__,
                message=_safe_failure_message(exc), job_id=job_id,
            )
            return "draft_failed"


def _editable_draft_message(draft: str) -> str:
    return "✍️ Черновик отклика — проверьте и отредактируйте перед ручной отправкой:\n\n" + draft


class NonOverlappingScheduler:
    def __init__(self, run_once: Callable[[], Awaitable[object]], interval_seconds: float):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.run_once = run_once
        self.interval_seconds = interval_seconds
        self._lock = asyncio.Lock()

    async def trigger(self) -> bool:
        if self._lock.locked():
            logger.warning("Skipping overlapping Live Canary polling run")
            return False
        async with self._lock:
            await self.run_once()
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                await self.trigger()
            except Exception:
                logger.exception("Live Canary polling run failed")
            await asyncio.sleep(self.interval_seconds)
