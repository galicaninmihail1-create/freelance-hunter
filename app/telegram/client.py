from __future__ import annotations

import html
import logging
import re

import httpx

from app.models import BudgetStatus, Job
from app.scoring import ScoringEngine

logger = logging.getLogger(__name__)


class _TelegramTokenRedactionFilter(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._token, "[telegram-token-redacted]")
        if isinstance(value, tuple):
            return tuple(self._redact(item) for item in value)
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        record.args = self._redact(record.args)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return True


def _protect_transport_logs(token: str | None) -> None:
    if not token:
        return
    redaction_filter = _TelegramTokenRedactionFilter(token)
    # httpx logs the full request URL at INFO, while the Telegram Bot API embeds
    # credentials in that URL. Keep routine transport logs disabled and redact
    # them as defense in depth if an operator explicitly lowers the logger level.
    for name in ("httpx", "httpcore"):
        transport_logger = logging.getLogger(name)
        if transport_logger.level < logging.WARNING:
            transport_logger.setLevel(logging.WARNING)
        transport_logger.addFilter(redaction_filter)


def _range(low: float | None, high: float | None, fallback: float | None = None) -> str:
    if low is not None and high is not None:
        if low == high:
            return f"{low:g}"
        return f"{low:g}–{high:g}"
    return f"{fallback:g}" if fallback is not None else "не оценено"


def _money(value: float) -> str:
    if float(value).is_integer():
        return f"{value:,.0f}".replace(",", " ")
    return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def _currency(currency: str | None) -> str:
    if (currency or "").strip().upper() in {"RUB", "RUR", "₽", "РУБ", "РУБ."}:
        return "₽"
    return (currency or "").strip()


def _budget(job: Job) -> str:
    if job.source_budget_status is BudgetStatus.UNKNOWN:
        return "не указан в RSS"
    if job.budget_min is None and job.budget_max is None:
        return "не указан в RSS"
    unit = _currency(job.currency)
    suffix = f" {unit}" if unit else ""
    if job.budget_min is not None and job.budget_max is not None:
        if job.budget_min == job.budget_max:
            return f"{_money(job.budget_min)}{suffix}"
        return f"{_money(job.budget_min)}–{_money(job.budget_max)}{suffix}"
    if job.budget_min is not None:
        return f"от {_money(job.budget_min)}{suffix}"
    return f"до {_money(job.budget_max or 0)}{suffix}"


def _russian_category(job: Job) -> str:
    source_category = (job.category or "").strip()
    base = source_category if re.search(r"[А-Яа-яЁё]", source_category) else "Автоматизация бизнеса"
    normalized = (job.problem_category or "").lower()
    if any(word in normalized for word in ("artificial intelligence", "machine learning", " ai", "ai_", "llm", "gpt")):
        detail = "AI-интеграции"
    elif "crm" in normalized:
        detail = "CRM-интеграции"
    elif any(word in normalized for word in ("telegram", "bot", "чат-бот", "бот")):
        detail = "Telegram-боты"
    elif any(word in normalized for word in ("api", "integration", "интеграц")):
        detail = "API-интеграции"
    elif any(word in normalized for word in ("erp", "1c", "1с")):
        detail = "Учётные системы"
    elif any(word in normalized for word in ("parser", "scrap", "data", "парс", "данн")):
        detail = "Сбор и обработка данных"
    elif any(word in normalized for word in ("automation", "автоматизац")):
        detail = "Автоматизация процессов"
    else:
        detail = "Другое"
    return base if detail.lower() in base.lower() else f"{base} / {detail}"


def _russian_client_need(job: Job) -> str:
    description = " ".join((job.description or "").split())
    if description and re.search(r"[А-Яа-яЁё]", description):
        return description[:500]
    return "Описание заказа доступно по ссылке; детали требуют ручной проверки."


def _interesting_reasons(job: Job, productization: float | None, owner_hours: str) -> list[str]:
    reasons: list[str] = []
    if job.codex_share is not None:
        if job.codex_share >= 70:
            reasons.append(f"Codex может выполнить значительную часть реализации — около {job.codex_share:g}%.")
        elif job.codex_share >= 40:
            reasons.append(f"Codex может взять на себя около {job.codex_share:g}% реализации.")
        else:
            reasons.append(f"Доля автоматизируемой реализации оценена примерно в {job.codex_share:g}%.")
    if productization is not None and productization >= 7:
        reasons.append("Результат хорошо подходит для повторного использования в похожих проектах.")
    if job.estimated_owner_hours_max is not None and job.estimated_owner_hours_max <= 8:
        reasons.append(f"Ожидаемое личное участие ограничено диапазоном {owner_hours} ч.")
    if len(reasons) < 2 and job.complexity_score is not None and job.complexity_score <= 6:
        reasons.append("Техническая сложность выглядит управляемой для короткого цикла реализации.")
    if len(reasons) < 2:
        reasons.append("Задача прошла фильтры и порог локального скоринга.")
    return reasons[:3]


