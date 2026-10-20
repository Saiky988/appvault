"""SQLAlchemy 2.x engine and session factory with SQLite and PostgreSQL compatibility."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import Base

_engine: Optional[Engine] = None
_session_factory: Optional[Any] = None


def build_engine(database_url: Optional[str] = None, settings: Optional[Settings] = None) -> Engine:
    """Create a configured SQLAlchemy Engine supporting both SQLite and PostgreSQL."""
    cfg = settings or get_settings()
    url = database_url or cfg.database_url

    if url.startswith("sqlite"):
        cfg.ensure_sqlite_directory()
        connect_args = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(url, connect_args=connect_args, future=True)

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    # PostgreSQL configuration
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
    )


def get_engine() -> Engine:
    """Return singleton SQLAlchemy Engine for current process."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> Any:
    """Return singleton sessionmaker bound to process engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _session_factory


def configure_database(database_url: str) -> Engine:
    """Reconfigure database engine and sessionmaker (useful for isolated tests)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = build_engine(database_url=database_url)
    _session_factory = sessionmaker(
        bind=_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        class_=Session,
    )
    return _engine


def init_db(engine: Optional[Engine] = None) -> None:
    """Ensure all database tables exist."""
    target_engine = engine or get_engine()
    Base.metadata.create_all(bind=target_engine)


def check_db_health(session: Optional[Session] = None) -> bool:
    """Verify database connectivity with a lightweight SELECT 1 query."""
    try:
        if session is not None:
            session.execute(text("SELECT 1"))
            return True
        with session_scope() as db:
            db.execute(text("SELECT 1"))
            return True
    except Exception:
        return False


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional context manager for scripts, bot commands, and background workers."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database Session per request."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
