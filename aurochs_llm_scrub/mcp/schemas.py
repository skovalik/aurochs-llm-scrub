"""JSON Schema definitions for MCP tool inputs.

Plan v4 cross-cutting fix #16 — every tool gets a strict JSON Schema for
inputs (``additionalProperties: false``, explicit ``required``) so the
``mcp.server.lowlevel.Server.call_tool(validate_input=True)`` decorator
can reject malformed payloads BEFORE dispatch reaches scrub business
logic.

Why these live in their own module
----------------------------------
The MCP server's tool-list handler returns one ``mcp.types.Tool`` per
exposed tool, and each ``Tool.inputSchema`` is a dict that must match the
arguments the call_tool handler later receives. Keeping the schemas in a
separate module keeps three things in sync:

1. Tool registration (``scrub_server.py`` builds the ``Tool`` list).
2. Input validation (handled by ``Server.call_tool(validate_input=True)``).
3. ``docs/contracts.md`` cross-plugin contract (which embeds these dicts
   verbatim so consumers — voice's overlay layer, recall, future plugins —
   can pin their own client-side validation against the same source of
   truth).

If you change a schema, change the ``SCHEMA_VERSION`` constant and update
``docs/contracts.md`` in the same commit. Future-Claude relies on this
to flag breaking input-shape changes during review.

Naming convention
-----------------
Schema dicts are named ``<tool_name>_input_schema`` so a grep finds them
instantly. The ``TOOL_NAMES`` tuple is the canonical surface; add to it
when adding a new tool, and the test for "every tool has a schema" will
fail loud if you forget.
"""

from __future__ import annotations

from typing import Any

# Bumped on any breaking change to a tool input shape. Output-shape
# changes also bump this — clients reading structuredContent rely on it.
# Plan v4 ScrubReport semver policy:
#   patch = additive optional fields (back-compat with all consumers)
#   minor = additive required fields + migration helper shipped
#   major = breaking rename / removal / semantic shift
SCHEMA_VERSION: str = "0.1.0"

# Canonical severity vocabulary. Clients reading structured outputs
# (and writing severity_filter inputs) MUST treat this as the closed
# enum. New tiers are a contract change; bump SCHEMA_VERSION.
SEVERITY_VALUES: tuple[str, ...] = ("P0", "P1", "P2")

# Canonical tool names. Add a new tool? Add it here AND add a schema below
# AND register it in scrub_server.py's tool list. The
# test_mcp_scrub_server::TestSchemaCompleteness test cross-checks this trio.
TOOL_NAMES: tuple[str, ...] = (
    "scrub_scan_text",
    "scrub_scrub_text",
    "scrub_list_rules",
    "scrub_run_status",
)


# ---------------------------------------------------------------------------
# scrub_scan_text
# ---------------------------------------------------------------------------
# Read-only scan. Returns the list of findings without modifying input.
# Mirrors CLI ``llm-scrub scan`` behavior. Used by Claude sessions that
# want to surface findings to the user without auto-fixing.

scrub_scan_text_input_schema: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {
            "type": "string",
            # Plan v4 #1 / chunking discipline: inputs >2MB get chunked
            # internally with 4KB overlap. We cap at 1 MB at the JSON
            # layer to bound MCP payload size — multi-MB scans should
            # come through scrub_file (CLI-only in v0.1) where the
            # input doesn't have to round-trip through JSON-RPC.
            "minLength": 1,
            "maxLength": 1_048_576,
            "description": "Text to scan for LLM-tell patterns. Returned "
                "findings include offsets, severities, and matched "
                "snippets. The input is NOT mutated. Cap at 1 MB at the "
                "MCP layer; for larger inputs use the CLI.",
        },
        "severity_filter": {
            "type": ["array", "null"],
            "items": {
                "type": "string",
                "enum": list(SEVERITY_VALUES),
            },
            "uniqueItems": True,
            "default": None,
            "description": "Optional list of severity tiers to include "
                "(``P0``, ``P1``, ``P2``). ``null`` or empty = all "
                "tiers. Used by clients that only want hard violations "
                "(``[\"P0\"]``) or skip style suggestions "
                "(``[\"P0\", \"P1\"]``).",
        },
    },
}


