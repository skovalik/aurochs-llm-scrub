"""Unit tests for core.recovery — verify quick + deep paths."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aurochs_llm_scrub.core import db_connect
from aurochs_llm_scrub.core.db import MigrationRunner
from aurochs_llm_scrub.core.recovery import VerifyReport, verify
from aurochs_llm_scrub.core.rule_pack import register_default_pack


@pytest.fixture
def runs_db(tmp_path: Path) -> Path:
    db = tmp_path / "runs.db"
    MigrationRunner(db).run()
    register_default_pack(db)
    return db


# ---------------------------------------------------------------------------
# Missing DB
# ---------------------------------------------------------------------------


class TestMissingDb:
    def test_returns_error(self, tmp_path: Path) -> None:
        report = verify(tmp_path / "nope.db")
        assert not report.ok
        assert any("not found" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Quick mode
# ---------------------------------------------------------------------------


class TestQuickMode:
    def test_clean_db_ok(self, runs_db: Path) -> None:
        report = verify(runs_db)
        assert isinstance(report, VerifyReport)
        assert report.ok
        assert report.errors == []
        assert report.orphan_runs == 0
        assert report.deep is False

    def test_orphan_running_run_warns(self, runs_db: Path) -> None:
        # Insert a 'running' scrub_run with started_at well in the past.
        conn = db_connect(runs_db)
        try:
            conn.execute(
                "INSERT INTO scrub_runs "
                "(started_at, status, target_path, mode) "
                "VALUES (?, ?, ?, ?)",
                (
                    int(time.time()) - 7200,  # 2 hours ago
                    "running",
                    "stale.md",
                    "scan",
                ),
            )
        finally:
            conn.close()

        report = verify(runs_db)
        # Warning, not error; ok is still True per voice's pattern.
        assert report.orphan_runs == 1
        assert any("running" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Deep mode
# ---------------------------------------------------------------------------


class TestDeepMode:
    def test_deep_clean_db_ok(self, runs_db: Path) -> None:
        report = verify(runs_db, deep=True)
        assert report.deep is True
        assert report.orphan_findings == 0
        assert report.dangling_rule_metrics == 0
        assert report.ok

    def test_deep_dangling_metrics_warns(self, runs_db: Path) -> None:
        # Insert a rule_metrics row whose run_id has no matching
        # scrub_runs row. We can't normally do this (FK fires), but we
        # can disable FKs for the insertion to simulate a previously-
        # cascaded delete that left an artifact.
        conn = db_connect(runs_db)
        try:
            # Need a real rule_id (FK to rules). Use the first rule.
            rid = conn.execute(
                "SELECT id FROM rules LIMIT 1"
            ).fetchone()[0]
            # Disable FKs to insert orphan.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO rule_metrics "
                "(rule_id, run_id, hit_count) VALUES (?, ?, ?)",
                (rid, 999_999, 0),
            )
            conn.execute("PRAGMA foreign_keys = ON")
        finally:
            conn.close()

        report = verify(runs_db, deep=True)
        assert report.dangling_rule_metrics == 1


# ---------------------------------------------------------------------------
# Iteration / __bool__
# ---------------------------------------------------------------------------


class TestIteration:
    def test_clean_report_iterates_empty(self, runs_db: Path) -> None:
        report = verify(runs_db)
        assert list(report) == []
        assert bool(report) is False
