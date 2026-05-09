"""``llm-scrub scan`` — read-only scan; report-only output.

Walks the input file (or directory with ``--recursive``), produces a
findings report, never writes back to disk. Exit code reflects findings:

* ``0`` — clean OR findings present without ``--severity`` filter / --hard-fail
* ``5`` — findings present that match ``--severity`` filter, OR --hard-fail and any P0

Default output is plain-text with category labels. ``--json`` emits the
versioned ``ScrubReport`` shape (P0/P1/P2 codes). Pipeline lives in
``aurochs_llm_scrub.core.scrubber``; this module imports lazily.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aurochs_llm_scrub.cli.paths import add_db_path_arg, resolve_db_path


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "scan",
        help="Read-only scan; report findings without writing.",
    )
    p.add_argument(
        "path",
        help="File or directory to scan. Use '-' for stdin.",
    )
    p.add_argument(
        "--rules",
        dest="rules_pack",
        help="Rule pack name (default: built-in default pack).",
    )
    p.add_argument(
        "--severity",
        dest="severity",
        help="Filter to severities (e.g. 'P0' or 'P0,P1'). Non-matching exit 0.",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit ScrubReport JSON (P0/P1/P2 codes).",
    )
    p.add_argument(
        "--format",
        dest="output_format",
        choices=("plain", "json", "markdown"),
        default=None,
        help="Output format (overrides --json; default 'plain').",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="Walk directory recursively.",
    )
    p.add_argument(
        "--hard-fail",
        dest="hard_fail",
        action="store_true",
        help="Exit non-zero (5) when any P0 findings are emitted, regardless of --severity.",
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
    output_format = _resolve_output_format(args)

    scrubber = _import_scrubber()
    if scrubber is None:
        # Pipeline not yet wired — surface explicitly. Unblocks T0
        # parallel build: CLI parser tests still pass; runtime tests
        # become live once cleanup wires the imports.
        print(
            "llm-scrub: scan pipeline not available "
            "(aurochs_llm_scrub.core.scrubber import failed). "
            "Wire core/scrubber.py to enable.",
            file=sys.stderr,
        )
        return 1

    try:
        report = scrubber.scan(
            target=target,
            db_path=db_path,
            rules_pack=args.rules_pack,
            severity_filter=severity_filter,
            recursive=args.recursive,
            quiet=args.quiet,
        )
    except FileNotFoundError as e:
        print(f"llm-scrub: {e}", file=sys.stderr)
        return 2

    _emit_report(report, output_format)
    return _exit_code_from_report(
        report,
        severity_filter=severity_filter,
        hard_fail=args.hard_fail,
    )


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


def _resolve_output_format(args: argparse.Namespace) -> str:
    if args.output_format:
        return args.output_format
    if args.as_json:
        return "json"
    return "plain"


def _import_scrubber() -> object | None:
    """Best-effort import of the scrubber pipeline.

    Returns None when the pipeline module isn't wired yet; callers
    handle the soft-failure path with a clear stderr message.
    """
    try:
        from aurochs_llm_scrub.core import scrubber

        return scrubber
    except ImportError:
        return None


def _emit_report(report: object, output_format: str) -> None:
    """Render a report in the chosen format.

    The scrubber pipeline is expected to return an object that exposes
    ``.to_plain()``, ``.to_json_dict()``, and ``.to_markdown()`` methods,
    or a Pydantic ``ScrubReport`` model. We probe both shapes.
    """
    if output_format == "json":
        if hasattr(report, "model_dump"):
            payload = report.model_dump()  # type: ignore[attr-defined]
        elif hasattr(report, "to_json_dict"):
            payload = report.to_json_dict()  # type: ignore[attr-defined]
        else:
            payload = dict(report) if isinstance(report, dict) else _asdict_safe(report)
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return
    if output_format == "markdown" and hasattr(report, "to_markdown"):
        sys.stdout.write(report.to_markdown())  # type: ignore[attr-defined]
        return
    if hasattr(report, "to_plain"):
        sys.stdout.write(report.to_plain())  # type: ignore[attr-defined]
        return
    # Last-resort: render via repr.
    print(report)


def _asdict_safe(obj: object) -> dict[str, object]:
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {"value": str(obj)}


def _exit_code_from_report(
    report: object,
    *,
    severity_filter: list[str] | None,
    hard_fail: bool,
) -> int:
    """Map a report to an exit code per plan v4 K3 / scope contract.

    Default: exit 0 (findings reported, scan succeeded).
    With ``--severity`` filter: exit 5 if matching findings exist.
    With ``--hard-fail``: exit 5 if any P0 findings exist.
    """
    summary = getattr(report, "summary", None)
    p0 = int(getattr(summary, "p0_count", 0) or 0) if summary is not None else 0
    p1 = int(getattr(summary, "p1_count", 0) or 0) if summary is not None else 0
    p2 = int(getattr(summary, "p2_count", 0) or 0) if summary is not None else 0

    if hard_fail and p0 > 0:
        return 5
    if severity_filter:
        for sev in severity_filter:
            count = {"P0": p0, "P1": p1, "P2": p2}.get(sev, 0)
            if count > 0:
                return 5
    return 0
