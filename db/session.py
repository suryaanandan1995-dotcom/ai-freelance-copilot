"""Engine/session factory + schema bootstrap. SQLite by default."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from db.models import Base

logger = logging.getLogger(__name__)

_settings = get_settings()
# An empty COPILOT_DATABASE_URL (e.g. an unset secret rendered as "" in CI) must
# not override the default — treat blank as "unset" and fall back to SQLite.
_db_url = (_settings.database_url or "").strip() or "sqlite:///copilot.db"
_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _ensure_columns(bind) -> list[str]:
    """Add any model columns missing from EXISTING tables (lightweight auto-migration).

    ``create_all`` only creates missing *tables*, never missing *columns* on a table
    that already exists — so a table created by an older schema silently lacks columns
    added later (e.g. ``outreach.replied``). This inspects each existing table and
    ``ALTER TABLE ... ADD COLUMN`` for anything the model defines but the DB lacks.

    Columns are added as NULLABLE (no default/constraint) so the operation is safe on
    populated tables across dialects; existing rows get NULL, which our queries handle.
    Idempotent, defensive (never raises), and a no-op on a fresh DB. Returns the list
    of ``table.column`` it added (for logging/one-shot migration output).
    """
    added: list[str] = []
    try:
        insp = inspect(bind)
        existing_tables = set(insp.get_table_names())
    except Exception as exc:  # inspection failed — don't block startup
        logger.warning("_ensure_columns: could not inspect schema: %s", exc)
        return added

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table: create_all already handled it
        try:
            have = {c["name"] for c in insp.get_columns(table.name)}
        except Exception as exc:
            logger.warning("_ensure_columns: could not read %s columns: %s", table.name, exc)
            continue
        for col in table.columns:
            if col.name in have:
                continue
            try:
                ddl_type = col.type.compile(dialect=bind.dialect)
                with bind.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}'))
                added.append(f"{table.name}.{col.name}")
                logger.info("_ensure_columns: added %s.%s (%s)", table.name, col.name, ddl_type)
            except Exception as exc:  # one bad column mustn't stop the rest
                logger.warning(
                    "_ensure_columns: could not add %s.%s: %s", table.name, col.name, exc
                )
    return added


def init_db() -> None:
    """Create missing tables, then backfill any missing columns on existing tables."""
    Base.metadata.create_all(engine)
    _ensure_columns(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
