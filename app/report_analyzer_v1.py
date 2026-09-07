"""Read-only integrity and market report for the unified analyzer_v1 snapshot."""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict

from app.ai.polza import PolzaStructuredAnalysis
from app.calibrate_polza import calibration_jobs
from app.config import Settings
from app.db import JobRepository
from app.models import BudgetStatus, Job
from app.scoring import ScoringEngine
from app.services.polza_evaluation import ANALYZER_VERSION


def midpoint(low: float | None, high: float | None) -> float | None:
    return (low + high) / 2 if low is not None and high is not None else None


def source_budget_value(job: Job) -> float | None:
    if job.source_budget_status is BudgetStatus.UNKNOWN:
        return None
    return job.budget_max if job.budget_max is not None else job.budget_min


def source_budget_label(job: Job) -> str:
    amount = source_budget_value(job)
    if amount is None:
        return "UNKNOWN" if job.source_budget_status is BudgetStatus.UNKNOWN else job.source_budget_status.value
    return f"{amount:g} {job.currency or ''}".strip()


def canonical_category(job: Job) -> str:
    problem = (job.problem_category or "").lower()
    title = job.title.lower()
    value = f"{problem} {title}"
    if "document" in problem or "распозна" in title or "документ" in title or re.search(r"\bocr\b", problem):
        return "Document OCR / extraction"
    if any(token in value for token in ("email", "mail", "gmail", "почт", "infrastructure migration")):
        return "Email / infrastructure migration"
    if any(token in problem for token in ("consult", "training")) or "optimacros" in title:
        return "Planning / consulting"
    if any(token in value for token in ("crm", "amo", "bitrix", "битрикс", "erp", "1c", "1с")):
        if any(token in value for token in ("telegram", "телеграм", "salebot", "lead management", "лид")):
            return "CRM / messaging integration"
        return "CRM / ERP automation"
    if "telegram" in value or re.search(r"\bbot\b", value) or re.search(r"\bбот", value):
        return "Bot automation"
    if any(token in value for token in ("api", "integration", "интеграц", "webhook", "legacy system")):
        return "API / system integration"
    if any(token in value for token in ("calculator", "report", "calculation", "расчет", "расчёт", "отчет", "отчёт")):
        return "Business calculations / reporting"
    if any(token in value for token in ("automation", "автоматизац")):
        return "Business process automation"
    return "Other business software"


def validated_structured(job: Job) -> PolzaStructuredAnalysis:
    return PolzaStructuredAnalysis(
        client_summary=job.analysis_summary,
        deliverable=job.analysis_deliverable,
        required_skills=job.analysis_required_skills,
        complexity_score=job.complexity_score,
        codex_share=job.codex_share,
        codex_share_reason=job.analysis_codex_share_reason,
        ai_executable_work=job.analysis_ai_executable_work,
        owner_required_work=job.analysis_owner_required_work,
        external_dependency_work=job.analysis_external_dependency_work,
        estimated_total_hours_min=job.estimated_total_hours_min,
        estimated_total_hours_max=job.estimated_total_hours_max,
        estimated_owner_hours_min=job.estimated_owner_hours_min,
        estimated_owner_hours_max=job.estimated_owner_hours_max,
        owner_communication_hours=job.owner_communication_hours,
        owner_access_setup_hours=job.owner_access_setup_hours,
        owner_review_testing_hours=job.owner_review_testing_hours,
        owner_manual_execution_hours=job.owner_manual_execution_hours,
        requirement_clarity=job.analysis_requirement_clarity_score,
        technical_risk=job.technical_risk_score,
        commercial_risk=job.commercial_risk_score,
        fit_score=job.analysis_fit_score,
        win_probability_score=job.analysis_win_probability_score,
        productization_score=job.analysis_productization_score,
        problem_category=job.problem_category,
        technical_risks=job.technical_risks,
        commercial_risks=job.commercial_risks,
        pricing_notes=job.analysis_pricing_notes or "",
        confidence=job.analysis_confidence,
    )


def verify_score(job: Job) -> bool:
    copy = job.model_copy(deep=True)
    analysis = validated_structured(job).to_job_analysis()
    ScoringEngine().analyze(copy, analysis)
    return (
        copy.final_score == job.final_score
        and copy.provisional_score == job.provisional_score
        and copy.score_is_provisional == job.score_is_provisional
    )


