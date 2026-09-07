from datetime import datetime

from app.models import BudgetStatus, RawJob
from app.normalizer import normalize


def test_normalizer_maps_only_source_data() -> None:
    job = normalize(RawJob(source="FL.RU", source_job_id="12", title="  API   integration ", description="  Need  bot ",
                           budget_text="30 000 – 50 000 ₽", published_at=datetime(2026, 1, 1)))
    assert job.source == "fl.ru"
    assert job.budget_min == 30000
    assert job.budget_max == 50000
    assert job.currency == "RUB"
    assert job.source_budget_status == BudgetStatus.PROVIDED
    assert job.client_name is None


def test_normalizer_distinguishes_unknown_and_zero_budget() -> None:
    unknown = normalize(RawJob(source="fl.ru", title="API", budget_text="по договоренности"))
    zero = normalize(RawJob(source="fl.ru", title="API", budget_text="0 ₽"))
    assert unknown.source_budget_status == BudgetStatus.UNKNOWN
    assert unknown.budget_min is None
    assert zero.source_budget_status == BudgetStatus.ZERO
    assert zero.budget_min == 0


def test_normalizer_extracts_explicit_budget_embedded_in_rss_title() -> None:
    job = normalize(RawJob(source="fl.ru", title="Доработка CRM (Бюджет: 5 000 ₽)"))
    assert job.source_budget_status == BudgetStatus.PROVIDED
    assert job.budget_min == 5000
