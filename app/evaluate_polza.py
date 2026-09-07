"""Explicit, one-off analysis of exactly 19 already-saved FL.ru jobs."""

from __future__ import annotations

import asyncio
import argparse
import json
import statistics
import sys
from collections import defaultdict
from typing import Callable

from app.ai.polza import PolzaAIProvider
from app.config import Settings
from app.db import JobRepository
from app.models import BudgetStatus, Job
from app.scoring import ScoringEngine
from app.services.polza_evaluation import ControlledPolzaEvaluation


def _midpoint(low: float | None, high: float | None, fallback: float | None) -> float | None:
    if low is not None and high is not None:
        return (low + high) / 2
    return fallback


def _distribution(jobs: list[Job], value: Callable[[Job], float | None]) -> dict[str, float | int | None]:
    values = [item for job in jobs if (item := value(job)) is not None]
    if not values:
        return {"min": None, "median": None, "max": None, "unique_values": 0}
    return {
        "min": min(values), "median": statistics.median(values), "max": max(values),
        "unique_values": len(set(values)),
    }


def _source_budget(job: Job) -> str:
    if job.source_budget_status is BudgetStatus.UNKNOWN:
        return "unknown"
    amount = job.budget_max if job.budget_max is not None else job.budget_min
    return f"{amount:g} {job.currency or ''}".strip() if amount is not None else job.source_budget_status.value.lower()


def _row(job: Job, successful_ids: set[str]) -> dict[str, object]:
    successful = job.id in successful_ids
    return {
        "id": job.id,
        "title": job.title,
        "successful": successful,
        "source_budget": _source_budget(job),
        "complexity": job.complexity_score if successful else None,
        "codex_share": job.codex_share if successful else None,
        "owner_hours": [job.estimated_owner_hours_min, job.estimated_owner_hours_max] if successful else None,
        "total_hours": [job.estimated_total_hours_min, job.estimated_total_hours_max] if successful else None,
        "requirement_clarity": job.analysis_requirement_clarity_score if successful else None,
        "technical_risk": job.technical_risk_score if successful else None,
        "commercial_risk": job.commercial_risk_score if successful else None,
        "fit": job.analysis_fit_score if successful else None,
        "win_probability": job.analysis_win_probability_score if successful else None,
        "productization": job.analysis_productization_score if successful else None,
        "confidence": job.analysis_confidence if successful else None,
        "problem_category": job.problem_category if successful else None,
        "score": job.final_score if successful else None,
        "score_is_provisional": job.score_is_provisional if successful else None,
    }


def _diagnostic_flags(jobs: list[Job]) -> list[dict[str, object]]:
    flags: list[dict[str, object]] = []
    repeated: dict[tuple[float | None, ...], list[Job]] = defaultdict(list)
    for job in jobs:
        repeated[(job.complexity_score, job.codex_share,
                  _midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max, job.estimated_owner_hours),
                  _midpoint(job.estimated_total_hours_min, job.estimated_total_hours_max, job.estimated_total_hours),
                  job.analysis_productization_score)].append(job)
    for values, matching in repeated.items():
        if len(matching) >= 3:
            flags.append({"kind": "template_like_estimates", "job_ids": [job.id for job in matching], "values": values})

    for job in jobs:
        text = f"{job.title} {job.description}".lower()
        integration = any(token in text for token in ("api", "интеграц", "бот", "автоматиз", "bitrix", "1с"))
        large = any(token in text for token in ("erp", "crm", "платформ", "система с нуля", "маркетплейс"))
        owner_midpoint = _midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max, job.estimated_owner_hours)
        if integration and not large and (job.codex_share or 0) < 20:
            flags.append({"kind": "low_codex_for_integration", "job_id": job.id})
        if large and (job.codex_share or 0) > 90:
            flags.append({"kind": "high_codex_for_large_project", "job_id": job.id})
        if large and owner_midpoint is not None and owner_midpoint < 2:
            flags.append({"kind": "low_owner_hours_for_large_project", "job_id": job.id})
        if integration and (job.analysis_productization_score or 0) <= 3:
            flags.append({"kind": "low_productization_for_integration", "job_id": job.id})
        score = job.final_score or 0
        if score >= 85 and ((job.codex_share or 0) < 20 or (job.analysis_productization_score or 0) <= 3):
            flags.append({"kind": "high_score_with_weak_factors", "job_id": job.id})
        if score <= 25 and (job.codex_share or 0) >= 60 and (job.analysis_productization_score or 0) >= 7:
            flags.append({"kind": "low_score_with_strong_factors", "job_id": job.id})
    return flags


