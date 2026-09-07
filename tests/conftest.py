from pathlib import Path

import pytest

from app.config import Settings
from app.db import JobRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", min_notification_score=60)


@pytest.fixture
def repository(settings: Settings) -> JobRepository:
    repo = JobRepository(settings.database_url)
    repo.initialize()
    return repo