def format_job_message(job: Job, scoring: ScoringEngine) -> str:
    score_kind = "предварительная" if job.score_is_provisional else "итоговая"
    productization = job.analysis_productization_score
    if productization is None and job.productization_score is not None:
        productization = job.productization_score / 10
    owner_hours = _range(job.estimated_owner_hours_min, job.estimated_owner_hours_max, job.estimated_owner_hours)
    summary = _russian_client_need(job)
    category = _russian_category(job)
    reasons = _interesting_reasons(job, productization, owner_hours)
    interesting = "\n".join(f"• {html.escape(item)}" for item in reasons)
    score = job.final_score if job.final_score is not None else 0
    confidence = (job.analysis_confidence or 0) * 100
    productization_label = f"{productization:g}" if productization is not None else "не оценена"
    link = html.escape(job.url or "", quote=True)
    link_line = f'<a href="{link}">🔗 Открыть заказ на FL.ru</a>' if link else "🔗 Ссылка на заказ отсутствует"
    return (
        f"🔥 <b>{html.escape(job.title)}</b>\n\n"
        f"💰 Бюджет: {html.escape(_budget(job))}\n"
        f"⭐ Оценка: {score:g}/100 ({score_kind})\n"
        f"🤖 Codex Share: {job.codex_share or 0:g}%\n"
        f"👤 Моё время: {owner_hours} ч\n"
        f"🧩 Сложность: {job.complexity_score or 0:g}/10\n"
        f"📦 Повторяемость: {productization_label}/10\n"
        f"🎯 Уверенность: {confidence:g}%\n"
        f"🗂 Категория: {html.escape(category)}\n\n"
        f"<b>Что нужно клиенту</b>\n{html.escape(summary)}\n\n"
        f"<b>Почему интересно</b>\n{interesting}\n\n"
        f"{link_line}"
    )


def keyboard_for(job: Job, *, include_draft: bool = True) -> dict:
    rows = [
        [
            {"text": "🔥 Интересно", "callback_data": f"interested:{job.id}"},
            {"text": "👎 Не подходит", "callback_data": f"rejected:{job.id}"},
        ],
    ]
    if include_draft:
        rows.append([{"text": "✍️ Подготовить отклик", "callback_data": f"draft_requested:{job.id}"}])
    return {"inline_keyboard": rows}


def format_fallback_job_message(job: Job) -> str:
    description = " ".join((job.description or "").split())
    description_line = html.escape(description[:1000]) if description else "Описание заказа отсутствует в RSS"
    category_line = (
        f"🗂 Категория: {html.escape(job.category)}\n"
        if job.category and job.category.strip()
        else ""
    )
    link = html.escape(job.url or "", quote=True)
    link_line = f'<a href="{link}">🔗 Открыть заказ на FL.ru</a>' if link else "🔗 Ссылка на заказ отсутствует"
    return (
        f"🔥 <b>{html.escape(job.title)}</b>\n\n"
        f"💰 Бюджет: {html.escape(_budget(job))}\n"
        f"{category_line}\n"
        "⚠️ AI-анализ временно недоступен\n\n"
        f"<b>Описание из RSS</b>\n{description_line}\n\n"
        f"{link_line}"
    )


class TelegramClient:
    def __init__(self, token: str | None, chat_id: str | None, client: httpx.AsyncClient | None = None):
        self.token, self.chat_id, self.client = token, chat_id, client
        _protect_transport_logs(token)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def notify(self, job: Job, scoring: ScoringEngine, *, fallback: bool = False) -> bool:
        if not self.enabled:
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": format_fallback_job_message(job) if fallback else format_job_message(job, scoring),
            "parse_mode": "HTML",
            "reply_markup": keyboard_for(job, include_draft=not fallback),
            "disable_web_page_preview": True,
        }
        return await self._post("sendMessage", payload, "notification", job.id)

    async def send_text(self, text: str) -> bool:
        if not self.enabled:
            return False
        return await self._post("sendMessage", {"chat_id": self.chat_id, "text": text}, "reply")

    async def _post(self, method: str, payload: dict, operation: str, job_id: str | None = None) -> bool:
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15)
        try:
            response = await client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            # Do not log the exception object: its request URL contains the bot token.
            logger.warning("Telegram %s failed%s", operation, f" for job {job_id}" if job_id else "")
            return False
        finally:
            if owned_client:
                await client.aclose()