async def main(max_jobs: int | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.from_env()
    if settings.ai_provider != "polza":
        raise ValueError("Set AI_PROVIDER=polza explicitly before a paid controlled evaluation")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    jobs = repository.list_all()
    if len(jobs) != 19:
        raise ValueError(f"Controlled evaluation requires exactly the 19 stored FL.ru jobs, found {len(jobs)}")
    provider = PolzaAIProvider(settings.polza_api_key, settings.polza_model, settings.polza_base_url)
    model_info = await provider.verify_model_available()
    pending_jobs = [job for job in jobs if job.analysis_provider != provider.provider_name]
    if not pending_jobs:
        raise ValueError("All 19 stored jobs already have a Polza analysis; refusing to repeat any job")
    if max_jobs is not None:
        pending_jobs = pending_jobs[:max_jobs]
    result = await ControlledPolzaEvaluation(repository, provider, ScoringEngine(), settings.min_notification_score).run(pending_jobs)
    all_jobs = repository.list_all()
    successful_ids = {job.id for job in result.jobs}
    successful = [job for job in all_jobs if job.id in successful_ids]
    rows = [_row(job, successful_ids) for job in all_jobs]
    rows.sort(key=lambda row: (not bool(row["successful"]), -(float(row["score"]) if row["score"] is not None else -1)))
    print(json.dumps({
        "batch": {
            "analyzed": result.analyzed,
            "remaining": len(jobs) - sum(job.analysis_provider == provider.provider_name for job in all_jobs),
            "failed": len(result.failures),
            "repair_retries": result.repair_retries,
            "response_drafts_generated": result.response_drafts_generated,
            "model": provider.model,
            "failures": [{"title": item.title, "error": item.error} for item in result.failures],
        },
        "usage": {
            "prompt_tokens": sum(item.prompt_tokens or 0 for item in result.usages),
            "completion_tokens": sum(item.completion_tokens or 0 for item in result.usages),
            "reasoning_tokens": sum(item.reasoning_tokens or 0 for item in result.usages),
            "total_tokens": sum(item.total_tokens or 0 for item in result.usages),
            "cost_rub": sum(item.cost_rub or 0 for item in result.usages),
        },
        "catalog_pricing": (model_info.get("top_provider") or {}).get("pricing"),
        "dispersion": {
            "complexity": _distribution(successful, lambda job: job.complexity_score),
            "codex_share": _distribution(successful, lambda job: job.codex_share),
            "owner_hours_midpoint": _distribution(successful, lambda job: _midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max, job.estimated_owner_hours)),
            "total_hours_midpoint": _distribution(successful, lambda job: _midpoint(job.estimated_total_hours_min, job.estimated_total_hours_max, job.estimated_total_hours)),
            "productization": _distribution(successful, lambda job: job.analysis_productization_score),
            "confidence": _distribution(successful, lambda job: job.analysis_confidence),
            "score": _distribution(successful, lambda job: job.final_score),
        },
        "jobs": rows,
        "diagnostic_flags": _diagnostic_flags(successful),
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an explicit, bounded Polza analysis of pending saved jobs.")
    parser.add_argument("--max-jobs", type=int, help="Analyze at most this many still-pending jobs; never repeats persisted analyses.")
    args = parser.parse_args()
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    asyncio.run(main(max_jobs=args.max_jobs))
