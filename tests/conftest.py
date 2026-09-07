import os
import socket
import tempfile
from pathlib import Path

import pytest

# These overrides must exist before test modules import app.main. Importing that
# module constructs the default FastAPI app and initializes its repository.
_SESSION_SANDBOX = tempfile.TemporaryDirectory(prefix="freelance-hunter-pytest-")
_BOOTSTRAP_DB = Path(_SESSION_SANDBOX.name) / "bootstrap.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_BOOTSTRAP_DB}",
    "HUNTER_MODE": "manual",
    "RUN_SCHEDULER": "false",
    "AI_PROVIDER": "mock",
    "AI_API_KEY": "",
    "POLZA_API_KEY": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_ALLOWED_CHAT_ID": "",
    "TELEGRAM_WEBHOOK_SECRET": "",
    "FL_RSS_URLS": "",
})

from app.config import Settings
from app.db import JobRepository


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow asyncio loopback plumbing, but fail every external network attempt."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def is_loopback(address) -> bool:
        return isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}

    def connect(sock, address):
        if is_loopback(address):
            return original_connect(sock, address)
        raise RuntimeError("External network is disabled during tests")

    def connect_ex(sock, address):
        if is_loopback(address):
            return original_connect_ex(sock, address)
        raise RuntimeError("External network is disabled during tests")

    def create_connection(address, *args, **kwargs):
        if is_loopback(address):
            return original_create_connection(address, *args, **kwargs)
        raise RuntimeError("External network is disabled during tests")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Close the import-time test repository before removing its temporary DB."""
    from app.main import app

    app.state.repository.engine.dispose()
    _SESSION_SANDBOX.cleanup()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", min_notification_score=60)


@pytest.fixture
def repository(settings: Settings) -> JobRepository:
    repo = JobRepository(settings.database_url)
    repo.initialize()
    return repo
