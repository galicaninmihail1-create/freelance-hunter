import pytest

from app.db import dedupe_key_for
from app.models import Job, JobStatus


def test_dedupe_and_status_transition(repository) -> None:
    job = Job(source="fl.ru", source_job_id="123", title="bot")
    key = dedupe_key_for(job)
    repository.save(job, key)
    assert repository.exists("fl.ru", key)
    repository.transition(job.id, JobStatus.ANALYZED)
    assert repository.get(job.id).status == JobStatus.ANALYZED
    with pytest.raises(ValueError):
        repository.transition(job.id, JobStatus.COMPLETED)
