"""Bounded one-shot production canary without FastAPI lifespan or a scheduler."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from app.ai import MockAIProvider, PolzaAIProvider
from app.collectors import FLRSSCollector
from app.config import OFFICIAL_FL_RSS_NAMES, OFFICIAL_FL_RSS_URL, Settings
from app.db import JobRepository, dedupe_key_for
from app.filters import FilterSettings, HardFilter
from app.models import Job, RawJob
from app.normalizer import normalize
from app.scoring import ScoringEngine
from app.services.live_canary import LiveCanaryService, LiveRunReport
from app.services.polza_evaluation import ANALYZER_VERSION
from app.telegram import TelegramClient
from app.telegram.client import _budget


@dataclass(frozen=True)
class CandidateSelection:
    selected: tuple[RawJob, ...]
    historical_suppressed: int = 0
    batch_duplicates: int = 0
    persistent_duplicates: int = 0
    malformed_items: int = 0


@dataclass(frozen=True)
class CanaryRuntime:
    repository: JobRepository
    collector: FLRSSCollector
    service: LiveCanaryService


@dataclass(frozen=True)
class CanaryExecution:
    fetched_items: int
    selection: CandidateSelection
    run1: LiveRunReport
    run2: LiveRunReport | None
    processing_errors_added: int
    jobs: tuple[Job, ...]


def bounded_job_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 3") from exc
    if count not in {1, 2, 3}:
        raise argparse.ArgumentTypeError("must be from 1 to 3")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded Freelance Hunter live canary")
    parser.add_argument("--max-new-jobs", required=True, type=bounded_job_count)
    parser.add_argument("--verify-dedupe", action="store_true")
    parser.add_argument("--confirm-telegram-destination", action="store_true")
    return parser


def select_fixed_candidates(
    raw_jobs: Sequence[RawJob],
    repository: JobRepository,
    max_new_jobs: int,
) -> CandidateSelection:
    """Select the first N eligible identities in feed order without writing state."""
    if max_new_jobs not in {1, 2, 3}:
        raise ValueError("max_new_jobs must be from 1 to 3")
    selected: list[RawJob] = []
    seen: set[tuple[str, str]] = set()
    historical = batch_duplicates = persistent_duplicates = malformed = 0
    for raw in raw_jobs:
        try:
            job = normalize(raw)
            key = dedupe_key_for(job)
        except Exception:
            malformed += 1
            continue
        identity = (job.source, key)
        if identity in seen:
            batch_duplicates += 1
            continue
        seen.add(identity)
        if repository.is_suppressed_feed_identity(job.source, key):
            historical += 1
            continue
        if repository.get_by_dedupe(job.source, key) is not None:
            persistent_duplicates += 1
            continue
        if len(selected) < max_new_jobs:
            selected.append(raw)
    return CandidateSelection(
        selected=tuple(selected),
        historical_suppressed=historical,
        batch_duplicates=batch_duplicates,
        persistent_duplicates=persistent_duplicates,
        malformed_items=malformed,
    )


def build_runtime(settings: Settings) -> CanaryRuntime:
    repository = JobRepository(settings.database_url)
    repository.initialize()
    scoring = ScoringEngine()
    ai = (
        PolzaAIProvider(settings.polza_api_key, settings.polza_model, settings.polza_base_url)
        if settings.ai_provider == "polza"
        else MockAIProvider()
    )
    collector = FLRSSCollector(settings.fl_rss_urls)
    service = LiveCanaryService(
        repository,
        HardFilter(FilterSettings(settings.min_budget_rub)),
        ai,
        scoring,
        TelegramClient(settings.telegram_bot_token, settings.telegram_allowed_chat_id),
        settings.telegram_score_threshold,
    )
    return CanaryRuntime(repository, collector, service)


def prepare_feed_onboarding(runtime: CanaryRuntime, raw_jobs: list[RawJob]) -> None:
    if runtime.repository.list_all():
        runtime.service.register_existing_feed(
            OFFICIAL_FL_RSS_NAMES[OFFICIAL_FL_RSS_URL],
            OFFICIAL_FL_RSS_URL,
        )
    runtime.service.onboard_new_feeds(raw_jobs, runtime.collector.last_feed_stats)


async def execute_canary(
    runtime: CanaryRuntime,
    max_new_jobs: int,
    verify_dedupe: bool,
) -> CanaryExecution:
    raw_jobs = await runtime.collector.collect()
    prepare_feed_onboarding(runtime, raw_jobs)
    selection = select_fixed_candidates(raw_jobs, runtime.repository, max_new_jobs)
    fixed_batch = list(selection.selected)
    errors_before = len(runtime.repository.list_processing_errors())
    run1 = await runtime.service.process_many(fixed_batch)
    run2 = await runtime.service.process_many(fixed_batch) if verify_dedupe and fixed_batch else None
    errors_after = len(runtime.repository.list_processing_errors())
    jobs: list[Job] = []
    for raw in fixed_batch:
        normalized = normalize(raw)
        saved = runtime.repository.get_by_dedupe(normalized.source, dedupe_key_for(normalized))
        if saved is not None:
            jobs.append(saved)
    return CanaryExecution(
        fetched_items=len(raw_jobs),
        selection=selection,
        run1=run1,
        run2=run2,
        processing_errors_added=errors_after - errors_before,
        jobs=tuple(jobs),
    )


def database_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("One-shot canary requires a SQLite DATABASE_URL")
    path = Path(database_url.removeprefix(prefix))
    return path if path.is_absolute() else Path.cwd() / path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def current_commit() -> str:
    candidates = [
        shutil.which("git"),
        str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "cmd" / "git.exe"),
    ]
    for executable in candidates:
        if not executable or not Path(executable).exists():
            continue
        try:
            return subprocess.run(
                [executable, "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return "unavailable"


def print_preflight(settings: Settings, max_new_jobs: int, verify_dedupe: bool, confirmed: bool) -> None:
    db_path = database_path(settings.database_url).resolve()
    env_path = (Path.cwd() / ".env").resolve()
    print(f"Current commit: {current_commit()}")
    print(f"DB path: {db_path}")
    print(f"DB SHA-256 before: {sha256(db_path)}")
    print(f".env timestamp: {env_path.stat().st_mtime_ns if env_path.exists() else 'missing'}")
    print(f"Max new jobs: {max_new_jobs}")
    print(f"Verify dedupe: {'yes' if verify_dedupe else 'no'}")
    print(f"RSS feeds count: {len(settings.fl_rss_urls)}")
    print(f"Polza: {'configured' if settings.polza_api_key else 'missing'}")
    print(f"Telegram: {'configured' if settings.telegram_bot_token and settings.telegram_allowed_chat_id else 'missing'}")
    print(f"Telegram destination explicitly confirmed: {'yes' if confirmed else 'no'}")


def print_execution(execution: CanaryExecution, runtime: CanaryRuntime) -> None:
    selection, run1 = execution.selection, execution.run1
    enriched = sum(job.analysis_analyzer_version == ANALYZER_VERSION for job in execution.jobs)
    fallback = len(execution.jobs) - enriched
    print("Run 1:")
    print(f"  fetched RSS items: {execution.fetched_items}")
    print(f"  historical suppressed: {selection.historical_suppressed}")
    print(f"  batch duplicates: {selection.batch_duplicates}")
    print(f"  persistent duplicates: {selection.persistent_duplicates}")
    print(f"  selected fixed candidates: {len(selection.selected)}")
    print(f"  new unique processed: {run1.new_jobs}")
    print(f"  enriched: {enriched}")
    print(f"  fallback: {fallback}")
    print(f"  Telegram sent: {run1.telegram_sent}")
    print(f"  Telegram failed: {run1.telegram_failed}")
    print(f"  processing errors added: {execution.processing_errors_added}")
    for job in execution.jobs:
        filter_result = runtime.service.hard_filter.evaluate(job)
        print(f"Job {job.id}:")
        print(f"  source: {job.source}")
        print(f"  title: {job.title}")
        print(f"  source URL: {job.url or 'missing'}")
        print(f"  source budget: {_budget(job)}")
        print(f"  hard filter: {'accepted' if filter_result.accepted else 'rejected'}")
        print(f"  analytical status: {job.status.value}")
        print(f"  score: {job.final_score if job.final_score is not None else 'missing'}")
        print(f"  card: {'enriched' if job.analysis_analyzer_version == ANALYZER_VERSION else 'fallback'}")
        print(f"  notification status: {runtime.repository.notification_status(job.id, ANALYZER_VERSION) or 'missing'}")
    if execution.run2 is not None:
        print("Run 2:")
        print(f"  persistent duplicates: {execution.run2.duplicates}")
        print(f"  new unique processed: {execution.run2.new_jobs}")
        print(f"  second Telegram attempts: {execution.run2.telegram_sent + execution.run2.telegram_failed}")


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: Callable[[Settings], CanaryRuntime] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.confirm_telegram_destination:
        print("Telegram destination: configured")
        print("Explicit confirmation: required")
        parser.error("--confirm-telegram-destination is required before network access")
    settings = Settings.from_env()
    if not settings.telegram_bot_token or not settings.telegram_allowed_chat_id:
        parser.error("Telegram must be configured before a live canary")
    print_preflight(settings, args.max_new_jobs, args.verify_dedupe, True)
    runtime = (runtime_factory or build_runtime)(settings)
    execution = asyncio.run(execute_canary(runtime, args.max_new_jobs, args.verify_dedupe))
    print_execution(execution, runtime)
    db_path = database_path(settings.database_url).resolve()
    env_path = (Path.cwd() / ".env").resolve()
    print(f"DB SHA-256 after: {sha256(db_path)}")
    print(f".env timestamp after: {env_path.stat().st_mtime_ns if env_path.exists() else 'missing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
