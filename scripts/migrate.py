"""One-shot schema migration against COPILOT_DATABASE_URL.

Runs the same lightweight auto-migration that ``init_db`` now performs on every
startup — create any missing tables, then ``ALTER TABLE ... ADD COLUMN`` for any
model column missing from an existing table (e.g. the ``outreach`` follow-up /
funnel columns that a table created by an older schema is missing).

Intended to be triggered in the cloud (GitHub Actions), because the workstation
can't reach Neon on 5432. Safe to run repeatedly — it's idempotent and only adds
what's actually missing.

    python -m scripts.migrate
"""
from __future__ import annotations

from db.session import _ensure_columns, engine, init_db


def main() -> int:
    # init_db already creates tables + ensures columns; call the ensure directly too
    # so we can report exactly what was added.
    init_db()
    added = _ensure_columns(engine)
    if added:
        print(f"Migration complete — added {len(added)} column(s):")
        for item in added:
            print(f"  + {item}")
    else:
        print("Schema already up to date — nothing to add.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
