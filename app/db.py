from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine

    if _engine is None:
        settings = settings or get_settings()
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        _engine = create_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory

    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


@contextmanager
def db_session(settings: Settings | None = None) -> Generator[Session, None, None]:
    session = get_session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def initialize_database(settings: Settings | None = None) -> None:
    import app.models  # noqa: F401
    from sqlalchemy import text

    engine = get_engine(settings)
    Base.metadata.create_all(bind=engine)

    # Manual migration: add new columns to existing tables.
    # SQLAlchemy's create_all не делает ALTER TABLE для новых колонок.
    # Используем ADD COLUMN с IF NOT EXISTS-стиле через try/except.
    migrations = [
        "ALTER TABLE positions ADD COLUMN bucket TEXT DEFAULT 'experiment'",
        "ALTER TABLE positions ADD COLUMN cluster_key TEXT",
    ]
    with engine.begin() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
            except Exception:
                # Column already exists or syntax not supported — skip silently.
                pass
