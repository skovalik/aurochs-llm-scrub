"""Tests for ``llm-scrub runs``.

Pipeline-independent: writes synthetic ``scrub_runs`` and ``findings``
rows directly through the spine to exercise list/show/diff handlers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
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


def _seed_pack_with_rule(db: Path) -> int:
    """Seed a single rule and return its sqlite PK.

    Uses INSERT OR IGNORE on the pack so this co-exists with the
    bundled default pack that ``llm-scrub init`` registers — the test
    just wants ANY rule row to FK against, and the default pack's
    ``p0-em-dash`` rule already exists post-init. We read the rule's
    PK back from the rules table for the tests that need it.
    """
    sha = hashlib.sha256(b"runs-test-pack").hexdigest()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT OR IGNORE INTO rule_packs (name, version, schema_version, "
            "  source_path, sha256, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("default", "0.1.0", "0.1.0", None, sha, int(time.time())),
        )
        pack_row = conn.execute(
            "SELECT id FROM rule_packs WHERE name = 'default' AND version = '0.1.0'"
        ).fetchone()
        assert pack_row is not None
        pack_id = int(pack_row["id"])
        conn.execute(
            "INSERT OR IGNORE INTO rules (pack_id, rule_id, name, pattern, "
            "  severity_default, language, description, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pack_id, "p0-em-dash", "Em dash", "—", "P0", None,
             "synthetic", 1),
        )
        rule_row = conn.execute(
            "SELECT id FROM rules WHERE pack_id = ? AND rule_id = 'p0-em-dash'",
            (pack_id,),
        ).fetchone()
        assert rule_row is not None
        conn.commit()
        return int(rule_row["id"])
    finally:
        conn.close()


def _seed_run(
    db: Path,
    *,
    rule_pk: int,
    target: str,
    severity: str = "P0",
    line_number: int = 1,
    snippet: str = "em dash here",
    file_offset: int = 100,
    length: int = 1,
) -> int:
    """Insert one scrub_runs row + one findings row, return run_id."""
    now = int(time.time())
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO scrub_runs (started_at, ended_at, status, "
            "  target_path, mode, total_findings, total_redacted) "
            "VALUES (?, ?, 'success', ?, 'scan', 1, 0)",
            (now, now + 1, target),
        )
        run_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO findings (run_id, rule_id, file_offset, length, "
            "  file_path, line_number, severity, snippet) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, rule_pk, file_offset, length, target, line_number,
             severity, snippet),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


def test_runs_help() -> None:
    proc = _run(["runs", "--help"])
    assert proc.returncode == 0
    for sub in ("list", "show", "diff"):
        assert sub in proc.stdout


def test_runs_list_empty(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    proc = _run(["runs", "--db-path", str(db), "list"])
    assert proc.returncode == 0, proc.stderr
    assert "no runs" in proc.stdout.lower()


def test_runs_list_after_seed(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    rule_pk = _seed_pack_with_rule(db)
    run_id = _seed_run(db, rule_pk=rule_pk, target="draft.md")
    proc = _run(["runs", "--db-path", str(db), "list"])
    assert proc.returncode == 0, proc.stderr
    assert str(run_id) in proc.stdout
    assert "draft.md" in proc.stdout


def test_runs_list_json(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    rule_pk = _seed_pack_with_rule(db)
    _seed_run(db, rule_pk=rule_pk, target="a.md")
    proc = _run(["runs", "--db-path", str(db), "list", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["target_path"] == "a.md"


def test_runs_show_missing(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    proc = _run(["runs", "--db-path", str(db), "show", "999"])
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_runs_show_existing(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    rule_pk = _seed_pack_with_rule(db)
    run_id = _seed_run(db, rule_pk=rule_pk, target="x.md")
    proc = _run(["runs", "--db-path", str(db), "show", str(run_id)])
    assert proc.returncode == 0, proc.stderr
    assert f"#{run_id}" in proc.stdout
    assert "x.md" in proc.stdout
    assert "P0" in proc.stdout


def test_runs_diff_resolved_and_regressed(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    rule_pk = _seed_pack_with_rule(db)
    run_a = _seed_run(db, rule_pk=rule_pk, target="x.md", line_number=5,
                      snippet="—", file_offset=50)
    run_b = _seed_run(db, rule_pk=rule_pk, target="x.md", line_number=42,
                      snippet="—", file_offset=200)
    proc = _run(["runs", "--db-path", str(db), "diff",
                 str(run_a), str(run_b), "--json"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["a"]["id"] == run_a
    assert payload["b"]["id"] == run_b
    # Different line numbers → unique on either side
    assert len(payload["only_in_a"]) == 1
    assert len(payload["only_in_b"]) == 1