# ---------------------------------------------------------------------------
# scrub_scrub_text
# ---------------------------------------------------------------------------
# Read+rewrite. Returns the cleaned text plus the list of findings that
# were applied. Only mechanical/deterministic fixes are made; non-
# mechanical findings are returned for human review.

scrub_scrub_text_input_schema: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1_048_576,
            "description": "Text to scan and mechanically rewrite. The "
                "returned ``cleaned_text`` has all auto-fixable findings "
                "applied (em dashes, whitespace, etc.); non-mechanical "
                "findings remain in the report for the caller to "
                "address by hand.",
        },
        "severity_filter": {
            "type": ["array", "null"],
            "items": {
                "type": "string",
                "enum": list(SEVERITY_VALUES),
            },
            "uniqueItems": True,
            "default": None,
            "description": "Optional severity filter. Filters BOTH the "
                "findings reported AND the rules considered for "
                "auto-fix. ``null`` or empty = all tiers.",
        },
    },
}


# ---------------------------------------------------------------------------
# scrub_list_rules
# ---------------------------------------------------------------------------
# Returns rules currently registered. Used by Claude sessions that want
# to surface "what does this scrubber check for?" or to filter custom
# rules out of a UI.

scrub_list_rules_input_schema: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "pack_name": {
            "type": ["string", "null"],
            # Pack names mirror PyPI-ish slug rules to keep file-system
            # mappings tidy. The default pack ships in-package as
            # ``"default"``; user packs can be ``"stefan-custom"`` etc.
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[a-z0-9][a-z0-9-]*$",
            "default": None,
            "description": "Filter to a single rule pack. ``null`` (the "
                "default) returns rules from ALL registered packs.",
        },
    },
}


# ---------------------------------------------------------------------------
# scrub_run_status
# ---------------------------------------------------------------------------
# Returns the current status of a scrub run. A null run_id returns the
# most-recent run (across all targets).

scrub_run_status_input_schema: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "run_id": {
            "type": ["integer", "null"],
            "minimum": 1,
            "default": None,
            "description": "ID of a specific run to inspect (the integer "
                "PK from runs.db). ``null`` (the default) returns the "
                "most recent run across all targets and modes. The "
                "in-progress run, if any, is preferred over recent "
                "finalized runs.",
        },
    },
}


# ---------------------------------------------------------------------------
# Lookup table — tool name → input schema
# ---------------------------------------------------------------------------
# scrub_server.py uses this to build the Tool list and to surface schemas
# in error messages. Keeping the dict next to the schemas keeps add-a-tool
# diff small (one new schema constant + one new entry here).

INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "scrub_scan_text": scrub_scan_text_input_schema,
    "scrub_scrub_text": scrub_scrub_text_input_schema,
    "scrub_list_rules": scrub_list_rules_input_schema,
    "scrub_run_status": scrub_run_status_input_schema,
}


# ---------------------------------------------------------------------------
# Output shapes (informational; not enforced by Server, but documented in
# docs/contracts.md and asserted by tests)
# ---------------------------------------------------------------------------
# The lowlevel Server's call_tool decorator returns content blocks
# (TextContent | ImageContent | etc.) plus an optional structuredContent
# dict. The structured payloads from each tool follow these shapes:

