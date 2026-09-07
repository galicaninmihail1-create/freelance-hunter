"""Read-only report for a completed controlled Polza batch."""

from __future__ import annotations

import json
import sys

from app.config import Settings
from app.db import JobRepository
from app.evaluate_polza import _diagnostic_flags, _distribution, _midpoint, _row


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.from_env()
    repository = JobRepository(settings.database_url)
    repository.initialize()
    jobs = repository.list_all()
    analyzed = [job for job in jobs if job.analysis_provider == "polza"]
    rows = [_row(job, {item.id for item in analyzed}) for job in jobs]
    rows.sort(key=lambda row: -(float(row["score"]) if row["score"] is not None else -1))
    print(json.dumps({
        "jobs": len(jobs),
        "analyzed": len(analyzed),
        "usage": {
            "prompt_tokens": sum(job.analysis_prompt_tokens or 0 for job in analyzed),
            "completion_tokens": sum(job.analysis_completion_tokens or 0 for job in analyzed),
            "total_tokens": sum(job.analysis_total_tokens or 0 for job in analyzed),
        },
        "dispersion": {
            "complexity": _distribution(analyzed, lambda job: job.complexity_score),
            "codex_share": _distribution(analyzed, lambda job: job.codex_share),
            "owner_hours_midpoint": _distribution(analyzed, lambda job: _midpoint(job.estimated_owner_hours_min, job.estimated_owner_hours_max, job.estimated_owner_hours)),
            "total_hours_midpoint": _distribution(analyzed, lambda job: _midpoint(job.estimated_total_hours_min, job.estimated_total_hours_max, job.estimated_total_hours)),
            "productization": _distribution(analyzed, lambda job: job.analysis_productization_score),
            "confidence": _distribution(analyzed, lambda job: job.analysis_confidence),
            "score": _distribution(analyzed, lambda job: job.final_score),
        },
        "rows": rows,
        "diagnostic_flags": _diagnostic_flags(analyzed),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
