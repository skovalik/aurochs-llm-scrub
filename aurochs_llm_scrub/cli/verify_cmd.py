"""``llm-scrub verify`` — integrity checks on runs.db.

Wraps an optional ``core.recovery.verify`` (pipeline-supplied). The CLI
runs the basic FK + finding-format checks itself if recovery isn't
wired; ``--deep`` adds the WAL walk + content-hash checks once recovery
lands.

Exit codes:
* ``0`` — clean.
* ``1`` — issues found (printed to stdout).
* ``5`` — orphan migration state detected.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from aurochs_llm_scrub.cli.paths import add_db_path_arg, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("verify", help="Run integrity checks on runs.db.")
    p.add_argument("--deep", action="store_true",
                   help="Add WAL walk + content_hash checks (requires core.recovery).")
    add_db_path_arg(p)
    p.set_defaults(_handler=run)


def run(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not db_path.exists():
        print(f"llm-scrub: runs.db not found at {db_path}.", file=sys.stderr)
        return 1

    issues: list[str] = []

    # Always-on basic checks (don't depend on the pipeline).
    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        # FK violations
        try:
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            for row in fk_rows:
                issues.append(
                    f"FK violation: table={row[0]} rowid={row[1]} parent={row[2]}"
                )
        except sqlite3.OperationalError as e:
            issues.append(f"FK check failed: {e}")

        # Finding snippets must be non-empty
        try:
            empty_snippets = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE LENGTH(snippet) = 0"
            ).fetchone()[0]
            if empty_snippets:
                issues.append(f"{empty_snippets} finding rows have empty snippet")
        except sqlite3.OperationalError as e:
            issues.append(f"finding snippet check failed: {e}")

        # Rule-pack sha256 format (64 hex chars per CHECK constraint)
        try:
            short = conn.execute(
                "SELECT COUNT(*) FROM rule_packs WHERE LENGTH(sha256) != 64"
            ).fetchone()[0]
            if short:
                issues.append(f"{short} rule_pack rows have non-SHA256 sha256")
        except sqlite3.OperationalError as e:
            issues.append(f"rule_pack sha256 check failed: {e}")

        # Orphan migration state
        try:
            orphan = conn.execute(
                "SELECT version FROM schema_version WHERE status='in_progress'"
            ).fetchall()
            if orphan:
                vers = ", ".join(str(r["version"]) for r in orphan)
                issues.append(
                    f"orphan schema_version rows (status in_progress): {vers}"
                )
        except sqlite3.OperationalError as e:
            issues.append(f"schema_version check failed: {e}")

        # Orphan running scrub_runs
        try:
            orphan_runs = conn.execute(
                "SELECT id FROM scrub_runs "
                "WHERE status IN ('pending', 'running') "
                "  AND ended_at IS NULL"
            ).fetchall()
            if orphan_runs:
                ids = ", ".join(str(r["id"]) for r in orphan_runs)
                issues.append(
                    f"orphan scrub_runs (pending/running with no ended_at): {ids}"
                )
        except sqlite3.OperationalError as e:
            issues.append(f"scrub_runs orphan check failed: {e}")
    finally:
        conn.close()

    # --deep: defer to recovery module if it exists.
    if args.deep:
        recovery = _import_recovery()
        if recovery is None:
            issues.append(
                "--deep requested but core.recovery is not wired"
            )
        else:
            try:
                report = recovery.verify(db_path, deep=True)
            except Exception as e:  # last-resort, surface upstream
                issues.append(f"--deep verify raised: {e}")
            else:
                for err in getattr(report, "errors", []) or []:
                    issues.append(str(err))
                for warn in getattr(report, "warnings", []) or []:
                    issues.append(f"warn: {warn}")

    if not issues:
        print(f"OK ({db_path})")
        return 0

    print(f"{len(issues)} issue(s) found:")
    for line in issues:
        print(f"  ! {line}")
    if any("orphan schema_version" in line for line in issues):
        return 5
    return 1


def _import_recovery() -> object | None:
    try:
        from aurochs_llm_scrub.core import recovery

        return recovery
    except ImportError:
        return None


def _resolve_db_path(args: argparse.Namespace) -> Path:
    return resolve_db_path(args)
