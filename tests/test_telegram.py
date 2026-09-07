import logging

import httpx

from app.models import BudgetStatus, Job
from app.scoring import ScoringEngine
from app.telegram.client import TelegramClient, format_job_message, keyboard_for


def test_telegram_format_and_buttons() -> None:
    job = Job(
        source="fl.ru", title="<Bot>", url="https://example.test/job", final_score=87,
        score_is_provisional=True, codex_share=92, estimated_owner_hours=3,
        estimated_owner_hours_min=2, estimated_owner_hours_max=4, complexity_score=4,
        productization_score=90, analysis_confidence=0.8,
        analysis_summary="Client needs an API bot", problem_category="bot automation",
        analysis_codex_share_reason="Most implementation is reusable application code.",
        source_budget_status=BudgetStatus.UNKNOWN,
    )
    text = format_job_message(job, ScoringEngine())
    assert "87/100" in text
    assert "&lt;Bot&gt;" in text
    assert "💰 Бюджет: не указан в RSS" in text
    assert "⭐ Оценка: 87/100 (предварительная)" in text
    assert "👤 Моё время: 2–4 ч" in text
    assert "📦 Повторяемость: 9/10" in text
    assert "🗂 Категория: Автоматизация бизнеса / Telegram-боты" in text
    assert "Client needs an API bot" not in text
    assert "Most implementation is reusable application code." not in text
    assert "bot automation" not in text
    assert "Owner hours" not in text
    assert "Productization" not in text
    assert "Confidence" not in text
    buttons = keyboard_for(job)["inline_keyboard"]
    assert buttons[0][0]["text"] == "🔥 Интересно"
    assert buttons[0][1]["text"] == "👎 Не подходит"
    assert buttons[1][0]["text"] == "✍️ Подготовить отклик"


def test_known_source_budget_is_formatted_in_rubles() -> None:
    job = Job(
        source="fl.ru", title="Интеграция", description="Нужна интеграция по API",
        budget_min=7000, budget_max=7000, currency="RUB",
        source_budget_status=BudgetStatus.PROVIDED,
    )
    text = format_job_message(job, ScoringEngine())
    assert "💰 Бюджет: 7 000 ₽" in text


def test_source_budget_range_is_formatted_without_using_ai_estimate() -> None:
    known = Job(
        source="fl.ru", title="Интеграция", description="Нужна интеграция",
        budget_min=7000, budget_max=12000, currency="RUB", recommended_price=999999,
        source_budget_status=BudgetStatus.PROVIDED,
    )
    unknown = Job(
        source="fl.ru", title="Интеграция", description="Нужна интеграция",
        recommended_price=999999, source_budget_status=BudgetStatus.UNKNOWN,
    )
    assert "💰 Бюджет: 7 000–12 000 ₽" in format_job_message(known, ScoringEngine())
    unknown_text = format_job_message(unknown, ScoringEngine())
    assert "💰 Бюджет: не указан в RSS" in unknown_text
    assert "999" not in unknown_text


async def test_raw_telegram_token_is_absent_from_httpx_logs(caplog) -> None:
    token = "123456:RAW-TELEGRAM-SECRET"

    async def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False}, request=request)

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as http_client:
        telegram = TelegramClient(token, "42", client=http_client)
        sent = await telegram.notify(
            Job(source="fl.ru", title="Тест", description="Проверка безопасных логов"),
            ScoringEngine(),
        )

    assert sent is False
    assert token not in caplog.text
