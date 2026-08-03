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


def missing_columns(bind) -> list[str]:
    """``table.column`` entries the models define but the live DB still lacks.

    Read-only counterpart to :func:`_ensure_columns`, and the reason it exists:
    ``_ensure_columns`` deliberately swallows every ``ALTER TABLE`` failure so one
    bad column can't block startup. That safety has a cost — a heal that *never
    succeeds* (no DDL grant on a managed Postgres, a type the dialect won't accept)
    looks identical to "nothing to do". Callers that report health must be able to
    tell those apart, so ask the DB what is actually missing instead of inferring it
    from what the heal claimed to add.

    Returns [] when the schema is complete or cannot be inspected (never raises).
    """
    missing: list[str] = []
    try:
        insp = inspect(bind)
        existing_tables = set(insp.get_table_names())
    except Exception as exc:
        logger.warning("missing_columns: could not inspect schema: %s", exc)
        return missing

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            missing.append(f"{table.name}.*")
            continue
        try:
            have = {c["name"] for c in insp.get_columns(table.name)}
        except Exception as exc:
            logger.warning("missing_columns: could not read %s columns: %s", table.name, exc)
            continue
        missing.extend(f"{table.name}.{c.name}" for c in table.columns if c.name not in have)
    return missing


def init_db() -> None:
    """Create missing tables, backfill missing columns, and log anything still absent.

    The log line matters: a column that could not be added will otherwise surface much
    later as an opaque ``no such column`` mid-run (this is how a missing
    ``outreach.replied`` took down the optimizer), with nothing at startup pointing at
    the schema. Still non-fatal — most of the system works with a partial schema, and
    refusing to boot would turn one broken column into total unavailability.
    """
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    still_missing = missing_columns(engine)
    if still_missing:
        logger.error(
            "init_db: %d column(s) could not be created and are STILL MISSING: %s — "
            "queries touching them will fail at runtime. Check DDL permissions on the "
            "database user.",
            len(still_missing),
            ", ".join(still_missing),
        )


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
