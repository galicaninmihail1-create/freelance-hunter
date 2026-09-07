from app.ai.polza import PolzaStructuredAnalysis, ProviderUsage
from app.models import Job
from app.scoring import ScoringEngine
from app.services.polza_evaluation import ControlledPolzaEvaluation


class _Repository:
    def __init__(self) -> None:
        self.saved: list[Job] = []

    def save(self, job: Job, dedupe_key: str) -> Job:
        self.saved.append(job.model_copy(deep=True))
        return job


class _Provider:
    provider_name = "polza"
    model = "qwen/qwen3.8-flash"

    def __init__(self) -> None:
        self.completion_request_count = 0
        self.last_usage = ProviderUsage(provider="polza", model=self.model, prompt_tokens=10, completion_tokens=20, total_tokens=30)
        self.last_structured_analysis = PolzaStructuredAnalysis(
            client_summary="Integration requested", deliverable="Configured API integration", required_skills=["API"],
            complexity_score=4, codex_share=60, codex_share_reason="API implementation, mapping, tests, and documentation are largely coding work.",
            ai_executable_work=["API client", "Tests"], owner_required_work=["Acceptance"], external_dependency_work=["Credentials"],
            estimated_total_hours_min=4, estimated_total_hours_max=8,
            estimated_owner_hours_min=2, estimated_owner_hours_max=3, requirement_clarity=7, technical_risk=3,
            owner_communication_hours=0.5, owner_access_setup_hours=0.5, owner_review_testing_hours=1.5, owner_manual_execution_hours=0,
            commercial_risk=2, fit_score=8, win_probability_score=6, productization_score=7,
            problem_category="integration", technical_risks=[], commercial_risks=[], pricing_notes="", confidence=0.8,
        )

    async def analyze_job(self, job: Job):
        self.completion_request_count += 1
        return self.last_structured_analysis.to_job_analysis()

    async def generate_response(self, *args, **kwargs):
        raise AssertionError("Draft generation is forbidden in an analysis-only batch")


async def test_controlled_evaluation_preserves_source_and_never_generates_drafts() -> None:
    repository = _Repository()
    provider = _Provider()
    job = Job(source="fl.ru", title="Source title", description="Source description", url="https://example.test/job", response_draft="existing draft")
    result = await ControlledPolzaEvaluation(repository, provider, ScoringEngine(), notification_threshold=70).run([job])
    assert result.analyzed == 1
    assert result.response_drafts_generated == 0
    assert job.title == "Source title"
    assert job.description == "Source description"
    assert job.url == "https://example.test/job"
    assert job.response_draft == "existing draft"
    assert repository.saved[0].analysis_provider == "polza"
    assert job.analysis_telemetry is not None
    assert job.analysis_telemetry.completion_request_count == 1
    assert job.analysis_telemetry.total_tokens == 30


async def test_calibration_preserves_previous_analysis_as_history() -> None:
    repository = _Repository()
    provider = _Provider()
    job = Job(
        source="fl.ru", title="Source title", description="Source description", budget_min=10000,
        analysis_provider="polza", analysis_model="qwen/qwen3.8-flash", codex_share=40,
        estimated_owner_hours_min=2, estimated_owner_hours_max=5, analysis_productization_score=3,
        final_score=42, provisional_score=42,
    )
    await ControlledPolzaEvaluation(
        repository, provider, ScoringEngine(), notification_threshold=70,
        analysis_prompt_version="calibration-v2", analyzer_version="v1", preserve_previous_analysis=True,
    ).run([job])
    assert job.title == "Source title"
    assert job.description == "Source description"
    assert job.budget_min == 10000
    assert job.analysis_prompt_version == "calibration-v2"
    assert job.analysis_analyzer_version == "v1"
    assert job.analysis_telemetry is not None
    assert job.analysis_telemetry.analyzer_version == "v1"
    assert len(job.analysis_history) == 1
    assert job.analysis_history[0].codex_share == 40
    assert job.analysis_history[0].final_score == 42
    assert job.analysis_history[0].telemetry is None
    assert job.analysis_history[0].analyzer_version is None


async def test_analyzer_version_prevents_duplicate_current_analysis() -> None:
    repository = _Repository()
    provider = _Provider()
    job = Job(source="fl.ru", title="Already current", analysis_analyzer_version="v1")
    try:
        await ControlledPolzaEvaluation(
            repository, provider, ScoringEngine(), notification_threshold=70,
            analysis_prompt_version="calibration-v3", analyzer_version="v1",
            preserve_previous_analysis=True,
        ).run([job])
    except ValueError as exc:
        assert "already has this analyzer version" in str(exc)
    else:
        raise AssertionError("Duplicate analyzer_v1 analysis was not refused")
