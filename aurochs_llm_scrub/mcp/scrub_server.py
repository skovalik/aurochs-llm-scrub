"""MCP server module — scrub tools exposed to MCP clients.

Why this file is named ``scrub_server.py``
-------------------------------------------
The local ``mcp/`` package shares a name with the installed Anthropic
``mcp`` SDK. Even though our ``mcp/__init__.py`` does NOT extend
``__path__`` (we rely on absolute imports working unambiguously through
the SDK install), naming a file ``server.py`` would shadow the SDK's
``mcp.server`` package for anyone who later tried to add a path-extension
shim. ``scrub_server.py`` preserves the intent (scrub MCP server entry
point) without creating any appearance of SDK collision.

What this module does
---------------------
Exposes four tools to MCP clients via the lowlevel ``Server`` interface:

* ``scrub_scan_text`` — read-only scan; returns findings without rewriting.
* ``scrub_scrub_text`` — read+rewrite; returns cleaned text plus findings.
* ``scrub_list_rules`` — list registered rules from one or all rule packs.
* ``scrub_run_status`` — inspect a runs.db row by id (or the most recent).

Parallel-build reality
----------------------
This module wires to pipeline functions (``core.scrubber``,
``core.rules``, ``core.runs``). At write time those modules may not
exist yet — the pipeline agent is building them in parallel. We import
them inside each tool handler (lazy, late-bound) so the module loads
cleanly even when pipeline dependencies aren't ready, and tools degrade
to a ``PIPELINE_UNAVAILABLE`` error block rather than crashing the whole
server. The cleanup agent removes the lazy-import shims once all
pipeline modules are present.

The lowlevel ``Server`` here uses ``@server.call_tool(validate_input=
True)`` so JSON Schema validation runs BEFORE our handler. We never see
malformed inputs; failures come back to the client as
``isError=True`` text blocks.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

# Anthropic mcp SDK imports. We take these eagerly because the [mcp]
# extra is opt-in: callers who don't install the extra never reach this
# module (the build_server() / run_stdio() entry points raise ImportError
# at construction time, and the package-level mcp/__init__.py exposes a
# ``_sdk_available()`` helper for callers that want to check).
try:
    from mcp.server.lowlevel import NotificationOptions, Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        ContentBlock,
        TextContent,
        Tool,
    )
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only when SDK absent
    _SDK_AVAILABLE = False
    # Re-raise on actual server construction; module import stays clean.
    NotificationOptions = None  # type: ignore[assignment, misc]
    Server = None  # type: ignore[assignment, misc]
    stdio_server = None  # type: ignore[assignment]
    ContentBlock = Any  # type: ignore[assignment, misc]
    TextContent = None  # type: ignore[assignment, misc]
    Tool = None  # type: ignore[assignment, misc]

from aurochs_llm_scrub.core import (
    ScrubMode,
    ScrubStatus,
    db_connect,
)
from aurochs_llm_scrub.mcp.schemas import (
    INPUT_SCHEMAS,
    SCHEMA_VERSION,
    TOOL_NAMES,
    ErrorCode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration — runs.db location
# ---------------------------------------------------------------------------
# Plan v4 cross-cutting #19: cross-OS data dir via platformdirs. Override
# via env var for tests and for users who want a non-default location.

_RUNS_DB_ENV_VAR = "AUROCHS_LLM_SCRUB_DB"


def _default_runs_db_path() -> Path:
    """Resolve the canonical runs.db path.

    Honors ``AUROCHS_LLM_SCRUB_DB`` for test overrides and for users who
    install scrub in a non-default location. Falls back to
    ``platformdirs.user_data_dir("aurochs-llm-scrub")/runs.db`` (plan v4
    cross-cutting #19).
    """
    env_override = os.environ.get(_RUNS_DB_ENV_VAR)
    if env_override:
        return Path(env_override)

    try:
        import platformdirs

        data_dir = Path(platformdirs.user_data_dir("aurochs-llm-scrub"))
    except ImportError:  # pragma: no cover — platformdirs is a hard dep
        # Worst-case fallback so the server still has *some* path.
        if platform.system() == "Windows":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            data_dir = base / "aurochs-llm-scrub"
        else:
            data_dir = Path.home() / ".aurochs-llm-scrub"
    return data_dir / "runs.db"


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------
# The lowlevel Server's call_tool wrapper expects either
# Iterable[ContentBlock] or a (content, structured) tuple. For errors we
# build a CallToolResult manually so isError=True flows through to the
# client.


def _error_block(code: str, message: str, **details: Any) -> Any:
    """Build a TextContent error block.

    The text is JSON-encoded for machine-parseability while still being
    human-readable when surfaced verbatim in clients that don't decode.
    """
    payload = {
        "error": {
            "code": code,
            "message": message,
            "schema_version": SCHEMA_VERSION,
            **({"details": details} if details else {}),
        }
    }
    return TextContent(type="text", text=json.dumps(payload, indent=2))


def _error_result(code: str, message: str, **details: Any) -> Any:
    """Build a CallToolResult-compatible error response.

    The lowlevel Server.call_tool decorator returns an isError=True
    CallToolResult when its handler returns a CallToolResult instance
    directly. This helper produces that shape.
    """
    # Lazy import — types are only available when SDK is installed.
    from mcp.types import CallToolResult

    return CallToolResult(
        content=[_error_block(code, message, **details)],
        isError=True,
    )


# ---------------------------------------------------------------------------
# Tool definitions (Tool objects with input schemas)
# ---------------------------------------------------------------------------


def build_tool_list() -> list[Any]:
    """Return the canonical list of scrub tools.

    Each ``Tool`` carries the JSON Schema from ``mcp.schemas`` so the
    Server's ``call_tool(validate_input=True)`` decorator can reject
    malformed payloads BEFORE dispatch. Adding a new tool means: add a
    schema in ``mcp/schemas.py``, register it in ``INPUT_SCHEMAS`` /
    ``TOOL_NAMES``, and append a ``Tool`` here.
    """
    if not _SDK_AVAILABLE:
        raise ImportError(
            "Anthropic mcp SDK is not installed. Install via: "
            "pip install 'aurochs-llm-scrub[mcp]'"
        )

    descriptions = {
        "scrub_scan_text": (
            "Scan a string of prose for LLM-tell patterns (em dashes, "
            "buzzwords, signposting, hedging, corporate filler, etc.). "
            "Returns a list of findings with severities (P0/P1/P2), "
            "byte offsets, line numbers, and matched snippets. Does NOT "
            "modify the input. Use this when surfacing findings to a "
            "user for review without auto-fixing. Optional "
            "``severity_filter`` narrows the report to specific tiers."
        ),
        "scrub_scrub_text": (
            "Scan a string of prose AND apply mechanical auto-fixes. "
            "Returns the cleaned text plus the full findings list and a "
            "count of how many findings were auto-fixed. Only "
            "deterministic patterns are rewritten (em dashes, double "
            "spaces, trailing whitespace, ``&mdash;`` HTML entities); "
            "non-mechanical findings (buzzwords, signposting) are "
            "surfaced for human judgment but NOT modified. Optional "
            "``severity_filter`` narrows both the scan AND auto-fix scope."
        ),
        "scrub_list_rules": (
            "List detection rules registered in runs.db. Returns one "
            "row per rule with pack name, version, rule_id, name, "
            "default severity, language attribution (None = mechanical, "
            "always-on; ``\"en\"`` = English-coded), description, and "
            "enabled state. Use ``pack_name`` to filter to a single "
            "rule pack (e.g. ``\"default\"`` or a custom pack). "
            "``null`` (the default) returns rules from ALL packs."
        ),
        "scrub_run_status": (
            "Look up the status of a scrub run by id from runs.db. "
            "Returns the run row including ScrubStatus "
            "(pending/running/success/partial/failed/aborted), "
            "ScrubMode (scan/scrub-in-place/scrub-to-stdout/precommit), "
            "target_path, total_findings, and total_redacted. ``null`` "
            "(the default) returns the most recent run across all "
            "targets — in-progress preferred over recent finalized."
        ),
    }

    return [
        Tool(
            name=name,
            description=descriptions[name],
            inputSchema=INPUT_SCHEMAS[name],
        )
        for name in TOOL_NAMES
    ]


# ---------------------------------------------------------------------------
# Pipeline import helpers (lazy, late-bound)
# ---------------------------------------------------------------------------
# Pipeline modules (core.scrubber, core.rules, core.runs) are built in
# parallel. We import them inside each tool's first call so this module
# loads even when the parallel agent hasn't finished yet, and a tool can
# return PIPELINE_UNAVAILABLE without crashing the server.


def _import_scrubber() -> Any:
    """Lazy import of ``core.scrubber``. Returns the module or None."""
    try:
        import aurochs_llm_scrub.core.scrubber as _mod
        return _mod
    except ImportError:
        return None


def _import_rules() -> Any:
    """Lazy import of ``core.rules`` (the engine surface).

    The ``# type: ignore[import-not-found]`` is intentional: ``core.rules``
    is built in parallel with this MCP module and may not exist at
    type-check time. The runtime import is wrapped in try/except so
    callers get a clean None during the parallel build window.
    """
    try:
        import aurochs_llm_scrub.core.rules as _mod  # type: ignore[import-not-found]
        return _mod
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
# Each tool function takes the validated arguments dict and returns either
# (content, structured) or a CallToolResult on error. The lowlevel Server
# call_tool decorator dispatches to these by name. The runs.db Path is
# threaded through so tests can point each tool at a fixture DB.


def _normalize_severity_filter(arguments: dict[str, Any]) -> list[str] | None:
    """Coerce ``severity_filter`` to a list[str] or None.

    The schema accepts ``null`` or an array; an empty array also means
    "no filter" so the engine doesn't get a falsy filter that silently
    rejects everything.
    """
    raw = arguments.get("severity_filter")
    if raw is None:
        return None
    if not isinstance(raw, list):
        # Schema validation should prevent this, but be defensive.
        return None
    if not raw:
        return None
    # Cast each element to str (the schema enforces enum values, but
    # mypy strict needs the narrowing).
    return [str(s) for s in raw]


def _format_finding_block(finding: dict[str, Any]) -> Any:
    """Format one finding as a TextContent block.

    Format::

        [P0 rule_id pack_name] line=N offset=M length=K
        snippet (verbatim, possibly redacted by secret-scan)

    Used by ``scrub_scan_text`` and ``scrub_scrub_text`` to surface
    findings as content blocks alongside the structured payload.
    """
    header = (
        f"[{finding.get('severity', '?')} "
        f"{finding.get('rule_id', '?')} "
        f"{finding.get('pack_name', '?')}] "
        f"line={finding.get('line_number', '?')} "
        f"offset={finding.get('file_offset', '?')} "
        f"length={finding.get('length', '?')}"
    )
    snippet = finding.get("snippet", "")
    return TextContent(type="text", text=f"{header}\n{snippet}")


def _summarize_totals(totals: dict[str, int]) -> str:
    """One-line summary of P0/P1/P2 counts for a content block."""
    p0 = totals.get("P0", 0)
    p1 = totals.get("P1", 0)
    p2 = totals.get("P2", 0)
    return f"P0={p0} P1={p1} P2={p2}"


async def _tool_scrub_scan_text(
    arguments: dict[str, Any],
    db_path: Path,
) -> Any:
    """Implementation of ``scrub_scan_text``.

    Calls into ``core.scrubber.scan_text`` with the input + filter, and
    returns one TextContent per finding plus a structured summary. The
    pipeline is responsible for: chunking inputs >2MB, language gate,
    secret-scan redaction, ReDoS-safe rule application, and cross-chunk
    dedup. We just adapt the call.

    The runs.db path is accepted but only used by the pipeline if it
    chooses to log this scan to the opt-in history surface; pure
    text-in / findings-out is the contract here.
    """
    text = arguments["text"]
    severity_filter = _normalize_severity_filter(arguments)

    scrubber = _import_scrubber()
    if scrubber is None:
        return _error_result(
            ErrorCode.PIPELINE_UNAVAILABLE,
            "core.scrubber not yet wired. The parallel pipeline build "
            "is in progress — retry after install completes.",
        )

    # Pipeline contract (locked in docs/contracts.md):
    #
    #   scan_text_report(text, *, severity_filter, db_path) -> dict with keys
    #       'findings', 'totals', 'auto_disabled_count', 'language',
    #       'redaction_count'.
    #
    # The cleanup agent wires this MCP-shaped wrapper around the
    # lower-level ``scan_text`` (which returns ``(ScrubRun,
    # list[Finding])`` for engine-internal callers). Looking up the
    # wrapper name explicitly — instead of just calling ``scan_text`` —
    # keeps the contract honest: PIPELINE_UNAVAILABLE if the wrapper
    # isn't there yet, and the lower-level entrypoint stays useful for
    # CLI / engine tests.
    if not hasattr(scrubber, "scan_text_report"):
        return _error_result(
            ErrorCode.PIPELINE_UNAVAILABLE,
            "core.scrubber.scan_text_report not yet implemented "
            "(MCP-shaped wrapper around scan_text).",
        )

    try:
        report = scrubber.scan_text_report(
            text,
            severity_filter=severity_filter,
            db_path=db_path,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("scan_text_report raised")
        return _error_result(
            ErrorCode.INTERNAL_ERROR,
            f"scan_text_report failed: {exc}",
        )

    # Defensive shape coercion: the pipeline returns a dict, but we
    # extract just the fields we promised in the schema.
    findings: list[dict[str, Any]] = list(report.get("findings", []))
    totals: dict[str, int] = dict(report.get("totals", {}))
    auto_disabled_count: int = int(report.get("auto_disabled_count", 0))
    language: str | None = report.get("language")
    redaction_count: int = int(report.get("redaction_count", 0))

    structured = {
        "schema_version": SCHEMA_VERSION,
        "findings": findings,
        "totals": totals,
        "auto_disabled_count": auto_disabled_count,
        "language": language,
        "redaction_count": redaction_count,
    }

    content_blocks: list[Any] = []
    if not findings:
        content_blocks.append(
            TextContent(type="text", text="(no findings)")
        )
    else:
        for finding in findings:
            content_blocks.append(_format_finding_block(finding))

    # Summary footer — surfaces totals + auto-disable signal up front.
    summary_parts = [_summarize_totals(totals)]
    if redaction_count > 0:
        summary_parts.append(f"redacted={redaction_count}")
    if auto_disabled_count > 0:
        # K2 lock: auto-disable footer must be visible whenever count > 0
        # so a clean-looking report doesn't hide a silently-disabled rule.
        summary_parts.append(
            f"auto_disabled={auto_disabled_count} "
            "(see 'llm-scrub rules list --disabled')"
        )
    content_blocks.append(
        TextContent(type="text", text=" · ".join(summary_parts))
    )

    return content_blocks, structured


async def _tool_scrub_scrub_text(
    arguments: dict[str, Any],
    db_path: Path,
) -> Any:
    """Implementation of ``scrub_scrub_text``.

    Calls into ``core.scrubber.scrub_text`` with the input + filter and
    returns the cleaned text alongside the findings (mechanical fixes
    are applied; non-mechanical ones are surfaced for human review).
    """
    text = arguments["text"]
    severity_filter = _normalize_severity_filter(arguments)

    scrubber = _import_scrubber()
    if scrubber is None:
        return _error_result(
            ErrorCode.PIPELINE_UNAVAILABLE,
            "core.scrubber not yet wired. The parallel pipeline build "
            "is in progress — retry after install completes.",
        )

    # Pipeline contract (locked in docs/contracts.md):
    #
    #   scrub_text_report(text, *, severity_filter, db_path) -> dict with
    #       keys 'cleaned_text', 'findings', 'fixed_count', 'totals',
    #       'auto_disabled_count'.
    #
    # The cleanup agent wires this wrapper around the engine's
    # mechanical-fix path. The same naming convention as
    # scan_text_report keeps the engine internals (which return tuples)
    # decoupled from the MCP surface (which returns dicts).
    if not hasattr(scrubber, "scrub_text_report"):
        return _error_result(
            ErrorCode.PIPELINE_UNAVAILABLE,
            "core.scrubber.scrub_text_report not yet implemented "
            "(MCP-shaped wrapper around the mechanical-fix path).",
        )

    try:
        report = scrubber.scrub_text_report(
            text,
            severity_filter=severity_filter,
            db_path=db_path,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("scrub_text_report raised")
        return _error_result(
            ErrorCode.INTERNAL_ERROR,
            f"scrub_text_report failed: {exc}",
        )

    cleaned_text: str = str(report.get("cleaned_text", ""))
    findings: list[dict[str, Any]] = list(report.get("findings", []))
    fixed_count: int = int(report.get("fixed_count", 0))
    totals: dict[str, int] = dict(report.get("totals", {}))
    auto_disabled_count: int = int(report.get("auto_disabled_count", 0))

    structured = {
        "schema_version": SCHEMA_VERSION,
        "cleaned_text": cleaned_text,
        "findings": findings,
        "fixed_count": fixed_count,
        "totals": totals,
        "auto_disabled_count": auto_disabled_count,
    }

    content_blocks: list[Any] = []
    # Lead with a one-line summary so callers that only render the
    # first block get the headline number.
    summary_parts = [
        f"fixed={fixed_count}",
        _summarize_totals(totals),
    ]
    if auto_disabled_count > 0:
        summary_parts.append(
            f"auto_disabled={auto_disabled_count}"
        )
    content_blocks.append(
        TextContent(type="text", text=" · ".join(summary_parts))
    )

    # Then findings (truncated for very long lists; structured payload
    # carries the full set).
    for finding in findings:
        content_blocks.append(_format_finding_block(finding))

    return content_blocks, structured


async def _tool_scrub_list_rules(
    arguments: dict[str, Any],
    db_path: Path,
) -> Any:
    """Implementation of ``scrub_list_rules``.

    Returns rules currently registered in runs.db. If ``runs.db`` is
    missing (fresh install, never ran scrub) we fall back to the in-
    package default rule pack via ``core.rules.default_rules()``.

    A ``pack_name`` filter narrows the response. PACK_NOT_FOUND is
    returned only if a pack name is supplied but no matching pack
    exists; an unfiltered call against an empty DB returns an empty
    list (not an error) so a fresh install can ask "what rules ship?"
    and get the in-package defaults via the fallback path.
    """
    pack_name: str | None = arguments.get("pack_name")

    rules_payload: list[dict[str, Any]] = []

    if db_path.exists():
        # DB present — read registered rules from it.
        conn = db_connect(db_path)
        try:
            if pack_name is not None:
                pack_row = conn.execute(
                    "SELECT id, name, version FROM rule_packs "
                    "WHERE name = ? ORDER BY version DESC LIMIT 1",
                    (pack_name,),
                ).fetchone()
                if pack_row is None:
                    return _error_result(
                        ErrorCode.PACK_NOT_FOUND,
                        f"No rule pack with name {pack_name!r}",
                        pack_name=pack_name,
                    )
                rule_rows = conn.execute(
                    "SELECT pack_id, rule_id, name, severity_default, "
                    "language, description, enabled "
                    "FROM rules WHERE pack_id = ? ORDER BY severity_default, rule_id",
                    (pack_row["id"],),
                ).fetchall()
                pack_lookup = {pack_row["id"]: pack_row}
            else:
                # No filter — join across all packs.
                rule_rows = conn.execute(
                    "SELECT pack_id, rule_id, name, severity_default, "
                    "language, description, enabled "
                    "FROM rules ORDER BY pack_id, severity_default, rule_id"
                ).fetchall()
                pack_id_rows = conn.execute(
                    "SELECT id, name, version FROM rule_packs"
                ).fetchall()
                pack_lookup = {row["id"]: row for row in pack_id_rows}

            for row in rule_rows:
                pack = pack_lookup.get(row["pack_id"])
                rules_payload.append(
                    {
                        "pack_name": pack["name"] if pack else "?",
                        "pack_version": pack["version"] if pack else "?",
                        "rule_id": row["rule_id"],
                        "name": row["name"],
                        "severity_default": row["severity_default"],
                        "language": row["language"],
                        "description": row["description"],
                        "enabled": bool(row["enabled"]),
                    }
                )
        finally:
            conn.close()
    else:
        # DB missing — fall back to in-package default rules if the
        # rule engine has registered them. This lets a fresh install
        # ask "what does this scrubber check?" without first running
        # the migrations. PACK_NOT_FOUND for an explicit (non-default)
        # filter is still returned because the in-package fallback is
        # only the default pack.
        if pack_name is not None and pack_name != "default":
            return _error_result(
                ErrorCode.PACK_NOT_FOUND,
                f"No rule pack with name {pack_name!r} (runs.db missing; "
                "only the in-package default pack is available before "
                "first scrub run).",
                pack_name=pack_name,
            )

        rules_module = _import_rules()
        if rules_module is not None and hasattr(rules_module, "default_rules"):
            try:
                defaults = rules_module.default_rules()
            except Exception:  # pragma: no cover — defensive
                defaults = []
            for rule in defaults:
                # Pipeline contract: default_rules() returns objects
                # with .rule_id, .name, .severity_default, .language,
                # .description, .enabled attributes (plus .pack_name +
                # .pack_version constants).
                rules_payload.append(
                    {
                        "pack_name": getattr(rule, "pack_name", "default"),
                        "pack_version": getattr(rule, "pack_version", "0.1.0"),
                        "rule_id": getattr(rule, "rule_id", ""),
                        "name": getattr(rule, "name", ""),
                        "severity_default": getattr(
                            rule, "severity_default", "P2"
                        ),
                        "language": getattr(rule, "language", None),
                        "description": getattr(rule, "description", ""),
                        "enabled": bool(getattr(rule, "enabled", True)),
                    }
                )
        # If the rules module isn't ready and DB is missing, return an
        # empty list — NOT an error. The caller can interpret "empty"
        # plus a fresh install as "no rules registered yet".

    structured = {
        "schema_version": SCHEMA_VERSION,
        "rules": rules_payload,
    }

    if not rules_payload:
        summary = (
            "(no rules registered yet — run 'llm-scrub init' to install "
            "default rules into runs.db)"
        )
    else:
        lines = [
            f"  [{r['severity_default']}] {r['rule_id']:<32} "
            f"({r['pack_name']}/{r['pack_version']}) "
            + ("" if r["enabled"] else "DISABLED ")
            + (f"[{r['language']}]" if r["language"] else "[mechanical]")
            for r in rules_payload
        ]
        summary = (
            f"{len(rules_payload)} rule(s):\n" + "\n".join(lines)
        )

    return [TextContent(type="text", text=summary)], structured


async def _tool_scrub_run_status(
    arguments: dict[str, Any],
    db_path: Path,
) -> Any:
    """Implementation of ``scrub_run_status``.

    Returns the row from ``scrub_runs`` matching ``run_id``, or the
    most recent row across all targets if ``run_id`` is None. An
    in-progress run takes precedence over recent finalized runs in
    the latter case so callers can quickly answer "is scrub running
    right now?".
    """
    if not db_path.exists():
        return _error_result(
            ErrorCode.DB_MISSING,
            f"runs.db not found at {db_path}. Run 'llm-scrub init' first.",
            db_path=str(db_path),
        )

    requested_id = arguments.get("run_id")

    conn = db_connect(db_path)
    try:
        if requested_id is not None:
            row = conn.execute(
                "SELECT id, started_at, ended_at, status, target_path, "
                "mode, total_findings, total_redacted "
                "FROM scrub_runs WHERE id = ?",
                (int(requested_id),),
            ).fetchone()
            if row is None:
                return _error_result(
                    ErrorCode.RUN_NOT_FOUND,
                    f"No scrub run with id {requested_id}",
                    run_id=int(requested_id),
                )
        else:
            # Prefer in-progress (pending or running) — they're the
            # interesting ones for "is anything happening now?".
            row = conn.execute(
                "SELECT id, started_at, ended_at, status, target_path, "
                "mode, total_findings, total_redacted "
                "FROM scrub_runs "
                "WHERE status IN ('pending', 'running') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id, started_at, ended_at, status, target_path, "
                    "mode, total_findings, total_redacted "
                    "FROM scrub_runs "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
    finally:
        conn.close()

    run: dict[str, Any] | None = None
    if row is not None:
        run = {
            "id": row["id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "target_path": row["target_path"],
            "mode": row["mode"],
            "total_findings": row["total_findings"],
            "total_redacted": row["total_redacted"],
        }

    structured = {
        "schema_version": SCHEMA_VERSION,
        "run": run,
    }

    if run is None:
        summary = "(no scrub runs recorded yet)"
    else:
        summary = (
            f"run #{run['id']} status={run['status']} "
            f"mode={run['mode']} target={run['target_path']} "
            f"findings={run['total_findings']} "
            f"redacted={run['total_redacted']}"
        )
        if run["ended_at"] is not None:
            summary += f" ended_at={run['ended_at']}"
        else:
            summary += " (in progress)"

    return [TextContent(type="text", text=summary)], structured


# Dispatch table — tool name → handler. Keeps the call_tool decorator
# body short and makes the wire-up testable in isolation.
_DISPATCH: dict[
    str,
    Any,  # async callable (dict, Path) -> tuple | CallToolResult
] = {
    "scrub_scan_text": _tool_scrub_scan_text,
    "scrub_scrub_text": _tool_scrub_scrub_text,
    "scrub_list_rules": _tool_scrub_list_rules,
    "scrub_run_status": _tool_scrub_run_status,
}


# ---------------------------------------------------------------------------
# Severity / mode enum sanity check (compile-time assertion)
# ---------------------------------------------------------------------------
# These constants live in core.types; the schemas hard-code them as enum
# values. Mismatches between the two sources would be silent contract
# breaks — this assertion makes them loud at import time.

_EXPECTED_STATUSES: frozenset[str] = frozenset(s.value for s in ScrubStatus)
_EXPECTED_MODES: frozenset[str] = frozenset(m.value for m in ScrubMode)


def _validate_schema_alignment() -> None:
    """Assert mcp.schemas enums match core.types enums.

    Called at server construction time; raising here is preferable to
    silently shipping a divergent contract.
    """
    from aurochs_llm_scrub.mcp.schemas import OUTPUT_SHAPES

    run_props: dict[str, Any] = (
        OUTPUT_SHAPES.get("scrub_run_status", {})
        .get("properties", {})
        .get("run", {})
        .get("properties", {})
    )
    declared_statuses = frozenset(
        run_props.get("status", {}).get("enum", [])
    )
    declared_modes = frozenset(
        run_props.get("mode", {}).get("enum", [])
    )
    if declared_statuses != _EXPECTED_STATUSES:
        raise RuntimeError(
            "mcp.schemas declares status enum "
            f"{sorted(declared_statuses)!r} but core.types.ScrubStatus is "
            f"{sorted(_EXPECTED_STATUSES)!r}. Update one or the other."
        )
    if declared_modes != _EXPECTED_MODES:
        raise RuntimeError(
            "mcp.schemas declares mode enum "
            f"{sorted(declared_modes)!r} but core.types.ScrubMode is "
            f"{sorted(_EXPECTED_MODES)!r}. Update one or the other."
        )


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def build_server(*, db_path: Path | None = None) -> Any:
    """Construct and return a configured ``mcp.server.lowlevel.Server``.

    Parameters
    ----------
    db_path:
        Override the runs.db location. Default: ``_default_runs_db_path()``.
        Pass an explicit path in tests.

    Returns
    -------
    Server
        The server instance with ``list_tools`` and ``call_tool``
        handlers registered. Caller runs it via ``await server.run(...)``.

    Raises
    ------
    ImportError
        If the Anthropic ``mcp`` SDK is not installed.
    RuntimeError
        If ``mcp.schemas`` enums and ``core.types`` enums diverge — see
        ``_validate_schema_alignment``.
    """
    if not _SDK_AVAILABLE:
        raise ImportError(
            "Anthropic mcp SDK is not installed. Install via: "
            "pip install 'aurochs-llm-scrub[mcp]'"
        )

    _validate_schema_alignment()

    server: Any = Server("aurochs-llm-scrub", version="0.1.0")
    resolved_db = db_path or _default_runs_db_path()

    @server.list_tools()  # type: ignore[untyped-decorator]
    async def _list_tools() -> list[Any]:
        return build_tool_list()

    @server.call_tool(validate_input=True)  # type: ignore[untyped-decorator]
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> Any:
        handler = _DISPATCH.get(name)
        if handler is None:
            return _error_result(
                ErrorCode.INTERNAL_ERROR,
                f"Unknown tool: {name!r}",
                tool_name=name,
            )
        try:
            return await handler(arguments, resolved_db)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("Tool %s raised", name)
            return _error_result(
                ErrorCode.INTERNAL_ERROR,
                f"Unhandled exception in {name}: {exc}",
                tool_name=name,
            )

    return server


async def run_stdio() -> None:  # pragma: no cover — exercised in integration
    """Run the scrub MCP server on stdio.

    Wires the server up to ``mcp.server.stdio.stdio_server()``. Used by
    the ``llm-scrub mcp`` CLI entry point and by Claude Desktop's
    mcpServers config.
    """
    if not _SDK_AVAILABLE:
        raise ImportError(
            "Anthropic mcp SDK is not installed. Install via: "
            "pip install 'aurochs-llm-scrub[mcp]'"
        )
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


def main() -> int:  # pragma: no cover — exercised in integration
    """Synchronous entry point for ``python -m aurochs_llm_scrub.mcp.scrub_server``.

    Returns process exit code.
    """
    import asyncio

    if not _SDK_AVAILABLE:
        sys.stderr.write(
            "aurochs-llm-scrub: Anthropic mcp SDK is not installed.\n"
            "Install via: pip install 'aurochs-llm-scrub[mcp]'\n"
        )
        return 2

    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
