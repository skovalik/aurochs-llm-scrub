"""``llm-scrub rules`` — rule pack + rule management.

Sub-subcommands::

    llm-scrub rules list [--disabled] [--pack NAME] [--json]
    llm-scrub rules show RULE-ID [--pack NAME] [--json]
    llm-scrub rules migrate [--from VERSION] [--to VERSION]
    llm-scrub rules enable RULE-ID [--pack NAME]
    llm-scrub rules disable RULE-ID [--pack NAME] [--reason TEXT]

The rule registry lives in ``runs.db`` (the ``rules`` and ``rule_packs``
tables); this module reads/writes via the spine's ``db_connect`` and
falls back to the pipeline's ``rule_pack`` module for migration logic
when available. Re-enable handles both the SQLite ``enabled`` column AND
``disabled_rules_log`` audit trail.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from aurochs_llm_scrub.cli.paths import add_db_path_arg, default_db_path, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("rules", help="Manage rule packs and individual rules.")
    add_db_path_arg(p)
    sub2 = p.add_subparsers(dest="rules_cmd", metavar="<sub>")

    list_p = sub2.add_parser("list", help="List loaded rules + status.")
    list_p.add_argument("--disabled", action="store_true",
                        help="Show only auto-disabled rules.")
    list_p.add_argument("--pack", dest="pack_name",
                        help="Filter to a single rule pack by name.")
    list_p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(list_p)
    list_p.set_defaults(_handler=_cmd_list)

    show_p = sub2.add_parser("show", help="Show one rule's full definition.")
    show_p.add_argument("rule_id", help="Rule id (e.g. 'p0-em-dash').")
    show_p.add_argument("--pack", dest="pack_name",
                        help="Pack name (required when rule_id is ambiguous).")
    show_p.add_argument("--json", dest="as_json", action="store_true")
    add_db_path_arg(show_p)
    show_p.set_defaults(_handler=_cmd_show)

    migrate_p = sub2.add_parser("migrate", help="Migrate custom-rules.toml schema.")
    migrate_p.add_argument("--from", dest="from_version",
                           help="Source schema version (default: detect).")
    migrate_p.add_argument("--to", dest="to_version",
                           help="Target schema version (default: latest).")
    add_db_path_arg(migrate_p)
    migrate_p.set_defaults(_handler=_cmd_migrate)

    enable_p = sub2.add_parser("enable", help="Re-enable a previously-disabled rule.")
    enable_p.add_argument("rule_id", help="Rule id to enable.")
    enable_p.add_argument("--pack", dest="pack_name",
                          help="Pack name (required when rule_id is ambiguous).")
    add_db_path_arg(enable_p)
    enable_p.set_defaults(_handler=_cmd_enable)

    disable_p = sub2.add_parser("disable", help="Disable a rule (manual scope).")
    disable_p.add_argument("rule_id", help="Rule id to disable.")
    disable_p.add_argument("--pack", dest="pack_name",
                           help="Pack name (required when rule_id is ambiguous).")
    disable_p.add_argument("--reason", default="manual",
                           help="Disable reason token (default: 'manual').")
    add_db_path_arg(disable_p)
    disable_p.set_defaults(_handler=_cmd_disable)

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
        rows = _load_rule_rows(conn, pack_name=args.pack_name,
                               only_disabled=args.disabled)
    finally:
        conn.close()

    if args.as_json:
        json.dump([dict(r) for r in rows], sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("(no rules)")
        return 0

    print(f"{'pack':<24} {'rule_id':<32} {'sev':<5} {'lang':<6} status")
    for r in rows:
        status = "active" if r["enabled"] else "disabled"
        lang = r["language"] or "*"
        print(
            f"{r['pack_name']:<24} {r['rule_id']:<32} "
            f"{r['severity_default']:<5} {lang:<6} {status}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        rows = _load_rule_rows(conn, rule_id=args.rule_id,
                               pack_name=args.pack_name)
    finally:
        conn.close()

    if not rows:
        print(f"llm-scrub: no rule with id {args.rule_id!r}.", file=sys.stderr)
        return 2
    if len(rows) > 1 and not args.pack_name:
        print(
            f"llm-scrub: rule_id {args.rule_id!r} exists in multiple packs; "
            "specify --pack.",
            file=sys.stderr,
        )
        for r in rows:
            print(f"  - pack={r['pack_name']} version={r['pack_version']}",
                  file=sys.stderr)
        return 2

    rule = rows[0]
    if args.as_json:
        json.dump(dict(rule), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"rule_id:        {rule['rule_id']}")
    print(f"pack:           {rule['pack_name']}  v{rule['pack_version']}")
    print(f"name:           {rule['name']}")
    print(f"severity:       {rule['severity_default']}")
    print(f"language:       {rule['language'] or '(none — always-on)'}")
    print(f"enabled:        {bool(rule['enabled'])}")
    print(f"pattern:        {rule['pattern']}")
    print("description:")
    for line in (rule["description"] or "").splitlines() or [""]:
        print(f"  {line}")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Delegate to the pipeline's rule_pack migration helper.

    The CLI knows about the file paths but not the schema details. The
    pipeline owns the migration map; this command surfaces failures
    cleanly.
    """
    db_path = resolve_db_path(args)
    rule_pack_mod = _import_rule_pack()
    if rule_pack_mod is None:
        print(
            "llm-scrub: rule-pack migration not available "
            "(aurochs_llm_scrub.core.rule_pack import failed). "
            "Wire core/rule_pack.py to enable.",
            file=sys.stderr,
        )
        return 1

    try:
        result = rule_pack_mod.migrate(
            db_path=db_path,
            from_version=args.from_version,
            to_version=args.to_version,
        )
    except FileNotFoundError as e:
        print(f"llm-scrub: {e}", file=sys.stderr)
        return 2

    print(
        f"migrated:        {getattr(result, 'from_version', '?')} → "
        f"{getattr(result, 'to_version', '?')}"
    )
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        rule = _resolve_single_rule(conn, args.rule_id, args.pack_name)
        if rule is None:
            return 2
        if rule["enabled"]:
            print(
                f"llm-scrub: rule {args.rule_id!r} is already enabled.",
                file=sys.stderr,
            )
            return 1
        conn.execute(
            "UPDATE rules SET enabled = 1 WHERE id = ?", (rule["id"],)
        )
    finally:
        conn.close()

    print(f"enabled:         {rule['pack_name']}/{rule['rule_id']}")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not _ensure_db(db_path):
        return 1

    from aurochs_llm_scrub.core import db_connect

    conn = db_connect(db_path)
    try:
        rule = _resolve_single_rule(conn, args.rule_id, args.pack_name)
        if rule is None:
            return 2
        if not rule["enabled"]:
            print(
                f"llm-scrub: rule {args.rule_id!r} is already disabled.",
                file=sys.stderr,
            )
            return 1
        now = int(time.time())
        conn.execute(
            "UPDATE rules SET enabled = 0 WHERE id = ?", (rule["id"],)
        )
        conn.execute(
            "INSERT INTO disabled_rules_log "
            "(pack_name, pack_version, rule_id, disabled_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                rule["pack_name"],
                rule["pack_version"],
                rule["rule_id"],
                now,
                args.reason or "manual",
            ),
        )
    finally:
        conn.close()

    print(f"disabled:        {rule['pack_name']}/{rule['rule_id']}  "
          f"(reason: {args.reason})")
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


