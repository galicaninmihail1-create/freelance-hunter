"""Bounded re-analysis of a diagnostic subset; it never fetches RSS or drafts replies."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.ai.polza import PolzaAIProvider
from app.analysis_version import ANALYSIS_PROMPT_VERSION
from app.config import Settings
from app.db import JobRepository
from app.scoring import ScoringEngine
from app.services.polza_evaluation import ControlledPolzaEvaluation

CALIBRATION_VERSION = ANALYSIS_PROMPT_VERSION
CALIBRATION_TITLE_MARKERS = (
    "настроить интеграцию срм и телеграма",
    "интеграция 1сfresh и битрикс 24",
    "разработка telegram-бота с crm",
    "передача лидов salebot",
    "автоматизация обработки документов с распознаванием",
    "crm/erp для дистрибьютора",
    "optimacros",
    "перенести корп почту с gmail",
)


def calibration_jobs(jobs):
    selected = []
    for marker in CALIBRATION_TITLE_MARKERS:
        matches = [job for job in jobs if marker in job.title.lower()]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one saved calibration job for marker {marker!r}, found {len(matches)}")
        selected.append(matches[0])
    return selected


async def main(max_jobs: int | None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.from_env()
    if settings.ai_provider != "polza":
        raise ValueError("Set AI_PROVIDER=polza explicitly before calibration")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    all_jobs = repository.list_all()
    if len(all_jobs) != 19:
        raise ValueError(f"Calibration requires exactly 19 stored source jobs, found {len(all_jobs)}")
    selected = calibration_jobs(all_jobs)
    pending = [job for job in selected if job.analysis_prompt_version != CALIBRATION_VERSION]
    if not pending:
        raise ValueError("Calibration subset already has the final decomposition version; refusing to repeat paid analyses")
    if max_jobs is not None:
        pending = pending[:max_jobs]
    provider = PolzaAIProvider(settings.polza_api_key, settings.polza_model, settings.polza_base_url)
    await provider.verify_model_available()
    result = await ControlledPolzaEvaluation(
        repository, provider, ScoringEngine(), settings.min_notification_score,
        analysis_prompt_version=CALIBRATION_VERSION, preserve_previous_analysis=True,
    ).run(pending)
    print(json.dumps({
        "analyzed": result.analyzed,
        "failed": len(result.failures),
        "repair_retries": result.repair_retries,
        "remaining": len(pending) - result.analyzed,
        "response_drafts_generated": result.response_drafts_generated,
        "model": provider.model,
        "tokens": sum(item.total_tokens or 0 for item in result.usages),
        "cost_rub": sum(item.cost_rub or 0 for item in result.usages),
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a bounded, versioned Polza calibration subset.")
    parser.add_argument("--max-jobs", type=int, help="Analyze at most this many pending calibration jobs.")
    args = parser.parse_args()
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    asyncio.run(main(args.max_jobs))
