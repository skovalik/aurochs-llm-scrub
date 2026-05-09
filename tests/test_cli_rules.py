"""Tests for ``llm-scrub rules``.

Pipeline-independent: writes directly through the spine. The rule
registry tables (``rule_packs``, ``rules``, ``disabled_rules_log``) are
populated synthetically here so the CLI's enable/disable/list/show
behavior can be exercised without the real rule pack module.
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


def _seed_pack_and_rules(db: Path, *, pack_name: str = "default",
                         rules: list[dict] | None = None) -> int:
    """Insert a pack + rules directly so we can test list/show/enable/disable.

    Uses INSERT OR IGNORE on the pack so this co-exists with the bundled
    default pack that ``llm-scrub init`` registers — the test fixtures
    operate on the same ``default`` pack identity, just with synthetic
    rules layered on top via INSERT OR IGNORE on ``(pack_id, rule_id)``.

    Returns the resolved pack id.
    """
    if rules is None:
        rules = [
            {
                "rule_id": "p0-em-dash",
                "name": "Em dash",
                "pattern": "—",
                "severity_default": "P0",
                "language": None,
                "description": "Mechanical em-dash detector.",
                "enabled": 1,
            },
            {
                "rule_id": "p1-pronoun-we",
                "name": "We pronoun",
                "pattern": r"\bwe\b",
                "severity_default": "P1",
                "language": "en",
                "description": "First-person plural in non-quote contexts.",
                "enabled": 1,
            },
        ]
    sha = hashlib.sha256(b"synthetic").hexdigest()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT OR IGNORE INTO rule_packs (name, version, schema_version, "
            "  source_path, sha256, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pack_name, "0.1.0", "0.1.0", None, sha, int(time.time())),
        )
        pack_row = conn.execute(
            "SELECT id FROM rule_packs WHERE name = ? AND version = '0.1.0'",
            (pack_name,),
        ).fetchone()
        assert pack_row is not None
        pack_id = int(pack_row["id"])
        for r in rules:
            conn.execute(
                "INSERT OR IGNORE INTO rules (pack_id, rule_id, name, pattern, "
                "  severity_default, language, description, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pack_id,
                    r["rule_id"],
                    r["name"],
                    r["pattern"],
                    r["severity_default"],
                    r["language"],
                    r["description"],
                    r["enabled"],
                ),
            )
        conn.commit()
        return pack_id
    finally:
        conn.close()


def test_rules_help() -> None:
    proc = _run(["rules", "--help"])
    assert proc.returncode == 0
    for sub in ("list", "show", "migrate", "enable", "disable"):
        assert sub in proc.stdout


def test_rules_list_after_init_lists_default_pack(tmp_path: Path) -> None:
    """``init`` registers the bundled default pack; ``rules list`` should surface it.

    Renamed from ``test_rules_list_empty`` after CL4 — ``init`` now
    seeds the default pack so a fresh DB always has the canonical
    ruleset registered. Verifies the default pack's signature rules
    (em dash, sycophantic-opener) are present.
    """
    db = _init_db(tmp_path)
    proc = _run(["rules", "--db-path", str(db), "list"])
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "p0-em-dash" in out
    assert "default" in out


def test_rules_list_after_seed(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    proc = _run(["rules", "--db-path", str(db), "list"])
    assert proc.returncode == 0, proc.stderr
    assert "p0-em-dash" in proc.stdout
    assert "p1-pronoun-we" in proc.stdout
    assert "default" in proc.stdout


def test_rules_list_json(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    proc = _run(["rules", "--db-path", str(db), "list", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert isinstance(data, list)
    assert any(r["rule_id"] == "p0-em-dash" for r in data)


def test_rules_show_existing(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    proc = _run(["rules", "--db-path", str(db), "show", "p0-em-dash"])
    assert proc.returncode == 0, proc.stderr
    assert "p0-em-dash" in proc.stdout
    assert "P0" in proc.stdout


def test_rules_show_missing(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    proc = _run(["rules", "--db-path", str(db), "show", "no-such-rule"])
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_rules_disable_then_list_disabled(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    p = _run(["rules", "--db-path", str(db), "disable", "p0-em-dash",
              "--reason", "test"])
    assert p.returncode == 0, p.stderr
    listing = _run(["rules", "--db-path", str(db), "list", "--disabled"])
    assert listing.returncode == 0
    assert "p0-em-dash" in listing.stdout
    # Audit row recorded
    conn = sqlite3.connect(str(db))
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM disabled_rules_log WHERE rule_id = ?",
            ("p0-em-dash",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 1


def test_rules_enable_after_disable(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    _run(["rules", "--db-path", str(db), "disable", "p0-em-dash"])
    p = _run(["rules", "--db-path", str(db), "enable", "p0-em-dash"])
    assert p.returncode == 0, p.stderr
    # Re-enabled rule shows back in the active set.
    listing = _run(["rules", "--db-path", str(db), "list"])
    assert "p0-em-dash" in listing.stdout
    assert "active" in listing.stdout


def test_rules_disable_unknown_rule(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db)
    p = _run(["rules", "--db-path", str(db), "disable", "no-such-rule"])
    assert p.returncode == 2, p.stdout + p.stderr


def test_rules_show_ambiguous_requires_pack(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    _seed_pack_and_rules(db, pack_name="default")
    _seed_pack_and_rules(db, pack_name="custom",
                         rules=[{
                             "rule_id": "p0-em-dash",
                             "name": "Em dash (custom)",
                             "pattern": "—",
                             "severity_default": "P0",
                             "language": None,
                             "description": "Custom override.",
                             "enabled": 1,
                         }])
    p = _run(["rules", "--db-path", str(db), "show", "p0-em-dash"])
    assert p.returncode == 2, p.stdout + p.stderr
    assert "multiple packs" in p.stderr.lower()
