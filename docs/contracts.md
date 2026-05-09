# External contracts

This page is the contract surface that other tools depend on. Anything
documented here is part of the public API of `aurochs-llm-scrub` and
follows [semver](https://semver.org/) at the major-version level. The
`schema_version` field embedded in every MCP tool response is bumped on
any breaking change.

> **Plan v4 reference:** cross-cutting fix #16 (MCP tool JSON Schemas
> pinned), cross-cutting fix #14 (canonical schema versioning enforced),
> cross-cutting fix #19 (`platformdirs` cross-OS data dir).

## Stability tiers

- **Stable** — semver major break only. MCP tool input shapes (the JSON
  Schemas below), output `structuredContent` shapes, error code names
  in `mcp.schemas.ErrorCode`, the on-disk SQLite schema (with migration
  coverage), and the public `core/` API surface (`Rule`, `RulePack`,
  `ScrubRun`, `Finding`, `RuleMetrics`, `DisabledRuleEntry`, `ScrubMode`,
  `ScrubStatus`, `db_connect`, `compute_finding_sha256`,
  `normalize_text`, `MigrationRunner`, the locks).
- **Experimental** — minor-version breakable with a CHANGELOG note.
  The `language` enum on rules (currently `None | "en"`; future packs
  may add `"de"` etc.), the `source` field on `disabled_rules_log`
  reasons, the `structuredContent` summary text wording (the *fields*
  are stable; free-text human summaries are not).
- **Internal** — anything not on this page.

## ScrubReport semver policy

The `ScrubReport` shape (returned by `scrub_scan_text` and
`scrub_scrub_text`) carries a `schema_version: str` field. Plan v4
locks the policy as a three-tier discipline:

- **Patch bump (`0.1.0` → `0.1.1`)** — additive optional fields only.
  Backward-compatible with all existing consumers. Voice's overlay
  layer needs no changes.
- **Minor bump (`0.1.x` → `0.2.0`)** — additive required fields with a
  migration helper. Schema gains a required field; ships with
  `migrate_v0_1_to_v0_2(report: dict) -> dict` helper that fills the
  new required field with a documented default. Voice overlay either
  upgrades to call the helper at the seam or pins to old minor.
- **Major bump (`0.x.y` → `1.0.0`)** — breaking field rename, removal,
  or semantic shift. Voice overlay must update or pin to old major.
  Migration helpers shipped for one major back; older history is
  considered out-of-band.

`schema_version: str` is parsed as a semver string; aurochs-voice's
overlay layer can consume `ScrubReport` knowing exactly what triggers
a major bump.

## Cross-plugin contract

`aurochs-llm-scrub`, `aurochs-voice`, `aurochs-recall` (and future
siblings — `aurochs-seo-geo`) share a manifest at
`platformdirs.user_data_dir('aurochs-llm-scrub')/manifest.json` that
records each plugin's `schema_version`. Plugins refuse to operate on a
`schema_version` newer than their compiled-in version; this page is the
source of truth for what each version promises.

```json
{
  "scrub": {"schema_version": "0.1.0", "db_schema": 1},
  "voice": {"schema_version": "0.1.0", "db_schema": 1},
  "recall": {"schema_version": "0.5.0", "db_schema": 1}
}
```

## MCP server tool surface

The MCP server ships in the base install. Run it via `python -m
aurochs_llm_scrub.mcp.scrub_server` or register it in Claude Desktop's
`mcpServers` config. The server speaks stdio JSON-RPC per the Model
Context Protocol spec.

All tool inputs are validated against their JSON Schema by the lowlevel
`Server.call_tool(validate_input=True)` decorator before dispatch. Tools
receive only validated payloads.

### Schema version

All structured outputs include `"schema_version": "0.1.0"`. Bumping
either an input schema or an output shape requires bumping
`mcp.schemas.SCHEMA_VERSION` and updating this page.

### Severity vocabulary

The closed enum is `P0 | P1 | P2`:

- **P0** — Hard violations. Em dashes, double space, trailing
  whitespace, banned buzzwords. Mechanical P0s have auto-fix; English-
  coded P0s do not.
