# CLI reference

The CLI is the source of truth. The MCP server (`[mcp]` extra) and the
Claude Code plugin both call into the same `core/` API.

## Global flags

| Flag              | What                                                              |
| ----------------- | ----------------------------------------------------------------- |
| `--db-path PATH`  | Override the `runs.db` location. Recognized at top level and after every subcommand. |
| `--version`       | Print version and exit.                                           |
| `--help` / `-h`   | Show help. `llm-scrub --help` lists subcommands.                  |

Set `AUROCHS_LLM_SCRUB_DEBUG=1` in the environment to surface full
tracebacks on uncaught exceptions; without it, the CLI prints a
one-liner and exits with a documented code.

## Commands

### `llm-scrub init`

Bootstrap `runs.db` and the sidecar config files. Idempotent —
re-running on an initialized layout is a no-op.

```bash
llm-scrub init
llm-scrub init --db-path /custom/path/runs.db
```

Creates `runs.db` (with WAL pragmas + foreign-key enforcement), runs
all migrations under `MigrateLock`, registers the bundled default rule
pack, and scaffolds `custom-rules.toml`, `exemptions.toml`,
`config.toml`, `manifest.json`.

### `llm-scrub scan`

Read-only scan. Walks the input file (or directory with
`--recursive`), produces a findings report, never writes back to disk.

```bash
llm-scrub scan path/to/draft.md
llm-scrub scan path/to/draft.md --severity P0
llm-scrub scan path/to/draft.md --json
llm-scrub scan path/to/draft.md --format markdown
llm-scrub scan ./drafts/ --recursive
echo "great question, let me be clear" | llm-scrub scan -
```

| Flag             | Default | What                                                                       |
| ---------------- | ------- | -------------------------------------------------------------------------- |
| `path`           | -       | File, directory (with `--recursive`), or `-` for stdin.                    |
| `--rules NAME`   | default | Rule pack name.                                                            |
| `--severity STR` | -       | Filter to severities (e.g. `P0` or `P0,P1`). Non-matching findings exit 0. |
| `--json`         | false   | Emit `ScrubReport` JSON.                                                   |
| `--format FMT`   | plain   | `plain` / `json` / `markdown`. Overrides `--json`.                         |
| `--recursive`    | false   | Walk directory recursively (default glob `**/*.md`).                       |
| `--hard-fail`    | false   | Exit 5 when any P0 finding is emitted, regardless of `--severity`.         |
| `--quiet`        | false   | Suppress P2 redundancy warnings (passthrough in v0.1).                     |

Exit codes: `0` clean or no severity match, `5` findings present that
match `--severity` filter or `--hard-fail` and any P0, `2` invalid
severity / file-not-found.

### `llm-scrub scrub`

Scan plus apply mechanical fixes. Auto-fixable rules in v0.1:
`p0-em-dash`, `p0-mdash-entity`, `p0-double-space`,
`p0-trailing-whitespace`. Non-mechanical findings are reported but
the cleaned text leaves them intact.

```bash
llm-scrub scrub draft.md --in-place                    # rewrite + .bak sidecar
llm-scrub scrub draft.md --to-stdout                   # print cleaned, leave file
llm-scrub scrub draft.md --in-place --backup-mode fail # refuse if .bak exists
llm-scrub scrub draft.md --in-place --no-backup        # skip .bak entirely
echo "draft" | llm-scrub scrub -                       # stdin → stdout
```

| Flag                  | Default | What                                                                       |
| --------------------- | ------- | -------------------------------------------------------------------------- |
| `path`                | -       | File to scrub. `-` for stdin (writes to stdout).                           |
| `--in-place`          | false   | Overwrite the original. mtime check + `.bak` per `--backup-mode`.          |
| `--to-stdout`         | true    | Print cleaned text; leave original untouched (default).                    |
| `--backup-mode MODE`  | rotate  | `rotate` (rename existing `.bak` to `.bak.<ts>`), `fail`, `overwrite`.     |
| `--no-backup`         | false   | Skip `.bak` entirely. Requires `--in-place`.                               |
| `--rules NAME`        | default | Rule pack name.                                                            |
| `--severity STR`      | -       | Filter findings to severities (same parsing as `scan`).                    |
| `--diff`              | false   | Print unified diff to stderr after scrubbing (passthrough in v0.1).        |
| `--quiet`             | false   | Suppress P2 redundancy warnings (passthrough in v0.1).                     |

`--in-place` and `--to-stdout` are mutually exclusive. The `.bak`
collision policy is locked: `rotate` is the default per plan v4
BLOCKER #9.

