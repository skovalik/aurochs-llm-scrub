"""Tests for ``llm-scrub scrub``.

Like scan, the pipeline lands in parallel; these tests exercise parser
shape + the soft-failure path.
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


def test_scrub_help() -> None:
    proc = _run(["scrub", "--help"])
    assert proc.returncode == 0
    assert "--in-place" in proc.stdout
    assert "--to-stdout" in proc.stdout
    assert "--backup-mode" in proc.stdout


def test_scrub_in_place_and_to_stdout_mutually_exclusive(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("hi\n", encoding="utf-8")
    proc = _run(["scrub", str(target), "--db-path", str(db),
                 "--in-place", "--to-stdout"])
    assert proc.returncode == 2
    assert "not allowed with" in proc.stderr.lower() \
        or "argument" in proc.stderr.lower()


def test_scrub_no_backup_requires_in_place(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("hi\n", encoding="utf-8")
    proc = _run(["scrub", str(target), "--db-path", str(db),
                 "--no-backup", "--to-stdout"])
    assert proc.returncode == 2
    assert "no-backup" in proc.stderr.lower() or "in-place" in proc.stderr.lower()


def test_scrub_backup_mode_choices(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("hi\n", encoding="utf-8")
    proc = _run(["scrub", str(target), "--db-path", str(db),
                 "--backup-mode", "lol"])
    assert proc.returncode == 2
    assert "lol" in proc.stderr.lower() or "invalid choice" in proc.stderr.lower()


def test_scrub_pipeline_unwired_returns_clear_error(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    target = tmp_path / "draft.md"
    target.write_text("text\n", encoding="utf-8")
    proc = _run(["scrub", str(target), "--db-path", str(db)])
    if proc.returncode == 1:
        assert "scrubber" in proc.stderr.lower() or "pipeline" in proc.stderr.lower()
    else:
        assert proc.returncode == 0, \
            f"unexpected exit {proc.returncode}: {proc.stderr}"
