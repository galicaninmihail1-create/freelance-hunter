from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import Job, JobAnalysis, ResponseDraft
from app.ai.safety import build_untrusted_job_context


class AIProvider(ABC):
    @abstractmethod
    async def analyze_job(self, job: Job) -> JobAnalysis: ...

    @abstractmethod
    async def generate_response(self, job: Job, analysis: JobAnalysis) -> ResponseDraft: ...


class MockAIProvider(AIProvider):
    """Deterministic, offline provider for local development and tests."""
    async def analyze_job(self, job: Job) -> JobAnalysis:
        # Kept here so every provider implementation has a single explicit safety boundary to reuse.
        build_untrusted_job_context(job)
        text = f"{job.title} {job.description}".lower()
        category = "telegram_crm_integration" if "telegram" in text and "crm" in text else "automation"
        codex_share = 90 if any(word in text for word in ("api", "бот", "python", "интеграц")) else 70
        return JobAnalysis(summary=job.description[:400] or job.title, expected_deliverable="Configured and documented automation solution",
                           required_technologies=["Python", "HTTP API"], technical_complexity=4, estimated_total_hours=12,
                           estimated_owner_hours=2.5, codex_share=codex_share, technical_risks=["Access credentials may be delayed"],
                           commercial_risks=[], requirement_clarity=75, hidden_complexity=30, capability_fit=85,
                           win_probability=55, productization_potential=75, strategic_value=70, problem_category=category,
                           recommended_price=job.budget_max or job.budget_min, uncertainty_notes="Mock estimate; confirm scope with client.")

    async def generate_response(self, job: Job, analysis: JobAnalysis) -> ResponseDraft:
        questions = ["Какие системы уже используются и есть ли доступ к их API?", "Какой результат нужен на первом этапе?"]
        return ResponseDraft(text=(f"Здравствуйте! Посмотрел задачу «{job.title}». Предлагаю начать с короткого уточнения процесса, "
                                   f"затем собрать решение на {', '.join(analysis.required_technologies)} и передать понятную инструкцию. "
                                   f"Вижу итог как: {analysis.expected_deliverable}. {questions[0]}"), questions=questions)
