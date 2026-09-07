"""Finish the frozen analyzer_v1 dataset without polling or draft generation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.ai.polza import PolzaAIProvider
from app.calibrate_polza import CALIBRATION_VERSION, calibration_jobs
from app.config import Settings
from app.db import JobRepository, dedupe_key_for
from app.scoring import ScoringEngine
from app.services.polza_evaluation import (
    ANALYZER_VERSION,
    SOURCE_FACT_FIELDS,
    ControlledPolzaEvaluation,
)

EXPECTED_JOB_COUNT = 19
FINAL_CALIBRATION_COUNT = 8


def _tag_final_calibration_jobs(repository: JobRepository, jobs):
    selected = calibration_jobs(jobs)
    if len(selected) != FINAL_CALIBRATION_COUNT:
        raise ValueError(f"Expected {FINAL_CALIBRATION_COUNT} final calibration jobs")
    for job in selected:
        if job.analysis_prompt_version != CALIBRATION_VERSION:
            raise ValueError(f"Final calibration result is missing for job {job.id}")
        if job.analysis_model != "qwen/qwen3.8-flash" or job.analysis_telemetry is None:
            raise ValueError(f"Final calibration metadata is incomplete for job {job.id}")
        if job.analysis_analyzer_version not in (None, ANALYZER_VERSION):
            raise ValueError(f"Unexpected analyzer version for job {job.id}")
        if job.analysis_analyzer_version is None:
            source_before = job.model_dump(include=SOURCE_FACT_FIELDS, mode="json")
            job.analysis_analyzer_version = ANALYZER_VERSION
            job.analysis_telemetry = job.analysis_telemetry.model_copy(
                update={"analyzer_version": ANALYZER_VERSION}
            )
            if source_before != job.model_dump(include=SOURCE_FACT_FIELDS, mode="json"):
                raise RuntimeError("Version tagging changed source facts")
            repository.save(job, dedupe_key_for(job))
    return selected


def pending_analyzer_v1_jobs(repository: JobRepository):
    jobs = repository.list_all()
    if len(jobs) != EXPECTED_JOB_COUNT:
        raise ValueError(f"analyzer_v1 requires exactly {EXPECTED_JOB_COUNT} stored jobs, found {len(jobs)}")
    final_calibration = _tag_final_calibration_jobs(repository, jobs)
    final_ids = {job.id for job in final_calibration}
    refreshed = repository.list_all()
    remaining_scope = [job for job in refreshed if job.id not in final_ids]
    if len(remaining_scope) != EXPECTED_JOB_COUNT - FINAL_CALIBRATION_COUNT:
        raise ValueError("The remaining analyzer_v1 scope is not exactly 11 jobs")
    return [job for job in remaining_scope if job.analysis_analyzer_version != ANALYZER_VERSION]


async def main(max_jobs: int | None, verify_model: bool) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.from_env()
    if settings.ai_provider != "polza":
        raise ValueError("Set AI_PROVIDER=polza explicitly before analyzer_v1 evaluation")
    if settings.polza_model != "qwen/qwen3.8-flash":
        raise ValueError("analyzer_v1 is frozen to qwen/qwen3.8-flash")

    repository = JobRepository(settings.database_url)
    repository.initialize()
    pending = pending_analyzer_v1_jobs(repository)
    if not pending:
        raise ValueError("All 19 jobs already have analyzer_v1; refusing paid reanalysis")
    selected = pending[:max_jobs] if max_jobs is not None else pending

    provider = PolzaAIProvider(settings.polza_api_key, settings.polza_model, settings.polza_base_url)
    if verify_model:
        await provider.verify_model_available()
    result = await ControlledPolzaEvaluation(
        repository,
        provider,
        ScoringEngine(),
        settings.min_notification_score,
        analysis_prompt_version=CALIBRATION_VERSION,
        analyzer_version=ANALYZER_VERSION,
        preserve_previous_analysis=True,
    ).run(selected)
    print(json.dumps({
        "analyzed": result.analyzed,
        "failed": len(result.failures),
        "failures": [{"job_id": item.job_id, "title": item.title, "error": item.error} for item in result.failures],
        "repair_retries": result.repair_retries,
        "remaining_after_run": len(pending) - result.analyzed,
        "model": provider.model,
        "analyzer_version": ANALYZER_VERSION,
        "prompt_tokens": sum(item.prompt_tokens or 0 for item in result.usages),
        "completion_tokens": sum(item.completion_tokens or 0 for item in result.usages),
        "reasoning_tokens": sum(item.reasoning_tokens or 0 for item in result.usages),
        "total_tokens": sum(item.total_tokens or 0 for item in result.usages),
        "cost_rub": sum(item.cost_rub or 0 for item in result.usages),
        "completion_requests": sum(
            job.analysis_telemetry.completion_request_count
            for job in result.jobs if job.analysis_telemetry
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze only the 11 remaining stored jobs with frozen analyzer_v1.")
    parser.add_argument("--max-jobs", type=int, help="Analyze at most this many pending jobs.")
    parser.add_argument("--verify-model", action="store_true", help="Run the authenticated read-only model check first.")
    args = parser.parse_args()
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    asyncio.run(main(args.max_jobs, args.verify_model))
