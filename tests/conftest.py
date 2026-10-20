"""Pytest fixtures for isolated database, settings, and FastAPI TestClient."""
from __future__ import annotations

from pathlib import Path
from typing import Generator

from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.api.dependencies import rate_limiter
from app.core.config import get_settings
from app.db.models import Base
from app.db.session import configure_database, get_engine, get_session_factory
from app.main import create_app

TEST_ADMIN_TOKEN = "test-super-secret-admin-bearer-token-999999"


@pytest.fixture
def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    """Provide a fresh isolated SQLite database session per test."""
    db_file = tmp_path / "test_appvault.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("API_ADMIN_TOKEN", TEST_ADMIN_TOKEN)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-discord-bot-token-placeholder")
    monkeypatch.setenv("DISCORD_ADMIN_USER_IDS", "9001,9002")
    monkeypatch.setenv("RESPECT_ROBOTS_TXT", "false")
    monkeypatch.setenv("MAX_RETRIES", "1")

    get_settings.cache_clear()
    rate_limiter.reset()

    engine = configure_database(db_url)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
        engine.dispose()
        get_settings.cache_clear()


@pytest.fixture
def api_client(db_session: Session) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient wired to the isolated test database."""
    settings = get_settings()
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client
    # Ensure schema remains clean if needed
    _ = get_engine()