- **P1** — LLM signposting. "We" pronoun, "That's the X." patterns,
  hedging, transition filler, vague quantifiers, corporate filler.
  Default P1 — voice's overlay can promote to P0.
- **P2** — Style suggestions. Sentence variance, paragraph length,
  significance-inflation patterns. Informational only in v0.1; opt-in
  to NLP-aware detection via the `[nlp]` extra in v0.2.

JSON output uses `P0/P1/P2` codes; markdown output uses both codes and
human-readable category labels (`## Hard Violations (P0)`); plain-text
terminal output uses labels only.

### Tool: `scrub_scan_text`

Read-only scan. Returns the list of findings without modifying the
input.

**Input schema:**

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["text"],
  "properties": {
    "text": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1048576
    },
    "severity_filter": {
      "type": ["array", "null"],
      "items": {"type": "string", "enum": ["P0", "P1", "P2"]},
      "uniqueItems": true,
      "default": null
    }
  }
}
```

**Output content blocks:** one `TextContent` per finding (formatted
header + snippet) plus a final `TextContent` summary with
`P0=N P1=N P2=N` totals and (when applicable) `redacted=N` and
`auto_disabled=N (see 'llm-scrub rules list --disabled')`.

If no findings, a single `TextContent` with `(no findings)` is
returned.

**Output `structuredContent`:**

```json
{
  "schema_version": "0.1.0",
  "findings": [
    {
      "rule_id": "p0-em-dash",
      "pack_name": "default",
      "severity": "P0",
      "file_offset": 5,
      "length": 1,
      "line_number": 1,
      "snippet": "Hello —world"
    }
  ],
  "totals": {"P0": 1, "P1": 0, "P2": 0},
  "auto_disabled_count": 0,
  "language": "en",
  "redaction_count": 0
}
```

Identity tuple per finding is `(file_offset, length, rule_id)` — plan
v4 BLOCKER #1 + #6 cross-chunk dedup key. `language` is the detected
language of the input (may be `null` if the gate could not evaluate);
`redaction_count` is the number of secret-scan redactions applied to
the input before rules ran (plan v4 K7). `auto_disabled_count > 0`
signals at least one rule was disabled by the ReDoS detector during
this scan — the K2 lock requires this to surface in the summary footer.

**Pipeline binding:** the MCP layer calls
`aurochs_llm_scrub.core.scrubber.scan_text_report(text, *,
severity_filter, db_path)` — an MCP-shaped wrapper around the
lower-level `scan_text(db_path, text, rules, ...)` (which returns
`(ScrubRun, list[Finding])` for engine-internal callers). The
wrapper-name pattern keeps the engine's tuple-returning entrypoint
useful for CLI / engine tests while the MCP layer always sees the
dict-shaped report.

**Errors:**

- `PIPELINE_UNAVAILABLE` — `core.scrubber.scan_text_report` not yet
  wired (transient during parallel build).
- `INPUT_VALIDATION` — schema-rejected input (handled by Server
  itself).
- `INTERNAL_ERROR` — pipeline raised an unexpected exception.

### Tool: `scrub_scrub_text`

Read+rewrite. Returns the cleaned text alongside the findings (only
mechanical/deterministic fixes are applied; non-mechanical findings are
returned for human review).

**Input schema:**

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["text"],
  "properties": {
    "text": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1048576
    },
    "severity_filter": {
      "type": ["array", "null"],
      "items": {"type": "string", "enum": ["P0", "P1", "P2"]},
      "uniqueItems": true,
      "default": null
    }
  }
}
```

**Output content blocks:** one `TextContent` summary block with
`fixed=N · P0=N P1=N P2=N · auto_disabled=N` (auto-disable suffix only
when nonzero) followed by one `TextContent` per finding.

**Output `structuredContent`:**

```json
{
  "schema_version": "0.1.0",
  "cleaned_text": "...",
  "findings": [/* same shape as scrub_scan_text */],
  "fixed_count": 1,
  "totals": {"P0": 1, "P1": 0, "P2": 0},
  "auto_disabled_count": 0
}
```

