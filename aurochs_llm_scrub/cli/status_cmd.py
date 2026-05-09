"""``llm-scrub status`` — version + DB stats + disabled-rule count.

Plain-text by default, ``--json`` for machine consumption. Reads only;
no writes, no locks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from aurochs_llm_scrub.cli.paths import add_db_path_arg, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "status",
        help="Show version, DB stats, rule + disabled-rule counts.",
    )
    p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(p)
    p.set_defaults(_handler=run)


def run(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)

    payload: dict[str, object] = {
        "version": _version_string(),
        "runs_db_path": str(db_path),
        "runs_db_exists": db_path.exists(),
        "runs_db_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }

    if not db_path.exists():
        if args.as_json:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            _emit_text_no_db(payload)
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        # Schema version
        try:
            sv = conn.execute(
                "SELECT MAX(version) FROM schema_version WHERE status='applied'"
            ).fetchone()[0]
            payload["schema_version"] = sv
        except sqlite3.OperationalError:
            payload["schema_version"] = None

        # Rule packs + rule counts
        try:
            payload["rule_packs_total"] = conn.execute(
                "SELECT COUNT(*) FROM rule_packs"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            payload["rule_packs_total"] = None
        try:
            payload["rules_total"] = conn.execute(
                "SELECT COUNT(*) FROM rules"
            ).fetchone()[0]
            payload["rules_enabled"] = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE enabled = 1"
            ).fetchone()[0]
            payload["rules_disabled"] = conn.execute(
                "SELECT COUNT(*) FROM rules WHERE enabled = 0"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            payload["rules_total"] = None
            payload["rules_enabled"] = None
            payload["rules_disabled"] = None

        # Scrub-run summary
        try:
            payload["scrub_runs_total"] = conn.execute(
                "SELECT COUNT(*) FROM scrub_runs"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            payload["scrub_runs_total"] = None

        # Disabled rules audit log size
        try:
            payload["disabled_log_total"] = conn.execute(
                "SELECT COUNT(*) FROM disabled_rules_log"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            payload["disabled_log_total"] = None

        # Latest run summary
        try:
            row = conn.execute(
                "SELECT id, status, started_at, ended_at, "
                "       total_findings, total_redacted, target_path, mode "
                "FROM scrub_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                payload["last_run"] = {
                    "id": row["id"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "target_path": row["target_path"],
                    "mode": row["mode"],
                    "total_findings": row["total_findings"],
                    "total_redacted": row["total_redacted"],
                }
            else:
                payload["last_run"] = None
        except sqlite3.OperationalError:
            payload["last_run"] = None
    finally:
        conn.close()

    if args.as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    _emit_text(payload)
    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _version_string() -> str:
    try:
        from aurochs_llm_scrub import __version__

        return __version__
    except ImportError:
        return "unknown"


def _emit_text_no_db(payload: dict[str, object]) -> None:
    print(f"version:         {payload['version']}")
    print(f"runs.db:         {payload['runs_db_path']}  (not found)")
    print("Run `llm-scrub init` to create runs.db.")


def _emit_text(payload: dict[str, object]) -> None:
    print(f"version:         {payload['version']}")
    print(
        f"runs.db:         {payload['runs_db_path']}  "
        f"({_humanize_bytes(int(payload['runs_db_bytes'] or 0))})"
    )
    schema_v = payload.get("schema_version")
    print(f"schema:          v{schema_v}" if schema_v else "schema:          (none)")
    print(f"rule packs:      {payload.get('rule_packs_total')}")
    print(
        f"rules:           {payload.get('rules_enabled')} active "
        f"/ {payload.get('rules_total')} total "
        f"({payload.get('rules_disabled')} disabled)"
    )
    print(f"scrub runs:      {payload.get('scrub_runs_total')}")
    print(f"disabled log:    {payload.get('disabled_log_total')}")
    last = payload.get("last_run")
    if last is None:
        print("last run:        (none yet)")
    else:
        assert isinstance(last, dict)
        print(
            f"last run:        #{last.get('id')} status={last.get('status')} "
            f"findings={last.get('total_findings')}"
        )


def _humanize_bytes(n: int) -> str:
    fn = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(fn) < 1024:
            return f"{fn:.1f}{unit}" if unit != "B" else f"{int(fn)}{unit}"
        fn /= 1024
    return f"{fn:.1f}PB"
