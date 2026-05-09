"""Tests for ``llm-scrub status``."""

from __future__ import annotations

import json
import os
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


def test_status_help() -> None:
    proc = _run(["status", "--help"])
    assert proc.returncode == 0


def test_status_missing_db(tmp_path: Path) -> None:
    proc = _run(["status", "--db-path", str(tmp_path / "absent.db")])
    assert proc.returncode == 1
    # stdout fallback message
    assert "init" in proc.stdout.lower() or "not found" in proc.stdout.lower()


def test_status_missing_db_json(tmp_path: Path) -> None:
    proc = _run(["status", "--db-path", str(tmp_path / "absent.db"), "--json"])
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["runs_db_exists"] is False
    assert "version" in payload


def test_status_text_output(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    proc = _run(["status", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "version" in out.lower()
    assert "runs.db" in out.lower()
    assert "schema" in out.lower()
    assert "rules" in out.lower()


def test_status_json_emits_valid_json(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    proc = _run(["status", "--db-path", str(db), "--json"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    for key in (
        "version",
        "runs_db_path",
        "runs_db_exists",
        "runs_db_bytes",
        "schema_version",
        "rule_packs_total",
        "rules_total",
        "rules_enabled",
        "rules_disabled",
        "scrub_runs_total",
        "disabled_log_total",
        "last_run",
    ):
        assert key in payload, f"missing key in JSON status: {key}"
    assert payload["runs_db_exists"] is True
    assert payload["scrub_runs_total"] == 0
    assert payload["last_run"] is None