`cleaned_text` has all auto-fixable findings (mechanical / language=
None rules with an `auto_fix` spec) applied. The `findings` list
includes BOTH the fixed findings AND the non-mechanical ones the
caller still needs to address. `fixed_count` ≤ `findings.length`.

**Pipeline binding:** the MCP layer calls
`aurochs_llm_scrub.core.scrubber.scrub_text_report(text, *,
severity_filter, db_path)`.

**Errors:**

- `PIPELINE_UNAVAILABLE` — `core.scrubber.scrub_text_report` not yet
  wired.
- `INPUT_VALIDATION` — schema-rejected input.
- `INTERNAL_ERROR` — pipeline raised an unexpected exception.

### Tool: `scrub_list_rules`

Returns rules currently registered in `runs.db`. If the DB is missing
(fresh install, never ran scrub) and the `pack_name` filter is `null`
or `"default"`, falls back to the in-package default rule pack via
`core.rules.default_rules()` so a fresh install can answer "what does
this scrubber check?" without first running migrations.

**Input schema:**

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "pack_name": {
      "type": ["string", "null"],
      "minLength": 1,
      "maxLength": 64,
      "pattern": "^[a-z0-9][a-z0-9-]*$",
      "default": null
    }
  }
}
```

**Output content blocks:** a single `TextContent` table summary, e.g.:

```
3 rule(s):
  [P0] p0-em-dash                      (default/0.1.0) [mechanical]
  [P0] p0-buzzword-leverage            (default/0.1.0) [en]
  [P1] p1-pronoun-we                   (default/0.1.0) [en]
```

Disabled rules carry a `DISABLED` marker before the language tag.

**Output `structuredContent`:**

```json
{
  "schema_version": "0.1.0",
  "rules": [
    {
      "pack_name": "default",
      "pack_version": "0.1.0",
      "rule_id": "p0-em-dash",
      "name": "Em dashes",
      "severity_default": "P0",
      "language": null,
      "description": "Em dashes are LLM-tell punctuation.",
      "enabled": true
    }
  ]
}
```

`language: null` means the rule is mechanical (always-on regardless of
language gate); `language: "en"` (etc.) means the rule is gated on
detected language matching.

**Errors:**

- `PACK_NOT_FOUND` — supplied `pack_name` does not match any
  registered pack. (When `runs.db` is missing, only `pack_name="default"`
  or `null` succeeds via the in-package fallback; any other pack name
  returns `PACK_NOT_FOUND`.)
- `INPUT_VALIDATION` — schema-rejected input.

### Tool: `scrub_run_status`

Returns the row from `scrub_runs` matching `run_id`, or the most
recent run across all targets if `run_id` is `null`. An in-progress
run takes precedence over recent finalized runs in the latter case.

**Input schema:**

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "run_id": {
      "type": ["integer", "null"],
      "minimum": 1,
      "default": null
    }
  }
}
```

**Output content blocks:** a single `TextContent` summary line:

```
run #42 status=running mode=scan target=/tmp/draft.md findings=0 redacted=0 (in progress)
```

or:

```
run #41 status=success mode=scan target=/tmp/draft.md findings=7 redacted=0 ended_at=1700000150
```

If no runs are recorded yet, the text is `(no scrub runs recorded yet)`.

**Output `structuredContent`:**

```json
{
  "schema_version": "0.1.0",
  "run": {
    "id": 42,
    "started_at": 1700000200,
    "ended_at": null,
    "status": "running",
    "target_path": "/tmp/longer-draft.md",
    "mode": "scan",
    "total_findings": 0,
    "total_redacted": 0
  }
}
```

