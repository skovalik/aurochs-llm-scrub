# Install

aurochs-llm-scrub is a Python package. Requires Python 3.13 or newer.

## Core install

```bash
pip install aurochs-llm-scrub
```

That gives you the CLI, the default rule pack, the SQLite-backed
`runs.db`, the redactor, and the pre-commit hook. Core install has
four runtime dependencies: `aurochs-core` (DB primitives + locks),
`psutil` (lockfile holder PID checks), `platformdirs` (cross-OS
data-dir resolution), `langdetect` (the language gate).

## Extras

| Extra      | What it adds                                                                  |
| ---------- | ----------------------------------------------------------------------------- |
| `[redos]`  | `redos-detector` — static ReDoS pre-check at rule-load time. Best-effort.     |
| `[dev]`    | pytest, pytest-cov, ruff, mypy, types-psutil.                                 |
| `[all]`    | Currently equivalent to `[redos]`; will broaden in v0.2.                      |

```bash
pip install "aurochs-llm-scrub[redos]"
```

The `[redos]` extra is genuinely optional. The engine's load-bearing
ReDoS defense is the per-rule wall-clock timeout (see
[failure-modes.md](failure-modes.md)). The static pre-check is a
second line of defense; missing it does not block scanning.

## First run

```bash
llm-scrub init
```

This creates the `runs.db` SQLite file, registers the bundled default
rule pack, and scaffolds three starter config files: `custom-rules.toml`,
`exemptions.toml`, `config.toml`. Re-running `init` on an
already-initialized layout is idempotent (no overwrites, no warnings).

Output is intentionally terse — one line per object created:

```
runs.db          /home/you/.local/share/aurochs-llm-scrub/runs.db
custom-rules     /home/you/.local/share/aurochs-llm-scrub/custom-rules.toml
exemptions       /home/you/.local/share/aurochs-llm-scrub/exemptions.toml
config           /home/you/.local/share/aurochs-llm-scrub/config.toml
data_dir         /home/you/.local/share/aurochs-llm-scrub
```

## Where the data lives

| Path                                                              | What                                              |
| ----------------------------------------------------------------- | ------------------------------------------------- |
| `~/.local/share/aurochs-llm-scrub/runs.db` (Linux)                | SQLite store — runs, findings, rules, audit log   |
| `~/Library/Application Support/aurochs-llm-scrub/runs.db` (macOS) | Same                                              |
| `%LOCALAPPDATA%\aurochs-llm-scrub\runs.db` (Windows)              | Same                                              |
| `<data_dir>/custom-rules.toml`                                    | User-authored rules (see [rules.md](rules.md))    |
| `<data_dir>/exemptions.toml`                                      | Workspace exemption baseline                      |
| `<data_dir>/config.toml`                                          | Engine config + `[severity_overrides]`            |
| `<data_dir>/disabled-rules.log`                                   | ReDoS auto-disable audit log (plain-text mirror)  |
| `<data_dir>/manifest.json`                                        | Cross-plugin schema_version pin                   |

Override with `--db-path` on any command, or set
`AUROCHS_LLM_SCRUB_DB` (database path) /
`AUROCHS_LLM_SCRUB_HOME` (data dir) in the environment.

## Verifying install

```bash
llm-scrub status
```

Reports the version, `runs.db` path + size, schema version, rule pack
count, rule counts (active / total / disabled), scrub-run count, and
the most recent run summary. If the DB doesn't exist yet, `status`
exits 1 with a one-line "run `llm-scrub init` first" message.

```bash
llm-scrub verify
```

Runs integrity checks (FK violations, empty snippets, malformed
sha256, orphan migration state). Add `--deep` once `core.recovery` is
wired to add WAL walk + content-hash checks.

If something looks wrong, see [failure-modes.md](failure-modes.md).
