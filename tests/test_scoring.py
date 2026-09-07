from app.models import BudgetStatus, Job, JobAnalysis
from app.scoring import ScoringEngine
import pytest


def _analysis(**changes) -> JobAnalysis:
    data = dict(summary="API automation", expected_deliverable="bot", technical_complexity=4, estimated_total_hours=10,
                estimated_owner_hours=2, codex_share=90, requirement_clarity=85, hidden_complexity=20,
                capability_fit=90, win_probability=70, productization_potential=80, strategic_value=75,
                problem_category="crm_automation", recommended_price=40000)
    data.update(changes)
    return JobAnalysis(**data)


def test_scoring_is_deterministic_and_capped() -> None:
    engine = ScoringEngine()
    source_job = dict(source="fl.ru", title="API", description="API", budget_min=40000, budget_max=40000,
                      source_budget_status=BudgetStatus.PROVIDED)
    one = engine.analyze(Job(**source_job), _analysis())
    two = engine.analyze(Job(**source_job), _analysis())
    assert one.final_score == two.final_score
    assert 0 <= one.final_score <= 100
    assert engine.revenue_per_owner_hour(one) == 20000


def test_profitability_uses_owner_hour_floor() -> None:
    engine = ScoringEngine()
    job = engine.analyze(Job(source="fl.ru", title="API", description="API", budget_max=40000,
                             source_budget_status=BudgetStatus.PROVIDED), _analysis(estimated_owner_hours=0.1))
    assert engine.revenue_per_owner_hour(job) == 40000


def test_unknown_budget_gets_provisional_score_without_profit_penalty() -> None:
    engine = ScoringEngine()
    job = engine.analyze(Job(source="fl.ru", title="API", description="API", source_budget_status=BudgetStatus.UNKNOWN), _analysis(recommended_price=None))
    assert job.score_is_provisional
    assert job.provisional_score == job.final_score
    assert job.profit_score is None
    assert engine.revenue_per_owner_hour(job) is None
    assert job.final_score > 70


def test_zero_budget_is_known_and_not_equivalent_to_unknown() -> None:
    engine = ScoringEngine()
    job = engine.analyze(Job(source="fl.ru", title="API", description="API", budget_min=0, budget_max=0, currency="RUB", source_budget_status=BudgetStatus.ZERO), _analysis(recommended_price=None))
    assert not job.score_is_provisional
    assert job.profit_score == 0
    assert engine.revenue_per_owner_hour(job) == 0


def test_scoring_preserves_source_facts_and_writes_ai_estimates_separately() -> None:
    engine = ScoringEngine()
    job = Job(source="fl.ru", title="API", description="source description", budget_min=10000, budget_max=20000,
              currency="RUB", source_budget_status=BudgetStatus.PROVIDED)
    scored = engine.analyze(job, _analysis(recommended_price=15000))
    assert (scored.title, scored.description, scored.budget_min, scored.budget_max, scored.currency) == ("API", "source description", 10000, 20000, "RUB")
    assert scored.codex_share == 90
    assert scored.estimated_owner_hours == 2


@pytest.mark.parametrize("codex_share", [0.6, -1, 101])
def test_scoring_refuses_invalid_codex_share_even_if_model_validation_is_bypassed(codex_share: float) -> None:
    invalid_analysis = _analysis().model_copy(update={"codex_share": codex_share})
    with pytest.raises(ValueError, match="codex_share"):
        ScoringEngine().analyze(Job(source="fl.ru", title="API"), invalid_analysis)