`run` is `null` when no rows match (and `run_id` was `null` — an
explicit `run_id` that doesn't match returns `RUN_NOT_FOUND` instead).

**ScrubStatus enum** (synced with `core.types.ScrubStatus`):
`pending | running | success | partial | failed | aborted`.

**ScrubMode enum** (synced with `core.types.ScrubMode`):
`scan | scrub-in-place | scrub-to-stdout | precommit`.

The MCP server runs `_validate_schema_alignment()` at construction
time so a divergence between `core.types` and `mcp.schemas` raises
loud at server startup rather than shipping a silently broken
contract.

**Errors:**

- `DB_MISSING` — `runs.db` not found at the resolved path.
- `RUN_NOT_FOUND` — supplied `run_id` does not match any row.
- `INPUT_VALIDATION` — schema-rejected input.

## Error block shape

All error responses are `CallToolResult` with `isError=true` containing
a single `TextContent` block whose `.text` is JSON-encoded:

```json
{
  "error": {
    "code": "RUN_NOT_FOUND",
    "message": "No scrub run with id 999",
    "schema_version": "0.1.0",
    "details": {"run_id": 999}
  }
}
```

The `code` value is one of `mcp.schemas.ErrorCode`:

| Code | Meaning |
|---|---|
| `INPUT_VALIDATION` | Input failed JSON Schema; emitted by lowlevel `Server` itself, not by tool handlers. |
| `DB_MISSING` | `runs.db` not found at resolved path. |
| `RUN_NOT_FOUND` | Supplied `run_id` does not match any row. |
| `PACK_NOT_FOUND` | Supplied `pack_name` does not match any registered rule pack. |
| `PIPELINE_UNAVAILABLE` | Pipeline function not yet wired (transient during parallel build; should be unreachable in a fully wired install). |
| `INTERNAL_ERROR` | Catch-all for unexpected exceptions. |

## Database resolution

The MCP server resolves `runs.db` in this order (plan v4 cross-cutting
#19):

1. `AUROCHS_LLM_SCRUB_DB` environment variable (test override / non-
   default install).
2. `platformdirs.user_data_dir("aurochs-llm-scrub")/runs.db`. On
   Windows this resolves to `%LOCALAPPDATA%\aurochs-llm-scrub\runs.db`;
   on POSIX-like systems to `~/.local/share/aurochs-llm-scrub/runs.db`
   (Linux) or
   `~/Library/Application Support/aurochs-llm-scrub/runs.db` (macOS).

## Bridge to aurochs-voice

When both packages are installed, voice's scrub mode imports the
`ScrubReport` shape (the `structuredContent` from `scrub_scan_text` /
`scrub_scrub_text`) and calls llm-scrub's engine directly (no
subprocess). Voice then:

1. Reads `ScrubReport.schema_version` and adapts:
   - Same major + same-or-lower minor → consume directly.
   - Higher minor → call applicable migration helper at the seam.
   - Higher major → either upgrade voice or pin llm-scrub to old
     major.
2. Receives baseline `ScrubReport` from llm-scrub (universal LLM
   tells).
3. Applies its overlay layer:
   - Promotes "we" pronoun from P1 → P0 (Stefan-strict).
   - Adds Stefan-banned-FOR-HIM rules (BOOK-PROTOCOL extensions).
   - Adds register-mismatch rules (recipient-aware).
4. Returns enriched report to voice's caller.

`aurochs-llm-scrub` never depends on `aurochs-voice`. Voice depends on
llm-scrub via the versioned protocol contract documented above.

## TODOs (placeholders for future surfaces)

- **Claude Code plugin manifest** (`plugin.json`) — populated when the
  `:scrub` command lands.
- **Hook stdin/stdout contract** — populated when the pre-commit hook
  contract is finalized (plan v4 BLOCKER #5 / K3).
- **Resource templates** — scrub may expose `scrub://rule/<rule-id>`
  resources later; not in T0 MVP.
- **Notification channels** — `notifications/tools/list_changed` is
  not emitted in T0; if a future write path mutates the tool list at
  runtime, register it here.
- **`scrub_file` tool** — present in early drafts of this plan as an
  MCP tool; deferred to v0.2 because multi-MB file inputs round-trip
  awkwardly through JSON-RPC. Use the CLI for files >1 MB; MCP tools
  cap at the 1 MB string limit.
- **`auto_fix` tool** — present in early drafts as a separate MCP
  tool; subsumed by `scrub_scrub_text` in this T0 MCP surface. The
  `--suggest` non-mechanical hint mode is CLI-only in v0.1.
