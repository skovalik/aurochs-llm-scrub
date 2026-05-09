"""Schema-application helper.

The actual DDL lives in ``core/migrations/0001_initial.sql``. This
module loads that file and applies it to a connection, idempotently.
Use this for fresh-database setup and tests; production upgrades go
through ``core.db.MigrationRunner`` which records ``schema_version``
rows and holds ``MigrateLock`` across the application.

The schema version is stored in two places that must agree:

* ``SCHEMA_VERSION`` constant here (semver string for human
  readability).
* ``CURRENT_SCHEMA_VERSION`` integer (matches the prefix on the latest
  ``NNNN_*.sql`` file in ``migrations/``).

The semver string is what gets recorded in
``~/.aurochs-llm-scrub/manifest.json`` for cross-plugin compat
detection (plan v4 cross-cutting fix #14, mirroring voice's pattern).
The integer is what the schema_version table tracks. Bumping either
requires bumping both, plus a new migration file with the new prefix.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Semver-style schema version tag. Recorded in manifest.json; read by
# voice's overlay layer to detect cross-plugin compat.
SCHEMA_VERSION: str = "0.1.0"

# Integer version tracked in the schema_version table. Must equal the
# prefix on the latest migration file.
CURRENT_SCHEMA_VERSION: int = 1

_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"


def schema_path(version: int = CURRENT_SCHEMA_VERSION) -> Path:
    """Return the filesystem path to the SQL file for a schema
    version.

    Raises
    ------
    FileNotFoundError
        If no migration file exists for the requested version.
    RuntimeError
        If multiple migration files share the same version prefix
        (developer error — the loader can't pick).
    """
    candidates = sorted(_MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
    if not candidates:
        raise FileNotFoundError(
            f"No migration file found for schema version {version} "
            f"in {_MIGRATIONS_DIR}"
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"Multiple migration files for version {version}: {candidates!r}"
        )
    return candidates[0]


def apply_schema(
    conn: sqlite3.Connection,
    version: int = CURRENT_SCHEMA_VERSION,
    description: str | None = None,
) -> None:
    """Apply the SQL DDL for a given schema version, idempotently.

    All statements use ``CREATE TABLE IF NOT EXISTS`` /
    ``INSERT OR IGNORE`` so re-running this on an already-initialized
    database is a no-op. After the DDL runs, a row is recorded in
    ``schema_version`` (also ``OR IGNORE`` so re-application is
    silent).

    NOTE: ``executescript`` issues its own COMMIT before running and
    after finishing, so we cannot wrap it in our own BEGIN/COMMIT —
    sqlite3 will raise "cannot commit - no transaction is active" on
    the explicit COMMIT. Partial-failure rollback within
    executescript is handled by sqlite itself: any error mid-script
    raises and leaves the database in a consistent state because each
    CREATE / INSERT is its own transaction at the sqlite level.

    Parameters
    ----------
    conn:
        Open sqlite3 Connection (preferably one from
        ``core.db.db_connect``).
    version:
        Schema version to apply. Defaults to
        ``CURRENT_SCHEMA_VERSION``.
    description:
        Optional human-readable description recorded in the
        ``schema_version`` row.
    """
    sql_text = schema_path(version).read_text(encoding="utf-8")
    conn.executescript(sql_text)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version "
        "(version, applied_at, description, status) "
        "VALUES (?, ?, ?, 'applied')",
        (
            version,
            int(time.time()),
            description or f"baseline v{version}",
        ),
    )


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version in the database, or
    0.

    Returns 0 if the schema_version table doesn't exist yet (fresh
    DB) or if no successful migration has been recorded.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    result = conn.execute(
        "SELECT MAX(version) FROM schema_version WHERE status='applied'"
    ).fetchone()
    return result[0] or 0
