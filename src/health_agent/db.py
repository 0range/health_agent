from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from health_agent.config import Settings


def build_engine(settings: Settings) -> Engine:
    """Build a PostgreSQL engine without opening a transaction."""
    assert settings.database_url is not None
    return create_engine(settings.database_url, pool_pre_ping=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Commit a database session or roll it back if the caller fails."""
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
