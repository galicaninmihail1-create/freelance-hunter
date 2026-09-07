from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobStatus(StrEnum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    REJECTED = "REJECTED"
    SHORTLISTED = "SHORTLISTED"
    RESPONSE_PREPARED = "RESPONSE_PREPARED"
    RESPONSE_SENT = "RESPONSE_SENT"
    CLIENT_REPLIED = "CLIENT_REPLIED"
    WON = "WON"
    LOST = "LOST"
    COMPLETED = "COMPLETED"


class BudgetStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    ZERO = "ZERO"
    PROVIDED = "PROVIDED"


class RawJob(BaseModel):
    source: str
    source_job_id: str | None = None
    url: str | None = None
    title: str
    description: str = ""
    published_at: datetime | None = None
    budget_text: str | None = None
    category: str | None = None
    skills: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisTelemetry(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=150)
    analyzer_version: str | None = Field(default=None, min_length=1, max_length=50)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_rub: float | None = Field(default=None, ge=0)
    completion_request_count: int = Field(ge=1)
    repair_retry_count: int = Field(ge=0, le=1)
    elapsed_ms: int | None = Field(default=None, ge=0)
    analyzed_at: datetime


class AnalysisSnapshot(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=150)
    analyzer_version: str | None = Field(default=None, min_length=1, max_length=50)
    prompt_version: str | None = Field(default=None, max_length=100)
    summary: str | None = None
    complexity: float | None = Field(default=None, ge=0, le=10)
    codex_share: float | None = Field(default=None, ge=0, le=100)
    codex_share_reason: str | None = None
    ai_executable_work: list[str] = Field(default_factory=list)
    owner_required_work: list[str] = Field(default_factory=list)
    external_dependency_work: list[str] = Field(default_factory=list)
    total_hours_min: float | None = Field(default=None, gt=0)
    total_hours_max: float | None = Field(default=None, gt=0)
    owner_hours_min: float | None = Field(default=None, gt=0)
    owner_hours_max: float | None = Field(default=None, gt=0)
    owner_communication_hours: float | None = Field(default=None, ge=0)
    owner_access_setup_hours: float | None = Field(default=None, ge=0)
    owner_review_testing_hours: float | None = Field(default=None, ge=0)
    owner_manual_execution_hours: float | None = Field(default=None, ge=0)
    productization: float | None = Field(default=None, ge=0, le=10)
    confidence: float | None = Field(default=None, ge=0, le=1)
    final_score: float | None = Field(default=None, ge=0, le=100)
    provisional_score: float | None = Field(default=None, ge=0, le=100)
    telemetry: AnalysisTelemetry | None = None
    captured_at: datetime


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    source_job_id: str | None = None
    url: str | None = None
    title: str
    description: str = ""
    published_at: datetime | None = None
    deadline: datetime | None = None
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    currency: str | None = None
    source_budget_status: BudgetStatus = BudgetStatus.UNKNOWN
    category: str | None = None
    skills: list[str] = Field(default_factory=list)
    requirements: str | None = None
    client_name: str | None = None
    client_rating: float | None = Field(default=None, ge=0)
    client_hire_rate: float | None = Field(default=None, ge=0, le=100)
    client_projects_count: int | None = Field(default=None, ge=0)
    competition_count: int | None = Field(default=None, ge=0)
    complexity_score: float | None = Field(default=None, ge=0, le=10)
    codex_share: float | None = Field(default=None, ge=0, le=100)
    estimated_total_hours: float | None = Field(default=None, gt=0)
    estimated_owner_hours: float | None = Field(default=None, gt=0)
    technical_risks: list[str] = Field(default_factory=list)
    commercial_risks: list[str] = Field(default_factory=list)
    recommended_price: float | None = Field(default=None, ge=0)
    fit_score: float | None = Field(default=None, ge=0, le=100)
    profit_score: float | None = Field(default=None, ge=0, le=100)
    codex_score: float | None = Field(default=None, ge=0, le=100)
    win_probability_score: float | None = Field(default=None, ge=0, le=100)
    productization_score: float | None = Field(default=None, ge=0, le=100)
    final_score: float | None = Field(default=None, ge=0, le=100)
    provisional_score: float | None = Field(default=None, ge=0, le=100)
    score_is_provisional: bool = False
    problem_category: str | None = None
    response_draft: str | None = None
    analysis_provider: str | None = None
    analysis_model: str | None = None
    analysis_summary: str | None = Field(default=None, max_length=1200)
    analysis_deliverable: str | None = Field(default=None, max_length=800)
    analysis_required_skills: list[str] = Field(default_factory=list)
    analysis_pricing_notes: str | None = Field(default=None, max_length=600)
    analysis_prompt_tokens: int | None = Field(default=None, ge=0)
    analysis_completion_tokens: int | None = Field(default=None, ge=0)
    analysis_total_tokens: int | None = Field(default=None, ge=0)
    analysis_confidence: float | None = Field(default=None, ge=0, le=1)
    analysis_prompt_version: str | None = Field(default=None, max_length=100)
    analysis_analyzer_version: str | None = Field(default=None, min_length=1, max_length=50)
    analysis_analyzed_at: datetime | None = None
    analysis_telemetry: AnalysisTelemetry | None = None
    analysis_history: list[AnalysisSnapshot] = Field(default_factory=list)
    analysis_requirement_clarity_score: float | None = Field(default=None, ge=0, le=10)
    analysis_fit_score: float | None = Field(default=None, ge=0, le=10)
    analysis_win_probability_score: float | None = Field(default=None, ge=0, le=10)
    analysis_productization_score: float | None = Field(default=None, ge=0, le=10)
    analysis_codex_share_reason: str | None = Field(default=None, max_length=600)
    analysis_ai_executable_work: list[str] = Field(default_factory=list)
    analysis_owner_required_work: list[str] = Field(default_factory=list)
    analysis_external_dependency_work: list[str] = Field(default_factory=list)
    owner_communication_hours: float | None = Field(default=None, ge=0)
    owner_access_setup_hours: float | None = Field(default=None, ge=0)
    owner_review_testing_hours: float | None = Field(default=None, ge=0)
    owner_manual_execution_hours: float | None = Field(default=None, ge=0)
    estimated_total_hours_min: float | None = Field(default=None, gt=0)
    estimated_total_hours_max: float | None = Field(default=None, gt=0)
    estimated_owner_hours_min: float | None = Field(default=None, gt=0)
    estimated_owner_hours_max: float | None = Field(default=None, gt=0)
    technical_risk_score: float | None = Field(default=None, ge=0, le=10)
    commercial_risk_score: float | None = Field(default=None, ge=0, le=10)
    status: JobStatus = JobStatus.NEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1200)
    expected_deliverable: str = Field(min_length=1, max_length=800)
    required_technologies: list[str] = Field(default_factory=list)
    technical_complexity: float = Field(ge=0, le=10)
    estimated_total_hours: float = Field(gt=0, le=2000)
    estimated_owner_hours: float = Field(gt=0, le=500)
    codex_share: float = Field(ge=0, le=100)
    technical_risks: list[str] = Field(default_factory=list)
    commercial_risks: list[str] = Field(default_factory=list)
    requirement_clarity: float = Field(ge=0, le=100)
    hidden_complexity: float = Field(ge=0, le=100)
    capability_fit: float = Field(ge=0, le=100)
    win_probability: float = Field(ge=0, le=100)
    productization_potential: float = Field(ge=0, le=100)
    strategic_value: float = Field(ge=0, le=100)
    problem_category: str = Field(min_length=1, max_length=100)
    recommended_price: float | None = Field(default=None, ge=0)
    pricing_notes: str = Field(default="", max_length=600)
    uncertainty_notes: str = Field(default="", max_length=600)

    @field_validator("codex_share")
    @classmethod
    def codex_share_must_be_a_percentage(cls, value: float) -> float:
        if 0 < value <= 1:
            raise ValueError("codex_share must be percentage points in 0-100; values between 0 and 1 are ambiguous")
        return value


class ResponseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=30, max_length=4000)
    questions: list[str] = Field(default_factory=list, max_length=2)


class FeedbackType(StrEnum):
    INTERESTED = "INTERESTED"
    SKIPPED = "SKIPPED"


class Feedback(BaseModel):
    job_id: str
    feedback: FeedbackType
    reason: str | None = Field(default=None, max_length=250)


ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.NEW: {JobStatus.ANALYZED, JobStatus.REJECTED},
    JobStatus.ANALYZED: {JobStatus.SHORTLISTED, JobStatus.REJECTED, JobStatus.RESPONSE_PREPARED},
    JobStatus.SHORTLISTED: {JobStatus.RESPONSE_PREPARED, JobStatus.REJECTED},
    JobStatus.RESPONSE_PREPARED: {JobStatus.RESPONSE_SENT, JobStatus.REJECTED},
    JobStatus.RESPONSE_SENT: {JobStatus.CLIENT_REPLIED, JobStatus.LOST, JobStatus.WON},
    JobStatus.CLIENT_REPLIED: {JobStatus.WON, JobStatus.LOST},
    JobStatus.WON: {JobStatus.COMPLETED},
    JobStatus.REJECTED: set(), JobStatus.LOST: set(), JobStatus.COMPLETED: set(),
}


def validate_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid job status transition: {current} -> {target}")
