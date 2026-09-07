from __future__ import annotations

from dataclasses import dataclass

from app.models import Job


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class FilterSettings:
    min_budget_rub: float = 3000


class HardFilter:
    manual_markers = ("раздача листовок", "ручной ввод", "ежедневно писать посты", "оператор на звонках", "круглосуточная поддержка")
    relevant_markers = ("python", "typescript", "telegram", "бот", "api", "интеграц", "crm", "парс", "автоматиза", "ai", "ии", "llm", "backend", "данных", "мониторинг")

    def __init__(self, settings: FilterSettings = FilterSettings()):
        self.settings = settings

    def evaluate(self, job: Job) -> FilterResult:
        text = f"{job.title} {job.description} {job.category or ''}".lower()
        if any(marker in text for marker in self.manual_markers):
            return FilterResult(False, "manual_recurring_work")
        if job.budget_max is not None and job.budget_max < self.settings.min_budget_rub:
            return FilterResult(False, "budget_below_threshold")
        if len(text.strip()) < 15:
            return FilterResult(False, "insufficient_description")
        if not any(marker in text for marker in self.relevant_markers):
            return FilterResult(False, "outside_target_profile")
        return FilterResult(True)
