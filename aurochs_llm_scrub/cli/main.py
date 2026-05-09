"""llm-scrub CLI — argparse-based, subcommand-dispatched.

T0 surface (this build):

    llm-scrub init                  create runs.db + sidecar files
    llm-scrub scan                  read-only scan; report-only output
    llm-scrub scrub                 scan + write cleaned output
    llm-scrub install-precommit     install pre-commit hook
    llm-scrub rules                 rule management (list/show/migrate/enable/disable)
    llm-scrub runs                  scrub-run history (list/show/diff)
    llm-scrub verify                integrity checks (--deep adds WAL walk)
    llm-scrub status                version + DB stats + disabled-rule count
    llm-scrub version               print version + exit

Each subcommand handler imports its core dependency lazily so
``llm-scrub --help`` is fast and zero-cost when the pipeline isn't wired.

Exit codes (locked):
    0  success
    1  generic error
    2  usage error / invalid args
    3  DB locked (LockError)
    4  schema mismatch (manifest.schema_version > plugin)
    5  scan found violations (with --severity filter or --hard-fail)
    6  pre-commit hook found findings (P0 = blocker per plan v4 K3)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence

# Windows console defaults to cp1252; reconfigure to UTF-8 with
# replacement fallback before any output happens. Mirrors voice/recall.
if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROG = "llm-scrub"


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv and dispatch. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return int(handler(args) or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as e:  # last-resort guard so users get a one-liner
        return _handle_exception(e)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "aurochs-llm-scrub — find the AI tells in your drafts. "
            "Then rewrite them yourself."
        ),
    )
    p.add_argument(
        "--db-path",
        dest="db_path",
        help="Path to runs.db (overrides default per-user data dir).",
    )
    p.add_argument(
        "--version",
        action="version",
        version=_version_string(),
    )

    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    # Subcommands — registered through their own modules so each owns
    # its parser shape and handler. Lazy imports keep --help cheap.
    from aurochs_llm_scrub.cli import (
        init_cmd,
        install_precommit_cmd,
        rules_cmd,
        runs_cmd,
        scan_cmd,
        scrub_cmd,
        status_cmd,
        verify_cmd,
    )

    init_cmd.add(sub)
    scan_cmd.add(sub)
    scrub_cmd.add(sub)
    install_precommit_cmd.add(sub)
    rules_cmd.add(sub)
    runs_cmd.add(sub)
    verify_cmd.add(sub)
    status_cmd.add(sub)

    # `llm-scrub version` as a subcommand for parity with `--version`.
    ver = sub.add_parser("version", help="Print version and exit.")
    ver.set_defaults(_handler=_cmd_version)

    return p


def _cmd_version(_args: argparse.Namespace) -> int:
    print(_version_string())
    return 0


def _version_string() -> str:
    try:
        from aurochs_llm_scrub import __version__
    except ImportError:
        return f"{PROG} (unknown version)"
    return f"{PROG} {__version__}"


def _handle_exception(e: BaseException) -> int:
    """Map exceptions to documented exit codes.

    LockError → 3
    MigrationError with schema-version mismatch → 4
    Anything else → 1 (generic), with traceback hidden unless debug.
    """
    lock_cls: type[BaseException] | None
    migrate_cls: type[BaseException] | None
    try:
        from aurochs_llm_scrub.core import LockError as _Lock
        from aurochs_llm_scrub.core import MigrationError as _Migrate

        lock_cls = _Lock
        migrate_cls = _Migrate
    except ImportError:
        lock_cls = None
        migrate_cls = None

    if lock_cls is not None and isinstance(e, lock_cls):
        print(f"{PROG}: {e}", file=sys.stderr)
        return 3
    if migrate_cls is not None and isinstance(e, migrate_cls):
        msg = str(e)
        # Schema mismatch is its own category (#4).
        if "schema_version" in msg.lower() or "schema mismatch" in msg.lower():
            print(f"{PROG}: schema mismatch — {e}", file=sys.stderr)
            return 4
        # Partial-state migrations need manual recovery.
        if "partial migration" in msg.lower():
            print(f"{PROG}: orphan migration state — {e}", file=sys.stderr)
            return 4
        print(f"{PROG}: migration error — {e}", file=sys.stderr)
        return 1

    if isinstance(e, FileNotFoundError):
        print(f"{PROG}: {e}", file=sys.stderr)
        return 2

    if os.environ.get("AUROCHS_LLM_SCRUB_DEBUG"):
        raise e
    print(f"{PROG}: error: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
