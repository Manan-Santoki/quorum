"""Database engine, session factory, and schema bootstrap.

We use a synchronous SQLAlchemy engine deliberately: APScheduler's
``SQLAlchemyJobStore`` is sync, and keeping one engine avoids a second
connection pool. The bots (async) call DB helpers via ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables if they don't exist.

    Fine for v1; migrate to Alembic before schema churn in production.
    """
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
