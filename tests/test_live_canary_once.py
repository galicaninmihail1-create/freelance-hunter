from pathlib import Path

import pytest

from app.ai import MockAIProvider
from app.db import dedupe_key_for
from app.filters import HardFilter
from app.live_canary_once import (
    CanaryRuntime,
    execute_canary,
    main,
    print_preflight,
    select_fixed_candidates,
)
from app.models import Job, RawJob
from app.normalizer import normalize
from app.scoring import ScoringEngine
from app.services.live_canary import LiveCanaryService


def raw_job(source_id: str) -> RawJob:
    return RawJob(
        source="fl.ru", source_job_id=source_id,
        title=f"API integration {source_id}", description="Python API automation",
        url=f"https://example.test/{source_id}",
    )


@pytest.mark.parametrize("value", ["0", "-1", "4"])
def test_cli_rejects_out_of_range_limit_before_runtime(value: str) -> None:
    with pytest.raises(SystemExit):
        main(["--max-new-jobs", value, "--confirm-telegram-destination"], runtime_factory=lambda _: pytest.fail("runtime created"))


def test_cli_requires_limit_before_runtime() -> None:
    with pytest.raises(SystemExit):
        main(["--confirm-telegram-destination"], runtime_factory=lambda _: pytest.fail("runtime created"))


def test_cli_requires_destination_confirmation_before_runtime(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--max-new-jobs", "1"], runtime_factory=lambda _: pytest.fail("runtime created"))
    output = capsys.readouterr().out
    assert output.splitlines() == [
        "Telegram destination: configured",
        "Explicit confirmation: required",
    ]


def test_cli_module_has_no_fastapi_or_scheduler_dependency() -> None:
    source = Path(__file__).parents[1].joinpath("app", "live_canary_once.py").read_text(encoding="utf-8")
    assert "app.main" not in source
    assert "NonOverlappingScheduler" not in source
    assert "uvicorn" not in source.lower()


def test_selection_honors_limits_and_stable_feed_order(repository) -> None:
    jobs = [raw_job(str(index)) for index in range(5)]
    one = select_fixed_candidates(jobs, repository, 1)
    three = select_fixed_candidates(jobs, repository, 3)
    repeated = select_fixed_candidates(jobs, repository, 3)
    assert [item.source_job_id for item in one.selected] == ["0"]
    assert [item.source_job_id for item in three.selected] == ["0", "1", "2"]
    assert three.selected == repeated.selected


def test_selection_ignores_score_and_excludes_historical_persistent_and_batch_duplicates(repository) -> None:
    historical = raw_job("historical")
    historical_job = normalize(historical)
    repository.onboard_feed(
        "fl.ru", "Python", "https://www.fl.ru/rss/python",
        {dedupe_key_for(historical_job)},
    )
    persisted_raw = raw_job("persisted")
    persisted = normalize(persisted_raw)
    persisted.final_score = 100
    repository.save(persisted, dedupe_key_for(persisted))
    first = raw_job("first")
    duplicate = first.model_copy(deep=True)
    duplicate.metadata = {"feed_url": "https://www.fl.ru/rss/other"}
    later = raw_job("later")

    selection = select_fixed_candidates(
        [historical, persisted_raw, first, duplicate, later], repository, 3,
    )

    assert [item.source_job_id for item in selection.selected] == ["first", "later"]
    assert selection.historical_suppressed == 1
    assert selection.persistent_duplicates == 1
    assert selection.batch_duplicates == 1


class FakeCollector:
    def __init__(self, jobs: list[RawJob]):
        self.jobs = jobs
        self.last_feed_stats = []
        self.calls = 0

    async def collect(self) -> list[RawJob]:
        self.calls += 1
        return self.jobs


class FailingTelegram:
    def __init__(self):
        self.attempts = 0

    async def notify(self, job: Job, scoring: ScoringEngine, *, fallback: bool = False) -> bool:
        self.attempts += 1
        return False


async def test_second_pass_reuses_fixed_batch_and_does_not_advance_after_failed_attempt(repository) -> None:
    collector = FakeCollector([raw_job("first"), raw_job("second"), raw_job("third")])
    telegram = FailingTelegram()
    service = LiveCanaryService(
        repository, HardFilter(), MockAIProvider(), ScoringEngine(), telegram, 50,
    )
    runtime = CanaryRuntime(repository, collector, service)

    execution = await execute_canary(runtime, max_new_jobs=1, verify_dedupe=True)

    assert collector.calls == 1
    assert [item.source_job_id for item in execution.selection.selected] == ["first"]
    assert execution.run1.telegram_failed == 1
    assert execution.run2 is not None and execution.run2.duplicates == 1
    assert execution.run2.telegram_sent + execution.run2.telegram_failed == 0
    assert telegram.attempts == 1
    assert len(repository.list_all()) == 1


async def test_empty_candidates_make_no_ai_or_telegram_attempt(repository) -> None:
    persisted = normalize(raw_job("persisted"))
    repository.save(persisted, dedupe_key_for(persisted))
    collector = FakeCollector([raw_job("persisted")])
    telegram = FailingTelegram()
    service = LiveCanaryService(
        repository, HardFilter(), MockAIProvider(), ScoringEngine(), telegram, 50,
    )

    execution = await execute_canary(CanaryRuntime(repository, collector, service), 1, True)

    assert execution.selection.selected == ()
    assert execution.run1.ai_requested == 0
    assert telegram.attempts == 0
    assert execution.run2 is None


def test_preflight_does_not_print_credentials(tmp_path: Path, settings, capsys) -> None:
    db_path = tmp_path / "canary.db"
    db_path.write_bytes(b"safe test database placeholder")
    safe_settings = settings.__class__(
        database_url=f"sqlite:///{db_path}",
        polza_api_key="POLZA-SHOULD-NOT-PRINT",
        telegram_bot_token="TELEGRAM-SHOULD-NOT-PRINT",
        telegram_allowed_chat_id="CHAT-SHOULD-NOT-PRINT",
        fl_rss_urls=("https://www.fl.ru/rss/test",),
    )

    print_preflight(safe_settings, 1, True, True)

    output = capsys.readouterr().out
    assert "POLZA-SHOULD-NOT-PRINT" not in output
    assert "TELEGRAM-SHOULD-NOT-PRINT" not in output
    assert "CHAT-SHOULD-NOT-PRINT" not in output
    assert "Polza: configured" in output
    assert "Telegram: configured" in output
