"""Tests for ``llm-scrub scan``.

The pipeline (``aurochs_llm_scrub.core.scrubber``) lands in parallel.
These tests exercise CLI parser shape + the soft-failure path so they
go green on the spine alone.
"""

from __future__ import annotations

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


def test_scan_help() -> None:
    proc = _run(["scan", "--help"])
    assert proc.returncode == 0
    assert "--severity" in proc.stdout
    assert "--json" in proc.stdout
    assert "--recursive" in proc.stdout


def test_scan_invalid_severity_rejected(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("hi\n", encoding="utf-8")
    proc = _run(["scan", str(target), "--db-path", str(db), "--severity", "P9"])
    # Either pipeline missing → exit 1, or severity validation → SystemExit 1
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "p9" in combined.lower() or "invalid severity" in combined.lower() \
        or "pipeline not available" in combined.lower()


def test_scan_pipeline_unwired_returns_clear_error(tmp_path: Path) -> None:
    """Until core.scrubber lands, scan exits 1 with a clear stderr message."""
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("text\n", encoding="utf-8")
    proc = _run(["scan", str(target), "--db-path", str(db)])
    # Either: pipeline already wired → 0 or 5; or unwired → 1 with clear msg
    if proc.returncode == 1:
        assert "scrubber" in proc.stderr.lower() or "pipeline" in proc.stderr.lower()
    else:
        assert proc.returncode in (0, 5), \
            f"unexpected exit {proc.returncode}: {proc.stderr}"


def test_scan_output_format_choices(tmp_path: Path) -> None:
    """Argparse should reject unknown --format values."""
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("x\n", encoding="utf-8")
    proc = _run(["scan", str(target), "--db-path", str(db),
                 "--format", "yaml"])
    # Argparse usage error → exit 2
    assert proc.returncode == 2
    assert "yaml" in proc.stderr.lower() or "invalid choice" in proc.stderr.lower()
