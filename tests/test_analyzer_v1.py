from app.models import Job
from app.report_analyzer_v1 import canonical_category


def test_amocrm_is_not_misclassified_as_ocr() -> None:
    job = Job(
        source="fl.ru",
        title="Передача лидов SaleBot > amoCRM",
        problem_category="CRM Integration & Automation",
    )
    assert canonical_category(job) == "CRM / messaging integration"


def test_russian_work_word_is_not_misclassified_as_bot() -> None:
    job = Job(
        source="fl.ru",
        title="Разработка корпоративного калькулятора для расчёта тендеров",
        problem_category="Custom Business Software / Domain-Specific Calculator",
    )
    assert canonical_category(job) == "Business calculations / reporting"
