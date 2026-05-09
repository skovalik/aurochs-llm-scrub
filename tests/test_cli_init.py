"""Tests for ``llm-scrub init``.

* Creates runs.db with the expected schema.
* Creates custom-rules.toml + exemptions.toml + config.toml + manifest.json.
* Idempotent on re-run (no errors, files preserved).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], env_extra: dict[str, str] | None = None,
         input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "aurochs_llm_scrub.cli.main", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        input=input_text,
        timeout=60,
    )


def test_init_creates_db(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    proc = _run(["init", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    for expected in (
        "rule_packs",
        "rules",
        "scrub_runs",
        "findings",
        "rule_metrics",
        "disabled_rules_log",
        "schema_version",
    ):
        assert expected in names, f"missing table: {expected}"


def test_init_creates_sidecar_files(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    proc = _run(["init", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    for name in ("custom-rules.toml", "exemptions.toml", "config.toml",
                 "manifest.json"):
        assert (tmp_path / name).exists(), f"missing sidecar: {name}"


def test_init_writes_manifest_with_schema_version(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    proc = _run(["init", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert "schema_version" in manifest
    assert manifest["schema_version_int"] >= 1


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    p1 = _run(["init", "--db-path", str(db)])
    assert p1.returncode == 0
    # Touch custom-rules.toml with content; idempotent re-run must NOT clobber.
    rules = tmp_path / "custom-rules.toml"
    rules.write_text("# user content\n", encoding="utf-8")
    p2 = _run(["init", "--db-path", str(db)])
    assert p2.returncode == 0, p2.stderr
    assert rules.read_text(encoding="utf-8") == "# user content\n", \
        "custom-rules.toml must be preserved on re-init"


def test_init_creates_layout_subdirs(tmp_path: Path) -> None:
    db = tmp_path / "runs.db"
    proc = _run(["init", "--db-path", str(db)])
    assert proc.returncode == 0, proc.stderr
    for sub in ("tmp", "locks"):
        assert (tmp_path / sub).is_dir(), f"missing subdir: {sub}"


def test_help_exits_clean() -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    for cmd in ("init", "scan", "scrub", "rules", "runs", "verify",
                "status", "install-precommit", "version"):
        assert cmd in proc.stdout, f"missing help entry for {cmd!r}"


def test_version_flag() -> None:
    proc = _run(["--version"])
    assert proc.returncode == 0
    assert "llm-scrub" in proc.stdout.lower()


def test_version_subcommand() -> None:
    proc = _run(["version"])
    assert proc.returncode == 0
    assert "llm-scrub" in proc.stdout.lower()


def test_no_args_prints_help() -> None:
    proc = _run([])
    assert proc.returncode == 0
    assert "llm-scrub" in proc.stdout.lower()
