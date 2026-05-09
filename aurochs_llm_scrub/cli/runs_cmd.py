"""``llm-scrub runs`` — scrub-run history.

Sub-subcommands::

    llm-scrub runs list [--limit N] [--target PATH] [--json]
    llm-scrub runs show RUN-ID [--json]
    llm-scrub runs diff RUN-ID-1 RUN-ID-2 [--json]

History is opt-in (off by default per plan v4); the CLI falls back to a
clear "no runs yet" message when ``runs.db`` exists but has no rows. All
queries hit ``scrub_runs`` and ``findings`` directly via the spine.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from aurochs_llm_scrub.cli.paths import add_db_path_arg, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("runs", help="Inspect scrub-run history.")
    add_db_path_arg(p)
    sub2 = p.add_subparsers(dest="runs_cmd", metavar="<sub>")

    list_p = sub2.add_parser("list", help="List recent scrub runs.")
    list_p.add_argument("--limit", type=int, default=50,
                        help="Max rows to list (default: 50).")
    list_p.add_argument("--target", dest="target_filter",
                        help="Filter to runs whose target_path matches.")
    list_p.add_argument("--status", dest="status_filter",
                        help="Filter by status (success/partial/failed/etc.).")
    list_p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(list_p)
    list_p.set_defaults(_handler=_cmd_list)

    show_p = sub2.add_parser("show", help="Show a run with all findings.")
    show_p.add_argument("run_id", type=int, help="Run id (from `runs list`).")
    show_p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(show_p)
    show_p.set_defaults(_handler=_cmd_show)

    diff_p = sub2.add_parser("diff", help="Compare findings between two runs.")
    diff_p.add_argument("run_id_a", type=int, help="Earlier run id.")
    diff_p.add_argument("run_id_b", type=int, help="Later run id.")
    diff_p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(diff_p)
    diff_p.set_defaults(_handler=_cmd_diff)

    p.set_defaults(_handler=lambda a: (p.print_help() or 0))


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        rows = _query_runs(
            conn,
            limit=max(1, args.limit),
            target_filter=args.target_filter,
            status_filter=args.status_filter,
        )
    finally:
        conn.close()

    if args.as_json:
        json.dump([dict(r) for r in rows], sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("(no runs yet — opt-in history is off by default)")
        return 0
    print(f"{'id':<6} {'status':<10} {'mode':<18} {'findings':<8}  target")
    for r in rows:
        print(
            f"{r['id']:<6} {r['status']:<10} {r['mode']:<18} "
            f"{r['total_findings']:<8}  {r['target_path']}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        run = _fetch_run(conn, args.run_id)
        findings = _fetch_findings(conn, args.run_id) if run is not None else []
    finally:
        conn.close()

    if run is None:
        print(f"llm-scrub: no run with id {args.run_id}.", file=sys.stderr)
        return 2

    if args.as_json:
        payload = {
            "run": dict(run),
            "findings": [dict(f) for f in findings],
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"run #{run['id']}")
    print(f"  target:        {run['target_path']}")
    print(f"  mode:          {run['mode']}")
    print(f"  status:        {run['status']}")
    print(f"  started:       {run['started_at']}")
    if run["ended_at"] is not None:
        print(f"  ended:         {run['ended_at']}")
    print(f"  findings:      {run['total_findings']}")
    print(f"  redacted:      {run['total_redacted']}")
    if findings:
        print()
        print(f"  {'sev':<4} {'line':<6} {'rule':<28}  snippet")
        for f in findings[:200]:
            snippet = (f["snippet"] or "").replace("\n", " ")
            if len(snippet) > 60:
                snippet = snippet[:57] + "..."
            print(
                f"  {f['severity']:<4} {f['line_number']:<6} "
                f"{f['rule_id_text'] or '?':<28}  {snippet}"
            )
        if len(findings) > 200:
            print(f"  ... ({len(findings) - 200} more)")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        run_a = _fetch_run(conn, args.run_id_a)
        run_b = _fetch_run(conn, args.run_id_b)
        if run_a is None or run_b is None:
            missing = args.run_id_a if run_a is None else args.run_id_b
            print(f"llm-scrub: no run with id {missing}.", file=sys.stderr)
            return 2
        findings_a = _fetch_findings(conn, args.run_id_a)
        findings_b = _fetch_findings(conn, args.run_id_b)
    finally:
        conn.close()

    keys_a = {_finding_key(f) for f in findings_a}
    keys_b = {_finding_key(f) for f in findings_b}
    only_a_keys = keys_a - keys_b
    only_b_keys = keys_b - keys_a

    only_a = [dict(f) for f in findings_a if _finding_key(f) in only_a_keys]
    only_b = [dict(f) for f in findings_b if _finding_key(f) in only_b_keys]

    if args.as_json:
        payload = {
            "a": dict(run_a),
            "b": dict(run_b),
            "only_in_a": only_a,
            "only_in_b": only_b,
            "shared_count": len(keys_a & keys_b),
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"run a: #{run_a['id']}  ({run_a['total_findings']} findings)")
    print(f"run b: #{run_b['id']}  ({run_b['total_findings']} findings)")
    print(f"shared: {len(keys_a & keys_b)}")
    print(f"only in a (resolved): {len(only_a)}")
    print(f"only in b (regressed): {len(only_b)}")
    if only_a:
        print()
        print("resolved (in a, not in b):")
        for f in only_a[:50]:
            print(
                f"  {f['severity']:<4} L{f['line_number']:<5} "
                f"{f.get('rule_id_text', '?')}"
            )
    if only_b:
        print()
        print("regressed (in b, not in a):")
        for f in only_b[:50]:
            print(
                f"  {f['severity']:<4} L{f['line_number']:<5} "
                f"{f.get('rule_id_text', '?')}"
            )
    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _ensure_db(db_path: Path) -> bool:
    if db_path.exists():
        return True
    print(
        f"llm-scrub: runs.db not found at {db_path}. Run `llm-scrub init` first.",
        file=sys.stderr,
    )
    return False


def _query_runs(
    conn: sqlite3.Connection,
    *,
    limit: int,
    target_filter: str | None,
    status_filter: str | None,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, started_at, ended_at, status, target_path, mode, "
        "       total_findings, total_redacted "
        "FROM scrub_runs "
    )
    where: list[str] = []
    params: list[object] = []
    if target_filter:
        where.append("target_path LIKE ?")
        params.append(f"%{target_filter}%")
    if status_filter:
        where.append("status = ?")
        params.append(status_filter)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.OperationalError:
        return []


def _fetch_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT id, started_at, ended_at, status, target_path, mode, "
            "       total_findings, total_redacted "
            "FROM scrub_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _fetch_findings(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                "SELECT f.id, f.run_id, f.rule_id, f.file_offset, f.length, "
                "       f.file_path, f.line_number, f.severity, f.snippet, "
                "       r.rule_id AS rule_id_text "
                "FROM findings f LEFT JOIN rules r ON f.rule_id = r.id "
                "WHERE f.run_id = ? "
                "ORDER BY f.line_number, f.file_offset",
                (run_id,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def _finding_key(f: sqlite3.Row) -> tuple[object, ...]:
    """Stable identity of a finding for diffing across runs."""
    return (
        f["file_path"],
        f["rule_id_text"],
        f["line_number"],
        f["severity"],
        (f["snippet"] or "").strip(),
    )
