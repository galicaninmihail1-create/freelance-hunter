"""Explicit, one-job Polza smoke check. It never writes to SQLite or starts a batch."""

from __future__ import annotations

import asyncio
import argparse
import json

from app.ai.polza import AIProviderError, PolzaAIProvider
from app.config import Settings
from app.db import JobRepository
from app.scoring import ScoringEngine


SOURCE_FACT_FIELDS = {
    "source", "source_job_id", "url", "title", "description", "published_at",
    "budget_min", "budget_max", "currency", "source_budget_status", "category", "skills",
}


async def main(catalog_only: bool = False) -> None:
    settings = Settings.from_env()
    if settings.ai_provider != "polza":
        raise ValueError("AI_PROVIDER must be polza for the explicit smoke check")
    repository = JobRepository(settings.database_url)
    repository.initialize()
    jobs = repository.list_all()
    if not jobs:
        raise ValueError("No saved job is available for the smoke check")
    provider = PolzaAIProvider(
        settings.polza_api_key, settings.polza_model, settings.polza_base_url, analysis_max_tokens=600
    )
    result: dict[str, object] = {
        "configuration": {
            "ai_provider": settings.ai_provider,
            "polza_base_url": settings.polza_base_url,
            "polza_model": settings.polza_model,
            "polza_api_key_present": bool(settings.polza_api_key),
        }
    }
    try:
        catalog_item = await provider.verify_model_available()
        result["catalog"] = {
            "available": True,
            "http_status": provider.last_catalog_status,
            "diagnostic": provider.last_diagnostic.as_dict() if provider.last_diagnostic else None,
            "pricing": (catalog_item.get("top_provider") or {}).get("pricing"),
            "supported_parameters": (catalog_item.get("top_provider") or {}).get("supported_parameters"),
        }
    except AIProviderError as exc:
        result["catalog"] = {
            "available": False,
            "diagnostic": exc.diagnostic.as_dict() if exc.diagnostic else None,
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    if catalog_only:
        print(json.dumps(result, ensure_ascii=False))
        return

    # This is the only completion in this command. No persistence occurs before or after it.
    job = jobs[0]
    source_facts_before = job.model_dump(include=SOURCE_FACT_FIELDS, mode="json")
    try:
        analysis = await provider.analyze_job(job)
        scored_job = job.model_copy(deep=True)
        ScoringEngine().analyze(scored_job, analysis)
        usage = provider.last_usage
        structured = provider.last_structured_analysis
        source_facts_after = job.model_dump(include=SOURCE_FACT_FIELDS, mode="json")
        result["completion"] = {
            "success": True,
            "completion_request_count": provider.completion_request_count,
            "provider": provider.provider_name,
            "model": provider.model,
            "http_status": provider.last_http_status,
            "diagnostic": provider.last_diagnostic.as_dict() if provider.last_diagnostic else None,
            "usage": usage.__dict__ if usage else None,
            "source_facts_unchanged": source_facts_before == source_facts_after,
            "sqlite_write_performed": False,
            "response_draft_generated": False,
            "job": {
                "title": job.title,
                "source_budget_status": job.source_budget_status,
                "client_summary": structured.client_summary if structured else None,
                "complexity": structured.complexity_score if structured else None,
                "codex_share": structured.codex_share if structured else None,
                "estimated_total_hours": [structured.estimated_total_hours_min, structured.estimated_total_hours_max] if structured else None,
                "estimated_owner_hours": [structured.estimated_owner_hours_min, structured.estimated_owner_hours_max] if structured else None,
                "technical_risk": structured.technical_risk if structured else None,
                "commercial_risk": structured.commercial_risk if structured else None,
                "fit": structured.fit_score if structured else None,
                "productization": structured.productization_score if structured else None,
                "confidence": structured.confidence if structured else None,
                "provisional_score": scored_job.provisional_score,
                "final_score": scored_job.final_score,
                "score_is_provisional": scored_job.score_is_provisional,
            },
        }
    except AIProviderError as exc:
        result["completion"] = {
            "success": False,
            "completion_request_count": provider.completion_request_count,
            "provider": provider.provider_name,
            "model": provider.model,
            "http_status": provider.last_http_status,
            "diagnostic": exc.diagnostic.as_dict() if exc.diagnostic else None,
            "usage": provider.last_usage.__dict__ if provider.last_usage else None,
            "source_facts_unchanged": source_facts_before == job.model_dump(include=SOURCE_FACT_FIELDS, mode="json"),
            "sqlite_write_performed": False,
            "response_draft_generated": False,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a safe Polza diagnostics check without SQLite writes.")
    parser.add_argument("--catalog-only", action="store_true", help="Call only the documented GET /models endpoint.")
    args = parser.parse_args()
    asyncio.run(main(catalog_only=args.catalog_only))
