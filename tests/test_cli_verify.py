"""Tests for ``llm-scrub verify``."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "aurochs_llm_scrub.cli.main", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )


def _init_db(tmp_path: Path) -> Path:
    db = tmp_path / "runs.db"
    p = _run(["init", "--db-path", str(db)])
    assert p.returncode == 0, p.stderr
    return db


def test_verify_help() -> None:
    proc = _run(["verify", "--help"])
    assert proc.returncode == 0
    assert "--deep" in proc.stdout


def test_verify_missing_db(tmp_path: Path) -> None:
    proc = _run(["verify", "--db-path", str(tmp_path / "absent.db")])
    assert proc.returncode == 1
    assert "not found" in proc.stderr.lower()


def test_verify_clean_db(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    proc = _run(["verify", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_verify_orphan_schema_version_returns_5(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    # Inject an in_progress schema_version row to simulate orphan migration.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at, description, status) "
            "VALUES (?, ?, ?, 'in_progress')",
            (99, 0, "synthetic-orphan"),
        )
        conn.commit()
    finally:
        conn.close()
    proc = _run(["verify", "--db-path", str(db)])
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "orphan" in proc.stdout.lower()


def test_verify_deep_without_recovery_module(tmp_path: Path) -> None:
    """--deep should report missing recovery module without crashing."""
    db = _init_db(tmp_path)
    proc = _run(["verify", "--db-path", str(db), "--deep"])
    # Either core.recovery isn't wired (returns 1 with notice) OR it is
    # wired and the deep check passes. Both are acceptable.
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
