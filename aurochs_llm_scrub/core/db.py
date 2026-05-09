"""SQLite migration runner (connection helper imported from aurochs-core).

The connection helper ``db_connect()`` is now provided by
``aurochs-core``; see ``aurochs_core.db`` for the canonical pragma
contract (``foreign_keys=ON``, ``journal_mode=WAL``,
``synchronous=NORMAL``, ``busy_timeout=30000``, ``isolation_level=None``).
We re-export it for backwards compatibility with existing
``from aurochs_llm_scrub.core.db import db_connect`` import sites.

The runner is a class (``MigrationRunner``) rather than a free function
because llm-scrub's migration story carries the
``--baseline-from-existing`` decision, holds the ``MigrateLock``
across multiple statements, and may need to be reused for the same db
path inside one CLI invocation (e.g. ``llm-scrub init`` followed by
``llm-scrub scan`` in the same process). Migration logic stays
plugin-local because each plugin owns its own ``schema_version`` table
+ migration files.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from pathlib import Path
from types import TracebackType

from aurochs_core import MigrateLock, db_connect

# Conservative lock timeout — the migration runner shouldn't block
# forever.
_DEFAULT_LOCK_TIMEOUT_SECONDS: float = 60.0

_VERSION_RE: re.Pattern[str] = re.compile(r"^(?P<v>\d{4})_")
_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"

__all__ = [
    "MigrationError",
    "MigrationRunner",
    "db_connect",
]


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """Raised on out-of-order migrations or partial-state detection."""


class MigrationRunner:
    """Sequential, lock-aware migration runner.

    Plan v4 contract (BLOCKER #13 — recall pattern inherited):

    1. Acquire ``MigrateLock`` (fail fast if held by another process;
       wait up to ``timeout`` seconds for stale locks to clear).
    2. ``BEGIN EXCLUSIVE`` transaction wrapping each migration apply.
    3. Verify the migration version is exactly ``current_version + 1``
       (no skips).
    4. Insert ``schema_version`` row with status ``in_progress``.
    5. Apply each statement.
    6. On success, flip status to ``applied`` and commit.
    7. On failure, ``ROLLBACK`` — schema_version stays at
       ``in_progress`` so the next run can detect partial state and
       refuse to proceed.

    The ``--baseline-from-existing`` flag handles the case where a
    runs.db file exists with schema (manually created, copied from
    another machine) but no schema_version table. In that mode,
    applying 0001 stamps schema_version=1 without re-running the DDL
    (assumes the existing schema matches v0.1.0; mismatch is the
    user's responsibility — they can ``llm-scrub doctor`` to
    validate).

    Usage::

        runner = MigrationRunner(db_path)
        version = runner.run()  # returns the version after migration

        # baseline an existing schema-but-no-schema_version DB:
        runner = MigrationRunner(db_path, baseline_from_existing=True)
        version = runner.run()
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        target: int | None = None,
        baseline_from_existing: bool = False,
        timeout: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
        migrations_dir: Path | None = None,
    ) -> None:
        """Construct a runner.

        Parameters
        ----------
        db_path:
            Path to runs.db.
        target:
            Migration version to stop at. ``None`` (default) = latest
            available.
        baseline_from_existing:
            If True and the db file has tables but no schema_version
            table, stamp schema_version=1 instead of running DDL. Use
            ONLY for external dbs (e.g. restored from backup); fresh
            installs should leave this False.
        timeout:
            Seconds to wait for ``MigrateLock``. Default 60s.
        migrations_dir:
            Override migration source directory (test injection
            point).
        """
        self._is_memory: bool = isinstance(db_path, str) and db_path == ":memory:"
        # NB: lockfile lives next to db_path; for in-memory tests the
        # lockfile lives next to a sentinel name so MigrateLock can
        # still construct. We don't actually exercise the lock for
        # ":memory:" because in-memory dbs are per-process by
        # definition.
        self.db_path: Path = (
            Path("./runs.db") if self._is_memory else Path(db_path)
        )
        self._raw_db_arg: Path | str = db_path
        self.target: int | None = target
        self.baseline_from_existing: bool = baseline_from_existing
        self.timeout: float = float(timeout)
        self.migrations_dir: Path = migrations_dir or _MIGRATIONS_DIR

    # ----- public API ----------------------------------------------------

    def run(self) -> int:
        """Apply pending migrations up to ``target`` (or latest).

        Returns the version the database is at after this call.
        Acquires ``MigrateLock`` for the duration on disk-backed dbs;
        in-memory dbs skip the lock (single-process by definition).
        """
        available = self._discover_migrations()
        if not available:
            raise MigrationError(
                f"No migration files found in {self.migrations_dir}"
            )

        latest_available = max(v for v, _ in available)
        target_version = (
            self.target if self.target is not None else latest_available
        )

        if self._is_memory:
            return self._run_unlocked(available, target_version)

        with MigrateLock(self.db_path, timeout=self.timeout):
            return self._run_unlocked(available, target_version)

    # ----- internals -----------------------------------------------------

    def _run_unlocked(
        self,
        available: list[tuple[int, Path]],
        target_version: int,
    ) -> int:
        conn = db_connect(self._raw_db_arg)
        try:
            # Detect partial state FIRST. An in_progress row from a
            # prior interrupted run means the DB is in an unknown
            # state — refuse loud regardless of whether we'd
            # otherwise be a no-op (the next migration step can't
            # proceed safely either way, and silently no-op'ing past
            # it hides the recovery requirement).
            self._detect_partial_state(conn)

            applied = self._current_version(conn)

            if (
                applied == 0
                and self.baseline_from_existing
                and self._has_user_tables(conn)
            ):
                # Stamp v1 without running DDL.
                self._ensure_schema_version_table(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version "
                    "(version, applied_at, description, status) "
                    "VALUES (?, ?, ?, 'applied')",
                    (
                        1,
                        int(time.time()),
                        "baseline-from-existing",
                    ),
                )
                applied = 1

            if applied >= target_version:
                return applied

            for version, path in available:
                if version <= applied:
                    continue
                if version > target_version:
                    break
                if version != applied + 1:
                    raise MigrationError(
                        f"Out-of-order migration: have v{applied}, "
                        f"expected v{applied + 1}, got v{version}"
                    )
                self._apply_one(conn, version, path)
                applied = version

            return applied
        finally:
            conn.close()

    def _discover_migrations(self) -> list[tuple[int, Path]]:
        """Enumerate migration files, sorted by numeric version
        prefix.

        Files must be named ``NNNN_description.sql`` with NNNN a
        zero-padded integer. Returns ``[(version, path), ...]``
        ascending.
        """
        out: list[tuple[int, Path]] = []
        for sql_file in sorted(self.migrations_dir.glob("*.sql")):
            match = _VERSION_RE.match(sql_file.name)
            if not match:
                continue
            out.append((int(match.group("v")), sql_file))
        return out

    @staticmethod
    def _current_version(conn: sqlite3.Connection) -> int:
        """Return the highest applied schema version, or 0 for fresh
        DB."""
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

    @staticmethod
    def _has_user_tables(conn: sqlite3.Connection) -> bool:
        """Check whether the db has user tables beyond
        schema_version."""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_version'"
        ).fetchall()
        return len(rows) > 0

    @staticmethod
    def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
        """Create schema_version table if missing.

        Used by ``--baseline-from-existing`` path; the normal path
        lets the migration SQL do this.
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at INTEGER NOT NULL,"
            "  description TEXT,"
            "  status TEXT NOT NULL,"
            "  CHECK (status IN ('in_progress', 'applied', 'failed'))"
            ")"
        )

    def _detect_partial_state(self, conn: sqlite3.Connection) -> None:
        """Refuse to proceed if a previous migration stalled
        mid-flight."""
        exists = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if exists is None:
            return
        in_prog = conn.execute(
            "SELECT version FROM schema_version WHERE status='in_progress'"
        ).fetchone()
        if in_prog is not None:
            raise MigrationError(
                f"Detected partial migration at version {in_prog[0]}. "
                "Manual recovery required: inspect schema_version, then "
                "re-run migration after rolling forward or dropping the "
                "partial state."
            )

    def _apply_one(
        self,
        conn: sqlite3.Connection,
        version: int,
        path: Path,
    ) -> None:
        """Apply one migration file under BEGIN EXCLUSIVE.

        For the v1 baseline (when schema_version table doesn't yet
        exist) we run the DDL first — it creates the table — then
        stamp the row. For v2+ we pre-stamp ``in_progress`` so a
        crash leaves a tombstone the next run can detect.
        """
        sql_text = path.read_text(encoding="utf-8")
        is_baseline = (version == 1) and (
            self._current_version(conn) == 0
        )

        if is_baseline:
            # The baseline migration creates schema_version itself;
            # we can't INSERT in_progress before the table exists.
            # Wrap the DDL + the stamp in a single EXCLUSIVE
            # transaction so a crash mid-DDL leaves nothing partial.
            #
            # NB: executescript() in older sqlite3 implementations
            # issues its own COMMIT; we open the EXCLUSIVE transaction
            # AFTER the script if needed. Modern sqlite3 (3.42+)
            # respects an outer transaction, but the suppress keeps
            # us robust to either.
            conn.execute("BEGIN EXCLUSIVE")
            try:
                conn.executescript(sql_text)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_version "
                    "(version, applied_at, description, status) "
                    "VALUES (?, ?, ?, 'applied')",
                    (version, int(time.time()), path.stem),
                )
                # If executescript already committed (older sqlite),
                # the COMMIT here is a no-op that raises; suppress.
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
            return

        # v2+ path
        conn.execute("BEGIN EXCLUSIVE")
        try:
            conn.execute(
                "INSERT INTO schema_version "
                "(version, applied_at, description, status) "
                "VALUES (?, ?, ?, 'in_progress')",
                (version, int(time.time()), path.stem),
            )
            conn.executescript(sql_text)
            conn.execute(
                "UPDATE schema_version SET status='applied' WHERE version=?",
                (version,),
            )
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise

    # ----- context manager (convenience) ---------------------------------

    def __enter__(self) -> MigrationRunner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Nothing to clean up — connections + locks are scoped inside
        # run().
        return None
