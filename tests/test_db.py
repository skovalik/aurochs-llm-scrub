"""Unit tests for core.db — connection pragmas + MigrationRunner contract."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aurochs_llm_scrub.core.db import (
    MigrationError,
    MigrationRunner,
    db_connect,
)
from aurochs_llm_scrub.core.schema import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    apply_schema,
    current_schema_version,
)

# ---------------------------------------------------------------------------
# db_connect — pragma contract
# ---------------------------------------------------------------------------


class TestDbConnect:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        # A fresh install should create
        # ~/.aurochs-llm-scrub/runs.db parent without the user
        # mkdir'ing it first.
        nested = tmp_path / "nested" / "deeply" / "runs.db"
        conn = db_connect(nested)
        try:
            assert nested.parent.exists()
            assert nested.exists()
        finally:
            conn.close()

    def test_foreign_keys_pragma_on(self, tmp_path: Path) -> None:
        # Plan v4 BLOCKER #14 — FKs must be enforced per-connection.
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert result == 1, (
                "PRAGMA foreign_keys must be ON every connection"
            )
        finally:
            conn.close()

    def test_journal_mode_wal(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            result = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert result.lower() == "wal"
        finally:
            conn.close()

    def test_busy_timeout_30s(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            result = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert result == 30000
        finally:
            conn.close()

    def test_synchronous_normal(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            # NORMAL = 1 in PRAGMA result.
            result = conn.execute("PRAGMA synchronous").fetchone()[0]
            assert result == 1
        finally:
            conn.close()

    def test_row_factory(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            assert conn.row_factory is sqlite3.Row
        finally:
            conn.close()

    def test_memory_db(self) -> None:
        conn = db_connect(":memory:")
        try:
            result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert result == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# FK enforcement (smoke test through schema)
# ---------------------------------------------------------------------------


class TestFkEnforcement:
    def test_findings_fk_on_run_enforces(self, tmp_path: Path) -> None:
        """FK violation: inserting a finding with a non-existent
        run_id raises IntegrityError."""
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            # findings.run_id FK references scrub_runs(id) — inserting
            # a non-existent run_id must fail. Need to provide a valid
            # rule_id too — both FKs are checked.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO findings "
                    "(run_id, rule_id, file_offset, length, file_path, "
                    "line_number, severity, snippet) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (999_999, 999_999, 0, 1, "f.md", 1, "P0", "x"),
                )
        finally:
            conn.close()

    def test_findings_cascade_on_run_delete(self, tmp_path: Path) -> None:
        """ON DELETE CASCADE: deleting a scrub_run removes its
        findings."""
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            # Set up: pack -> rule -> run -> finding chain.
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            pack_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pack_id, "p0-em-dash", "Em dash", "—", "P0", "desc"),
            )
            rule_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO scrub_runs "
                "(started_at, status, target_path, mode) "
                "VALUES (?, ?, ?, ?)",
                (0, "success", "f.md", "scan"),
            )
            run_id = cur.lastrowid
            conn.execute(
                "INSERT INTO findings "
                "(run_id, rule_id, file_offset, length, file_path, "
                "line_number, severity, snippet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, rule_id, 0, 1, "f.md", 1, "P0", "—"),
            )

            # Delete the run — the finding should cascade.
            conn.execute(
                "DELETE FROM scrub_runs WHERE id = ?", (run_id,)
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_rule_pack_cascade_on_pack_delete(self, tmp_path: Path) -> None:
        """ON DELETE CASCADE: deleting a rule_pack removes its
        rules."""
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("custom", "0.0.1", "b" * 64, 0),
            )
            pack_id = cur.lastrowid
            conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pack_id, "custom-1", "Custom", "x", "P1", "desc"),
            )

            conn.execute(
                "DELETE FROM rule_packs WHERE id = ?", (pack_id,)
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE pack_id = ?",
                (pack_id,),
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_rejects_invalid_scrub_status(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO scrub_runs "
                    "(started_at, status, target_path, mode) "
                    "VALUES (?, ?, ?, ?)",
                    (0, "bogus_status", "f.md", "scan"),
                )
        finally:
            conn.close()

    def test_rejects_invalid_scrub_mode(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO scrub_runs "
                    "(started_at, status, target_path, mode) "
                    "VALUES (?, ?, ?, ?)",
                    (0, "success", "f.md", "bogus_mode"),
                )
        finally:
            conn.close()

    def test_rejects_invalid_severity(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            # Set up the FK chain so the only constraint that fires is
            # the severity CHECK.
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            pack_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pack_id, "r", "n", "p", "P0", "d"),
            )
            rule_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO scrub_runs "
                "(started_at, status, target_path, mode) "
                "VALUES (?, ?, ?, ?)",
                (0, "success", "f.md", "scan"),
            )
            run_id = cur.lastrowid

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO findings "
                    "(run_id, rule_id, file_offset, length, file_path, "
                    "line_number, severity, snippet) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, rule_id, 0, 1, "f.md", 1, "BOGUS", "x"),
                )
        finally:
            conn.close()

    def test_rejects_short_pack_sha256(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rule_packs "
                    "(name, version, sha256, registered_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("default", "0.1.0", "tooshort", 0),
                )
        finally:
            conn.close()

    def test_rejects_invalid_rule_severity(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            pack_id = cur.lastrowid
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rules "
                    "(pack_id, rule_id, name, pattern, severity_default, "
                    "description) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pack_id, "r", "n", "p", "P9", "d"),
                )
        finally:
            conn.close()

    def test_rule_metrics_fp_lte_hits(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            pack_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pack_id, "r", "n", "p", "P0", "d"),
            )
            rule_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO scrub_runs "
                "(started_at, status, target_path, mode) "
                "VALUES (?, ?, ?, ?)",
                (0, "success", "f.md", "scan"),
            )
            run_id = cur.lastrowid

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rule_metrics "
                    "(rule_id, run_id, hit_count, false_positive_count) "
                    "VALUES (?, ?, ?, ?)",
                    (rule_id, run_id, 5, 10),
                )
        finally:
            conn.close()

    def test_rule_packs_unique_name_version(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rule_packs "
                    "(name, version, sha256, registered_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("default", "0.1.0", "b" * 64, 0),
                )
        finally:
            conn.close()

    def test_rules_unique_pack_rule_id(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        conn = db_connect(db)
        try:
            apply_schema(conn)
            cur = conn.execute(
                "INSERT INTO rule_packs "
                "(name, version, sha256, registered_at) "
                "VALUES (?, ?, ?, ?)",
                ("default", "0.1.0", "a" * 64, 0),
            )
            pack_id = cur.lastrowid
            conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pack_id, "p0-em-dash", "n", "p", "P0", "d"),
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO rules "
                    "(pack_id, rule_id, name, pattern, severity_default, "
                    "description) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pack_id, "p0-em-dash", "n2", "p2", "P0", "d2"),
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# MigrationRunner — baseline + 0001 apply
# ---------------------------------------------------------------------------


class TestMigrationRunner:
    def test_run_on_fresh_db_applies_v1(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        runner = MigrationRunner(db)
        result = runner.run()
        assert result == 1

        conn = db_connect(db)
        try:
            assert current_schema_version(conn) == 1
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for required in (
                "rule_packs",
                "rules",
                "scrub_runs",
                "findings",
                "rule_metrics",
                "disabled_rules_log",
                "schema_version",
            ):
                assert required in tables, f"missing table: {required}"
        finally:
            conn.close()

    def test_run_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        first = MigrationRunner(db).run()
        second = MigrationRunner(db).run()
        third = MigrationRunner(db).run()
        assert first == second == third == 1

        conn = db_connect(db)
        try:
            rows = conn.execute(
                "SELECT version FROM schema_version WHERE status='applied'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_baseline_from_existing(self, tmp_path: Path) -> None:
        """A db with schema but no schema_version table can be
        baselined."""
        db = tmp_path / "external.db"
        # Build a db with a partial schema but NO schema_version row.
        conn = db_connect(db)
        try:
            conn.executescript(
                """
                CREATE TABLE rule_packs (
                  id INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  schema_version TEXT NOT NULL DEFAULT '0.1.0',
                  source_path TEXT,
                  sha256 TEXT NOT NULL,
                  registered_at INTEGER NOT NULL
                );
                """
            )
        finally:
            conn.close()

        # Without baseline_from_existing, the runner would try to run
        # 0001_initial.sql on the existing DB. Because we use IF NOT
        # EXISTS in the DDL the run is still safe — but the user may
        # have mismatched schema, so the explicit baseline path is
        # the contract.
        runner = MigrationRunner(db, baseline_from_existing=True)
        version = runner.run()
        assert version == 1

        conn = db_connect(db)
        try:
            row = conn.execute(
                "SELECT version, description FROM schema_version "
                "WHERE status='applied'"
            ).fetchone()
            assert row is not None
            assert row["version"] == 1
            assert row["description"] == "baseline-from-existing"
        finally:
            conn.close()

    def test_partial_state_detection(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        # Apply v1 cleanly.
        MigrationRunner(db).run()
        # Force an in_progress row for a hypothetical v2.
        conn = db_connect(db)
        try:
            conn.execute(
                "INSERT INTO schema_version "
                "(version, applied_at, description, status) "
                "VALUES (?, ?, ?, ?)",
                (2, 0, "test-partial", "in_progress"),
            )
        finally:
            conn.close()

        runner = MigrationRunner(db)
        with pytest.raises(MigrationError, match="partial migration"):
            runner.run()

    def test_no_migrations_dir(self, tmp_path: Path) -> None:
        db = tmp_path / "runs.db"
        empty_dir = tmp_path / "empty_migrations"
        empty_dir.mkdir()
        runner = MigrationRunner(db, migrations_dir=empty_dir)
        with pytest.raises(MigrationError, match="No migration files"):
            runner.run()


# ---------------------------------------------------------------------------
# Schema version constants
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_semver_string(self) -> None:
        assert SCHEMA_VERSION == "0.1.0"

    def test_int_version(self) -> None:
        assert CURRENT_SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Spine surface (re-exports work)
# ---------------------------------------------------------------------------


class TestSpinePublicApi:
    def test_imports_from_core(self) -> None:
        # The spine __init__ re-exports the surface that pipeline +
        # CLI + MCP will import. Sanity-check the names resolve.
        from aurochs_llm_scrub.core import (
            HASH_INPUT_VERSION,
            DisabledRuleEntry,
            Finding,
            LockError,
            MigrateLock,
            MigrationError,
            MigrationRunner,
            Rule,
            RuleMetrics,
            RulePack,
            ScrubMode,
            ScrubRun,
            ScrubStatus,
            WriteLock,
            compute_finding_sha256,
            db_connect,
            normalize_text,
        )

        # If any of these are missing the import would have failed;
        # tap-test by referencing each.
        assert DisabledRuleEntry is not None
        assert Finding is not None
        assert HASH_INPUT_VERSION == 1
        assert LockError is not None
        assert MigrateLock is not None
        assert MigrationError is not None
        assert MigrationRunner is not None
        assert Rule is not None
        assert RuleMetrics is not None
        assert RulePack is not None
        assert ScrubMode is not None
        assert ScrubRun is not None
        assert ScrubStatus is not None
        assert WriteLock is not None
        assert callable(compute_finding_sha256)
        assert callable(db_connect)
        assert callable(normalize_text)