OUTPUT_SHAPES: dict[str, dict[str, Any]] = {
    "scrub_scan_text": {
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        # Position-tuple identity (file_offset, length,
                        # rule_id) is the cross-chunk dedup key per
                        # plan v4 BLOCKER #1 + #6.
                        "rule_id": {"type": "string"},
                        "pack_name": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": list(SEVERITY_VALUES),
                        },
                        "file_offset": {"type": "integer", "minimum": 0},
                        "length": {"type": "integer", "minimum": 1},
                        "line_number": {"type": "integer", "minimum": 1},
                        "snippet": {"type": "string"},
                    },
                },
            },
            "totals": {
                "type": "object",
                "properties": {
                    "P0": {"type": "integer", "minimum": 0},
                    "P1": {"type": "integer", "minimum": 0},
                    "P2": {"type": "integer", "minimum": 0},
                },
            },
            "auto_disabled_count": {
                # Plan v4 K2: surfaces silent-failure flag when a rule
                # was auto-disabled (ReDoS timeout) during this scan.
                "type": "integer",
                "minimum": 0,
            },
            "language": {
                "type": ["string", "null"],
                "description": "Detected language of the input. ``null`` "
                    "if the language gate could not be evaluated.",
            },
            "redaction_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of secret-scan redactions applied "
                    "to the input before rules ran.",
            },
        },
    },
    "scrub_scrub_text": {
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "properties": {
            "cleaned_text": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {"$ref": "#/scrub_scan_text/properties/findings/items"},
            },
            "fixed_count": {
                "type": "integer",
                "minimum": 0,
                "description": "Number of findings auto-fixed in the "
                    "returned cleaned_text. Non-mechanical findings are "
                    "still surfaced in ``findings`` but were not applied.",
            },
            "totals": {
                "type": "object",
                "properties": {
                    "P0": {"type": "integer", "minimum": 0},
                    "P1": {"type": "integer", "minimum": 0},
                    "P2": {"type": "integer", "minimum": 0},
                },
            },
            "auto_disabled_count": {"type": "integer", "minimum": 0},
        },
    },
    "scrub_list_rules": {
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pack_name": {"type": "string"},
                        "pack_version": {"type": "string"},
                        "rule_id": {"type": "string"},
                        "name": {"type": "string"},
                        "severity_default": {
                            "type": "string",
                            "enum": list(SEVERITY_VALUES),
                        },
                        "language": {"type": ["string", "null"]},
                        "description": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                },
            },
        },
    },
    "scrub_run_status": {
        "schema_version": SCHEMA_VERSION,
        "type": "object",
        "properties": {
            "run": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "integer"},
                    "started_at": {"type": "integer"},
                    "ended_at": {"type": ["integer", "null"]},
                    # ScrubStatus enum — pending/running/success/partial/
                    # failed/aborted. Synced with core.types.ScrubStatus.
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "running",
                            "success",
                            "partial",
                            "failed",
                            "aborted",
                        ],
                    },
                    "target_path": {"type": "string"},
                    # ScrubMode enum — synced with core.types.ScrubMode.
                    "mode": {
                        "type": "string",
                        "enum": [
                            "scan",
                            "scrub-in-place",
                            "scrub-to-stdout",
                            "precommit",
                        ],
                    },
                    "total_findings": {"type": "integer", "minimum": 0},
                    "total_redacted": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Error codes — surfaced in TextContent error blocks
# ---------------------------------------------------------------------------
# The lowlevel Server returns isError=True with a single TextContent on
# input validation failure; we mirror that shape for business-logic
# failures so clients can pattern-match on the prefix.


class ErrorCode:
    """Canonical error-code prefixes for MCP tool error blocks.

    These prefixes are part of the contract — clients read them to
    distinguish "your input was malformed" from "the database is
    missing" from "the pipeline isn't wired yet". Adding a new code is
    a contract change; bump ``SCHEMA_VERSION``.
    """

    # Input failed JSON Schema validation (handled by Server itself; we
    # don't emit this manually but document it for clients).
    INPUT_VALIDATION = "INPUT_VALIDATION"

    # runs.db is missing or unreadable (fresh install? wrong path?).
    # Only relevant for tools that consult run history.
    DB_MISSING = "DB_MISSING"

    # Run id from input does not match any row.
    RUN_NOT_FOUND = "RUN_NOT_FOUND"

    # Pack name from input does not match any registered rule pack.
    PACK_NOT_FOUND = "PACK_NOT_FOUND"

    # Pipeline function not yet wired (transient during parallel build;
    # should be unreachable in a fully wired install). Plan v4 cleanup
    # agent removes the lazy-import shims once all pipeline modules are
    # present.
    PIPELINE_UNAVAILABLE = "PIPELINE_UNAVAILABLE"

    # Generic catch-all for unexpected exceptions.
    INTERNAL_ERROR = "INTERNAL_ERROR"