def _import_rule_pack() -> object | None:
    try:
        from aurochs_llm_scrub.core import rule_pack

        return rule_pack
    except ImportError:
        return None


def _load_rule_rows(
    conn: sqlite3.Connection,
    *,
    rule_id: str | None = None,
    pack_name: str | None = None,
    only_disabled: bool = False,
) -> list[sqlite3.Row]:
    sql = (
        "SELECT r.id, r.pack_id, r.rule_id, r.name, r.pattern, "
        "       r.severity_default, r.language, r.description, r.enabled, "
        "       p.name AS pack_name, p.version AS pack_version "
        "FROM rules r JOIN rule_packs p ON r.pack_id = p.id "
    )
    where: list[str] = []
    params: list[object] = []
    if rule_id is not None:
        where.append("r.rule_id = ?")
        params.append(rule_id)
    if pack_name is not None:
        where.append("p.name = ?")
        params.append(pack_name)
    if only_disabled:
        where.append("r.enabled = 0")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY p.name, r.severity_default, r.rule_id"
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.OperationalError:
        return []


def _resolve_single_rule(
    conn: sqlite3.Connection,
    rule_id: str,
    pack_name: str | None,
) -> sqlite3.Row | None:
    rows = _load_rule_rows(conn, rule_id=rule_id, pack_name=pack_name)
    if not rows:
        print(
            f"llm-scrub: no rule with id {rule_id!r}"
            + (f" in pack {pack_name!r}" if pack_name else "")
            + ".",
            file=sys.stderr,
        )
        return None
    if len(rows) > 1:
        print(
            f"llm-scrub: rule_id {rule_id!r} exists in multiple packs; "
            "specify --pack.",
            file=sys.stderr,
        )
        for r in rows:
            print(f"  - pack={r['pack_name']} version={r['pack_version']}",
                  file=sys.stderr)
        return None
    return rows[0]


def _resolve_default_db_arg() -> Path:
    return default_db_path()