### `llm-scrub install-precommit`

Install a `.git/hooks/pre-commit` script that runs `llm-scrub scan`
against staged files.

```bash
llm-scrub install-precommit
llm-scrub install-precommit --mode hard
llm-scrub install-precommit --force
llm-scrub install-precommit --repo-root /path/to/repo
```

| Flag             | Default | What                                                                  |
| ---------------- | ------- | --------------------------------------------------------------------- |
| `--mode MODE`    | soft    | `soft` (only fail on redacted secrets), `hard` (also fail on any P0). |
| `--repo-root P`  | auto    | Override repo root (default: walk up from cwd looking for `.git/`).   |
| `--force`        | false   | Overwrite an existing pre-commit hook.                                |

The hook script is portable across POSIX shells; on Windows, git for
Windows runs `.git/hooks/pre-commit` via the bundled MSYS bash. The
hook sets `LLM_SCRUB_PRE_COMMIT=1` in its environment so the engine
treats secret-scan redactions as commit blockers.

### `llm-scrub rules`

Rule pack and rule management.

```bash
llm-scrub rules list
llm-scrub rules list --disabled
llm-scrub rules list --pack default --json
llm-scrub rules show p0-em-dash
llm-scrub rules show p0-em-dash --pack default --json
llm-scrub rules enable p0-em-dash
llm-scrub rules disable p1-vague-quantifier --reason "editorial"
llm-scrub rules migrate --from 0.1.0 --to 0.2.0
```

`list` prints a fixed-width table of pack / rule_id / severity /
language / status. `show` prints the full rule definition including
the regex pattern.

`migrate` is a stub in v0.1 — only one schema version exists. Land
in v0.2 along with the first pack-format bump.

### `llm-scrub runs`

Inspect scrub-run history.

```bash
llm-scrub runs list
llm-scrub runs list --limit 20 --target draft.md --status partial
llm-scrub runs show 42
llm-scrub runs diff 41 42
llm-scrub runs show 42 --json
```

`list` prints the most recent runs (default limit 50). `show` prints
the run header plus up to 200 findings, truncating the rest. `diff`
compares two runs by the finding key tuple `(file_path, rule_id,
line_number, severity, snippet)` and prints `resolved` (in A, not B)
and `regressed` (in B, not A) lists.

### `llm-scrub verify`

Integrity checks on `runs.db`.

```bash
llm-scrub verify
llm-scrub verify --deep
```

Always-on basic checks: foreign-key violations, empty `findings.snippet`
rows, malformed `rule_packs.sha256` rows, orphan `schema_version`
rows (status `in_progress`), orphan `scrub_runs` rows (pending /
running with no `ended_at`).

`--deep` defers to `core.recovery.verify(db_path, deep=True)` once
that path is wired — adds WAL walk, content-hash checks, orphan
findings, dangling rule metrics, unmatched disabled-log entries.

Exit codes: `0` clean, `1` issues found, `5` orphan migration state
(needs manual recovery — see [migrations.md](migrations.md)).

### `llm-scrub status`

Version + DB stats + disabled-rule count. Read-only; no locks.

```bash
llm-scrub status
llm-scrub status --json
```

Prints version, `runs.db` path + size, schema version, rule pack
count, rule counts (active / total / disabled), scrub-run count,
disabled-log size, last-run summary. Exits 1 if `runs.db` doesn't
exist.

### `llm-scrub version`

Prints the version and exits. Same output as `--version`.

## Exit codes (locked)

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| 0    | Success.                                                             |
| 1    | Generic failure.                                                     |
| 2    | Usage error / invalid arguments / file not found.                    |
| 3    | DB lock contention (`LockError` from `aurochs-core`).                |
| 4    | Schema version mismatch (`manifest.schema_version` ahead of plugin). |
| 5    | `scan`: findings matched `--severity` or `--hard-fail` saw a P0.     |
| 6    | Pre-commit hook found findings (P0 = blocker per plan v4 K3).        |
| 130  | Interrupted (SIGINT / Ctrl-C).                                       |

## Pre-commit integration

The hook installer writes a script that loops over staged files and
calls `llm-scrub scan $FILE`. Soft mode lets the commit proceed unless
a secret was redacted. Hard mode also blocks on any P0 finding. The
hook is editable — open `.git/hooks/pre-commit` and adjust as needed;
just keep the `aurochs-llm-scrub: pre-commit` marker comment so
re-running `install-precommit` knows to refresh the right block.
