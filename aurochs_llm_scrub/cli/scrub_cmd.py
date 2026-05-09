"""``llm-scrub scrub`` — apply mechanical fixes; write cleaned output.

Two output modes (mutually exclusive):

* ``--in-place`` — mtime check + ``.bak`` (per ``--backup-mode``) +
  fcntl/win32 lockfile + write back to the original.
* ``--to-stdout`` — print cleaned text to stdout, leave the original
  untouched (default; pipeline-friendly).

A ``.scrubbed`` sibling file is produced when neither mode is set,
matching the v0.1 default ``llm-scrub <file> --fix`` behavior. Pipeline
lives in ``aurochs_llm_scrub.core.scrubber``; this module imports
lazily.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aurochs_llm_scrub.cli.paths import add_db_path_arg, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "scrub",
        help="Apply mechanical fixes; write cleaned output.",
    )
    p.add_argument(
        "path",
        help="File to scrub. Use '-' for stdin (writes to stdout).",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--in-place",
        dest="in_place",
        action="store_true",
        help="Overwrite the original file (mtime check + .bak per --backup-mode).",
    )
    g.add_argument(
        "--to-stdout",
        dest="to_stdout",
        action="store_true",
        help="Print cleaned text to stdout; leave the original untouched.",
    )
    p.add_argument(
        "--backup-mode",
        dest="backup_mode",
        choices=("rotate", "fail", "overwrite"),
        default="rotate",
        help=(
            "Behavior when a .bak file already exists "
            "(rotate=rename existing, fail=refuse, overwrite=replace). Default: rotate."
        ),
    )
    p.add_argument(
        "--no-backup",
        dest="no_backup",
        action="store_true",
        help="Skip the .bak sidecar entirely (in-place mode only).",
    )
    p.add_argument(
        "--rules",
        dest="rules_pack",
        help="Rule pack name (default: built-in default pack).",
    )
    p.add_argument(
        "--severity",
        dest="severity",
        help="Filter findings to severities (e.g. 'P0' or 'P0,P1').",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="Print unified diff to stderr after scrubbing.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress P2 redundancy warnings.",
    )
    add_db_path_arg(p)
    p.set_defaults(_handler=run)


def run(args: argparse.Namespace) -> int:
    target = _resolve_target(args.path)
    db_path = resolve_db_path(args)
    severity_filter = _parse_severity_filter(args.severity)

    scrubber = _import_scrubber()
    if scrubber is None:
        print(
            "llm-scrub: scrub pipeline not available "
            "(aurochs_llm_scrub.core.scrubber import failed). "
            "Wire core/scrubber.py to enable.",
            file=sys.stderr,
        )
        return 1

    if args.no_backup and not args.in_place:
        print(
            "llm-scrub: --no-backup requires --in-place.",
            file=sys.stderr,
        )
        return 2

    mode = _resolve_mode(args)

    try:
        result = scrubber.scrub(
            target=target,
            db_path=db_path,
            rules_pack=args.rules_pack,
            severity_filter=severity_filter,
            mode=mode,
            backup_mode=args.backup_mode,
            no_backup=args.no_backup,
            quiet=args.quiet,
            emit_diff=args.diff,
        )
    except FileNotFoundError as e:
        print(f"llm-scrub: {e}", file=sys.stderr)
        return 2

    # Pipeline returns a (cleaned_text, report) tuple-like or single
    # result with .cleaned_text + .report attrs. Print to stdout when
    # --to-stdout / stdin pipeline.
    if mode == "scrub-to-stdout" or target == "<stdin>":
        cleaned = _extract_cleaned_text(result)
        if cleaned is not None:
            sys.stdout.write(cleaned)

    return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _resolve_target(raw: str) -> Path | str:
    if raw == "-":
        return "<stdin>"
    return Path(raw).expanduser().resolve()


def _parse_severity_filter(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = []
    for piece in raw.split(","):
        sev = piece.strip().upper()
        if sev not in {"P0", "P1", "P2"}:
            raise SystemExit(
                f"llm-scrub: invalid severity {piece!r}; must be P0/P1/P2"
            )
        out.append(sev)
    return out or None


def _resolve_mode(args: argparse.Namespace) -> str:
    if args.in_place:
        return "scrub-in-place"
    if args.to_stdout:
        return "scrub-to-stdout"
    return "scrub-to-stdout"  # default per scope description


def _import_scrubber() -> object | None:
    try:
        from aurochs_llm_scrub.core import scrubber

        return scrubber
    except ImportError:
        return None


def _extract_cleaned_text(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, tuple) and result and isinstance(result[0], str):
        return result[0]
    if hasattr(result, "cleaned_text"):
        val = result.cleaned_text  # type: ignore[attr-defined]
        if isinstance(val, str):
            return val
    return None
