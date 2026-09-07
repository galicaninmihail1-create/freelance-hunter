from __future__ import annotations

import re

from app.models import BudgetStatus, Job, RawJob


_BUDGET = re.compile(r"(?P<low>[\d\s]{2,})\s*(?:[-–—]\s*(?P<high>[\d\s]{2,}))?\s*(?P<currency>руб\.?|₽|rub|р\.)", re.IGNORECASE)


def normalize(raw: RawJob) -> Job:
    """Maps source data only; absent values stay nullable instead of being invented."""
    low = high = None
    currency = None
    # FL.ru RSS has no dedicated <budget> element. Some entries explicitly embed
    # a currency-tagged budget in their title or description; both are source facts.
    match = _BUDGET.search(" ".join(part for part in (raw.title, raw.budget_text, raw.description) if part))
    budget_status = BudgetStatus.UNKNOWN
    if match:
        low = float(match.group("low").replace(" ", ""))
        high = float(match.group("high").replace(" ", "")) if match.group("high") else low
        currency = "RUB"
        budget_status = BudgetStatus.ZERO if low == 0 and high == 0 else BudgetStatus.PROVIDED
    return Job(source=raw.source.lower(), source_job_id=raw.source_job_id, url=raw.url, title=" ".join(raw.title.split()),
               description=" ".join(raw.description.split()), published_at=raw.published_at, category=raw.category,
               skills=raw.skills, budget_min=low, budget_max=high, currency=currency, source_budget_status=budget_status)