def row(job: Job) -> dict[str, object]:
    owner_mid = midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max)
    budget = source_budget_value(job)
    efficiency = round(budget / owner_mid, 2) if budget is not None and owner_mid else None
    return {
        "title": job.title,
        "source_budget": source_budget_label(job),
        "codex_share": job.codex_share,
        "owner_hours": [job.estimated_owner_hours_min, job.estimated_owner_hours_max],
        "total_hours": [job.estimated_total_hours_min, job.estimated_total_hours_max],
        "productization": job.analysis_productization_score,
        "confidence": job.analysis_confidence,
        "score": job.final_score,
        "provisional": job.score_is_provisional,
        "owner_hour_efficiency": efficiency,
        "category": canonical_category(job),
    }


def category_rows(jobs: list[Job]) -> list[dict[str, object]]:
    groups: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        groups[canonical_category(job)].append(job)
    result = []
    for category, items in groups.items():
        known_budgets = [value for job in items if (value := source_budget_value(job)) is not None]
        result.append({
            "category": category,
            "jobs": len(items),
            "median_codex_share": statistics.median(job.codex_share for job in items),
            "median_owner_hours": statistics.median(
                midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max) for job in items
            ),
            "median_productization": statistics.median(job.analysis_productization_score for job in items),
            "known_budget_count": len(known_budgets),
            "known_budget_median": statistics.median(known_budgets) if len(known_budgets) >= 2 else None,
        })
    return sorted(result, key=lambda item: (-item["jobs"], item["category"]))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    repository = JobRepository(Settings.from_env().database_url)
    repository.initialize()
    jobs = repository.list_all()
    if len(jobs) != 19:
        raise ValueError(f"Expected 19 jobs, found {len(jobs)}")
    if len({job.id for job in jobs}) != 19:
        raise ValueError("Duplicate current job records found")
    if any(job.analysis_analyzer_version != ANALYZER_VERSION for job in jobs):
        raise ValueError("Not all jobs have the current analyzer_v1 result")

    range_valid = all(validated_structured(job) for job in jobs)
    reproducible = [job.id for job in jobs if verify_score(job)]
    unknown_jobs = [job for job in jobs if job.source_budget_status is BudgetStatus.UNKNOWN]
    if any(job.budget_min is not None or job.budget_max is not None for job in unknown_jobs):
        raise ValueError("UNKNOWN source budget has acquired a numeric value")

    calibration_ids = {job.id for job in calibration_jobs(jobs)}
    run_jobs = [job for job in jobs if job.id not in calibration_ids]
    telemetries = [job.analysis_telemetry for job in run_jobs if job.analysis_telemetry]
    rows = sorted((row(job) for job in jobs), key=lambda item: -item["score"])
    known_efficiency = sorted(
        (item for item in rows if item["owner_hour_efficiency"] is not None),
        key=lambda item: -item["owner_hour_efficiency"],
    )
    print(json.dumps({
        "update": {
            "current": len(jobs),
            "analyzer_version": ANALYZER_VERSION,
            "run_jobs": len(run_jobs),
            "successful": len(telemetries),
            "repair_retries": sum(item.repair_retry_count for item in telemetries),
            "completion_requests": sum(item.completion_request_count for item in telemetries),
        },
        "usage_11": {
            "prompt_tokens": sum(item.prompt_tokens or 0 for item in telemetries),
            "completion_tokens": sum(item.completion_tokens or 0 for item in telemetries),
            "reasoning_tokens": sum(item.reasoning_tokens or 0 for item in telemetries),
            "total_tokens": sum(item.total_tokens or 0 for item in telemetries),
            "cost_rub": round(sum(item.cost_rub or 0 for item in telemetries), 6),
        },
        "ranking": rows,
        "known_budget_efficiency": known_efficiency,
        "categories": category_rows(jobs),
        "integrity": {
            "analyzer_v1": len(jobs),
            "duplicate_current_analyses": len(jobs) - len({job.id for job in jobs}),
            "numeric_ranges_valid": bool(range_valid),
            "reproducible_scores": len(reproducible),
            "unknown_budgets": len(unknown_jobs),
            "unknown_budgets_with_numeric_value": 0,
            "history_snapshots": sum(len(job.analysis_history) for job in jobs),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
