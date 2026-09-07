from __future__ import annotations

from dataclasses import dataclass

from app.models import BudgetStatus, Job, JobAnalysis


@dataclass(frozen=True)
class ScoringSettings:
    revenue_weight: float = 25
    codex_weight: float = 20
    fit_weight: float = 15
    win_weight: float = 15
    clarity_weight: float = 10
    productization_weight: float = 10
    strategic_weight: float = 5
    owner_hour_floor: float = 1.0
    hourly_reference_rub: float = 8000


class ScoringEngine:
    def __init__(self, settings: ScoringSettings = ScoringSettings()):
        self.settings = settings

    def analyze(self, job: Job, analysis: JobAnalysis) -> Job:
        if not 0 <= analysis.codex_share <= 100 or 0 < analysis.codex_share <= 1:
            raise ValueError("Scoring refuses ambiguous or out-of-range codex_share; expected percentage points in 0-100")
        owner_hours = max(analysis.estimated_owner_hours, self.settings.owner_hour_floor)
        source_budget_known = job.source_budget_status is not BudgetStatus.UNKNOWN
        source_price = job.budget_max if job.budget_max is not None else job.budget_min
        price = analysis.recommended_price if analysis.recommended_price is not None else source_price
        revenue_per_hour = price / owner_hours if price is not None else None
        revenue_score = min(100.0, revenue_per_hour / self.settings.hourly_reference_rub * 100) if revenue_per_hour is not None else None
        competition_penalty = min(15.0, (job.competition_count or 0) / 10)
        risk_penalty = min(15.0, len(analysis.technical_risks) * 3 + len(analysis.commercial_risks) * 2)
        owner_penalty = min(10.0, max(0, owner_hours - 8) * 0.8)
        scope_penalty = max(0.0, (60 - analysis.requirement_clarity) / 10)
        non_revenue_points = (
            analysis.codex_share * self.settings.codex_weight + analysis.capability_fit * self.settings.fit_weight +
            analysis.win_probability * self.settings.win_weight + analysis.requirement_clarity * self.settings.clarity_weight +
            analysis.productization_potential * self.settings.productization_weight + analysis.strategic_value * self.settings.strategic_weight
        )
        non_revenue_weight = 100 - self.settings.revenue_weight
        if source_budget_known:
            weighted = ((revenue_score or 0) * self.settings.revenue_weight + non_revenue_points) / 100
        else:
            weighted = non_revenue_points / non_revenue_weight
        job.complexity_score = analysis.technical_complexity
        job.codex_share = analysis.codex_share
        job.estimated_total_hours = analysis.estimated_total_hours
        job.estimated_owner_hours = analysis.estimated_owner_hours
        job.technical_risks = analysis.technical_risks
        job.commercial_risks = analysis.commercial_risks
        job.recommended_price = price
        job.fit_score = round(analysis.capability_fit, 1)
        job.profit_score = round(revenue_score, 1) if revenue_score is not None and source_budget_known else None
        job.codex_score = round(analysis.codex_share, 1)
        job.win_probability_score = round(analysis.win_probability, 1)
        job.productization_score = round(analysis.productization_potential, 1)
        job.problem_category = analysis.problem_category
        score = round(max(0.0, min(100.0, weighted - competition_penalty - risk_penalty - owner_penalty - scope_penalty)), 1)
        job.final_score = score
        job.provisional_score = score if not source_budget_known else None
        job.score_is_provisional = not source_budget_known
        return job

    def revenue_per_owner_hour(self, job: Job) -> float | None:
        if job.source_budget_status is BudgetStatus.UNKNOWN or job.recommended_price is None or job.estimated_owner_hours is None:
            return None
        return round(job.recommended_price / max(job.estimated_owner_hours, self.settings.owner_hour_floor), 2)
