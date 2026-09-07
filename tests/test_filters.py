from app.filters import HardFilter
from app.models import Job


def test_filter_rejects_low_budget_manual_work() -> None:
    result = HardFilter().evaluate(Job(source="fl.ru", title="Ручной ввод данных", description="ручной ввод каждый день", budget_max=1000))
    assert not result.accepted
    assert result.reason == "manual_recurring_work"


def test_filter_keeps_relevant_job() -> None:
    assert HardFilter().evaluate(Job(source="fl.ru", title="Telegram bot", description="Нужна интеграция API и CRM", budget_max=40000)).accepted
