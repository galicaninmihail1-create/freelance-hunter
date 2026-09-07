"""Read-only old/new comparison for the completed calibration subset."""

from __future__ import annotations

import json
import sys

from app.calibrate_polza import CALIBRATION_VERSION, calibration_jobs
from app.config import Settings
from app.db import JobRepository


def _midpoint(low, high):
    return (low + high) / 2 if low is not None and high is not None else None


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    repository = JobRepository(Settings.from_env().database_url)
    repository.initialize()
    selected = calibration_jobs(repository.list_all())
    if any(job.analysis_prompt_version != CALIBRATION_VERSION or not job.analysis_history for job in selected):
        raise ValueError("Calibration report requires all eight versioned snapshots")
    rows = []
    flags = []
    for job in selected:
        old = job.analysis_history[-1]
        telemetry = job.analysis_telemetry
        row = {
            "title": job.title,
            "old_codex_share": old.codex_share,
            "new_codex_share": job.codex_share,
            "codex_share_reason": job.analysis_codex_share_reason,
            "old_owner_hours": [old.owner_hours_min, old.owner_hours_max],
            "new_owner_hours": [job.estimated_owner_hours_min, job.estimated_owner_hours_max],
            "owner_decomposition": {
                "communication": job.owner_communication_hours,
                "access_setup": job.owner_access_setup_hours,
                "review_testing": job.owner_review_testing_hours,
                "manual_execution": job.owner_manual_execution_hours,
            },
            "old_productization": old.productization,
            "new_productization": job.analysis_productization_score,
            "old_confidence": old.confidence,
            "new_confidence": job.analysis_confidence,
            "old_complexity": old.complexity,
            "new_complexity": job.complexity_score,
            "old_score": old.final_score,
            "new_score": job.final_score,
            "telemetry": telemetry.model_dump(mode="json") if telemetry else None,
        }
        rows.append(row)
        title = job.title.lower()
        integration = any(item in title for item in ("интеграц", "telegram", "salebot", "bitrix", "crm", "1с"))
        external_access = integration
        owner_midpoint = _midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max)
        if integration and (job.analysis_productization_score or 0) <= 3:
            flags.append({"kind": "low_productization_for_common_automation", "title": job.title})
        if external_access and owner_midpoint is not None and owner_midpoint < 1:
            flags.append({"kind": "owner_hours_below_one_with_external_access", "title": job.title})
        if integration and (job.codex_share or 0) < 30:
            flags.append({"kind": "low_codex_share_for_ordinary_integration", "title": job.title})
        if "crm/erp" in title and (job.codex_share or 0) > 90:
            flags.append({"kind": "high_codex_share_for_broad_erp", "title": job.title})
        if integration and owner_midpoint is not None and owner_midpoint > 20:
            flags.append({"kind": "owner_hours_above_twenty_for_scoped_integration", "title": job.title})
    telemetries = [job.analysis_telemetry for job in selected if job.analysis_telemetry]
    print(json.dumps({
        "jobs": len(selected),
        "tokens": {
            "prompt": sum(item.prompt_tokens or 0 for item in telemetries),
            "completion": sum(item.completion_tokens or 0 for item in telemetries),
            "reasoning": sum(item.reasoning_tokens or 0 for item in telemetries),
            "total": sum(item.total_tokens or 0 for item in telemetries),
            "cost_rub": sum(item.cost_rub or 0 for item in telemetries),
            "completion_requests": sum(item.completion_request_count for item in telemetries),
            "repair_retries": sum(item.repair_retry_count for item in telemetries),
        },
        "rows": rows,
        "flags": flags,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
