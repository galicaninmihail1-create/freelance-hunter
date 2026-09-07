from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time

from app.ai.polza import AIProviderError, PolzaAIProvider, ProviderUsage
from app.analysis_version import ANALYZER_VERSION
from app.db import JobRepository, dedupe_key_for
from app.models import AnalysisSnapshot, AnalysisTelemetry, Job, JobAnalysis, JobStatus
from app.scoring import ScoringEngine

EVALUATION_ALLOWED_MODELS = {"qwen/qwen3.8-flash"}
SOURCE_FACT_FIELDS = {
    "source", "source_job_id", "url", "title", "description", "published_at",
    "budget_min", "budget_max", "currency", "source_budget_status", "category", "skills",
}


@dataclass(frozen=True)
class EvaluationFailure:
    job_id: str
    title: str
    error: str


@dataclass(frozen=True)
class EvaluationResult:
    analyzed: int
    repair_retries: int
    failures: list[EvaluationFailure]
    usages: list[ProviderUsage]
    jobs: list[Job]
    response_drafts_generated: int = 0


class ControlledPolzaEvaluation:
    """Explicit analysis of saved jobs only; it never polls RSS or generates drafts."""

    def __init__(
        self,
        repository: JobRepository,
        provider: PolzaAIProvider,
        scoring: ScoringEngine,
        notification_threshold: float,
        analysis_prompt_version: str | None = None,
        analyzer_version: str | None = None,
        preserve_previous_analysis: bool = False,
    ):
        if provider.model not in EVALUATION_ALLOWED_MODELS:
            raise ValueError(f"Refusing controlled evaluation with unverified or unexpectedly expensive model: {provider.model}")
        self.repository, self.provider, self.scoring, self.notification_threshold = repository, provider, scoring, notification_threshold
        self.analysis_prompt_version = analysis_prompt_version
        self.analyzer_version = analyzer_version
        self.preserve_previous_analysis = preserve_previous_analysis

    async def run(self, jobs: list[Job]) -> EvaluationResult:
        if not jobs:
            raise ValueError("No stored jobs available for evaluation")
        if not self.preserve_previous_analysis and any(job.analysis_provider == self.provider.provider_name for job in jobs):
            raise ValueError("Pass only jobs without a persisted Polza analysis; repeating an analysis is prohibited")
        if self.preserve_previous_analysis and any(job.analysis_prompt_version == self.analysis_prompt_version for job in jobs):
            raise ValueError("A job already has this calibration prompt version; repeating it is prohibited")
        if self.analyzer_version and any(job.analysis_analyzer_version == self.analyzer_version for job in jobs):
            raise ValueError("A job already has this analyzer version; repeating it is prohibited")

        usages: list[ProviderUsage] = []
        evaluated: list[Job] = []
        failures: list[EvaluationFailure] = []
        repair_retries = 0
        for job in jobs:
            request_count_before = self.provider.completion_request_count
            try:
                evaluated_job, usage = await self._analyze_and_persist(job, request_count_before)
                evaluated.append(evaluated_job)
                if usage:
                    usages.append(usage)
            except AIProviderError as exc:
                failures.append(EvaluationFailure(job_id=job.id, title=job.title, error=str(exc)))
                # A successful HTTP response can expose usage even if its JSON is
                # invalid; retain that billing metadata without persisting analysis.
                usage = self._aggregate_current_usages()
                if usage:
                    usages.append(usage)
            finally:
                repair_retries += max(0, self.provider.completion_request_count - request_count_before - 1)
        return EvaluationResult(
            analyzed=len(evaluated), repair_retries=repair_retries, failures=failures,
            usages=usages, jobs=evaluated,
        )

    async def _analyze_and_persist(self, job: Job, request_count_before: int) -> tuple[Job, ProviderUsage | None]:
        source_facts_before = job.model_dump(include=SOURCE_FACT_FIELDS, mode="json")
        started_at = time.perf_counter()
        analysis = await self.provider.analyze_job(job)
        if self.preserve_previous_analysis and job.analysis_provider:
            job.analysis_history.append(self._snapshot(job))
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        self._apply_analysis(job, analysis, request_count_before, elapsed_ms)
        if source_facts_before != job.model_dump(include=SOURCE_FACT_FIELDS, mode="json"):
            raise RuntimeError("Controlled evaluation attempted to modify immutable source facts")
        usage = self._aggregate_current_usages()
        # Analysis-only: retain any pre-existing draft unchanged and never call
        # generate_response. Shortlisting is local state, not a notification.
        job.status = JobStatus.SHORTLISTED if (job.final_score or 0) >= self.notification_threshold else JobStatus.REJECTED
        self.repository.save(job, dedupe_key_for(job))
        return job, usage

    def _snapshot(self, job: Job) -> AnalysisSnapshot:
        return AnalysisSnapshot(
            provider=job.analysis_provider or "unknown",
            model=job.analysis_model or "unknown",
            analyzer_version=job.analysis_analyzer_version,
            prompt_version=job.analysis_prompt_version,
            summary=job.analysis_summary,
            complexity=job.complexity_score,
            codex_share=job.codex_share,
            codex_share_reason=job.analysis_codex_share_reason,
            ai_executable_work=job.analysis_ai_executable_work,
            owner_required_work=job.analysis_owner_required_work,
            external_dependency_work=job.analysis_external_dependency_work,
            total_hours_min=job.estimated_total_hours_min,
            total_hours_max=job.estimated_total_hours_max,
            owner_hours_min=job.estimated_owner_hours_min,
            owner_hours_max=job.estimated_owner_hours_max,
            owner_communication_hours=job.owner_communication_hours,
            owner_access_setup_hours=job.owner_access_setup_hours,
            owner_review_testing_hours=job.owner_review_testing_hours,
            owner_manual_execution_hours=job.owner_manual_execution_hours,
            productization=job.analysis_productization_score if job.analysis_productization_score is not None else (
                job.productization_score / 10 if job.productization_score is not None else None
            ),
            confidence=job.analysis_confidence,
            final_score=job.final_score,
            provisional_score=job.provisional_score,
            telemetry=job.analysis_telemetry,
            captured_at=datetime.now(timezone.utc),
        )

    def _apply_analysis(self, job: Job, analysis: JobAnalysis, request_count_before: int, elapsed_ms: int) -> None:
        structured = self.provider.last_structured_analysis
        usage = self._aggregate_current_usages()
        if structured is None:
            raise RuntimeError("Polza provider returned analysis without validated structured metadata")
        self.scoring.analyze(job, analysis)
        job.analysis_provider = self.provider.provider_name
        job.analysis_model = self.provider.model
        job.analysis_summary = analysis.summary
        job.analysis_deliverable = analysis.expected_deliverable
        job.analysis_required_skills = analysis.required_technologies
        job.analysis_pricing_notes = analysis.pricing_notes
        job.analysis_confidence = structured.confidence
        job.analysis_prompt_version = self.analysis_prompt_version
        job.analysis_analyzer_version = self.analyzer_version
        job.analysis_analyzed_at = datetime.now(timezone.utc)
        job.analysis_requirement_clarity_score = structured.requirement_clarity
        job.analysis_fit_score = structured.fit_score
        job.analysis_win_probability_score = structured.win_probability_score
        job.analysis_productization_score = structured.productization_score
        job.analysis_codex_share_reason = structured.codex_share_reason
        job.analysis_ai_executable_work = structured.ai_executable_work
        job.analysis_owner_required_work = structured.owner_required_work
        job.analysis_external_dependency_work = structured.external_dependency_work
        job.estimated_total_hours_min = structured.estimated_total_hours_min
        job.estimated_total_hours_max = structured.estimated_total_hours_max
        job.estimated_owner_hours_min = structured.estimated_owner_hours_min
        job.estimated_owner_hours_max = structured.estimated_owner_hours_max
        job.owner_communication_hours = structured.owner_communication_hours
        job.owner_access_setup_hours = structured.owner_access_setup_hours
        job.owner_review_testing_hours = structured.owner_review_testing_hours
        job.owner_manual_execution_hours = structured.owner_manual_execution_hours
        job.technical_risk_score = structured.technical_risk
        job.commercial_risk_score = structured.commercial_risk
        if usage:
            job.analysis_prompt_tokens = usage.prompt_tokens
            job.analysis_completion_tokens = usage.completion_tokens
            job.analysis_total_tokens = usage.total_tokens
        completion_request_count = self.provider.completion_request_count - request_count_before
        job.analysis_telemetry = AnalysisTelemetry(
            provider=self.provider.provider_name,
            model=self.provider.model,
            analyzer_version=self.analyzer_version,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            cost_rub=usage.cost_rub if usage else None,
            completion_request_count=completion_request_count,
            repair_retry_count=max(0, completion_request_count - 1),
            elapsed_ms=elapsed_ms,
            analyzed_at=job.analysis_analyzed_at,
        )

    def _aggregate_current_usages(self) -> ProviderUsage | None:
        attempts = getattr(self.provider, "last_analysis_usages", None)
        if not attempts:
            return self.provider.last_usage

        def total_optional(field: str) -> int | float | None:
            values = [getattr(item, field) for item in attempts]
            present = [value for value in values if value is not None]
            return sum(present) if present else None

        return ProviderUsage(
            provider=self.provider.provider_name,
            model=self.provider.model,
            prompt_tokens=total_optional("prompt_tokens"),
            completion_tokens=total_optional("completion_tokens"),
            reasoning_tokens=total_optional("reasoning_tokens"),
            total_tokens=total_optional("total_tokens"),
            cost_rub=total_optional("cost_rub"),
        )
