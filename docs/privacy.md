# Privacy

aurochs-llm-scrub is **explicitly** designed not to transmit your data
anywhere.

## What it does

- Reads the file (or stdin) you pass to `scan` / `scrub`.
- Writes findings to a local SQLite database under your user data
  directory.
- Optionally rewrites the input file in place with mechanical fixes
  (em dashes → comma, double spaces → single, etc.).

## What it does not do

- **No telemetry.** Zero phone-home, zero opt-in analytics, zero crash
  reporting.
- **No LLM calls.** v0.1 is pure regex + langdetect. There is no
  LLM-as-judge, no API call to any model, no BYOK key path. The
  judgment is entirely in the rule pack.
- **No background uploads.** Nothing in `core/` or `cli/` initiates
  outbound HTTP. Period.
- **No vendor lock-in.** The `runs.db` file is just SQLite. You can
  read it with the standard `sqlite3` CLI, dump it to JSON, or migrate
  to whatever else you want. Nothing about the format depends on this
  package being installed.
- **No cloud component.** There is no aurochs-llm-scrub server. There
  is no aurochs-llm-scrub account. There is no API at any domain we
  control.

## What gets written to disk

| Data                              | Where it goes                                          |
| --------------------------------- | ------------------------------------------------------ |
| Your prose (the scrub target)     | Read-only in `scan` mode; rewritten in place in `scrub --in-place` (with `.bak` rotation by default). |
| Findings (matched snippets)       | `runs.db` — one row per finding, snippet column carries the verbatim match. |
| Run history                       | `runs.db` `scrub_runs` table — target_path, mode, status, started_at, ended_at, total counts. |
| Auto-disable audit                | `runs.db` `disabled_rules_log` + plain-text mirror at `<data_dir>/disabled-rules.log`. |
| Schema state                      | `runs.db` `schema_version` table.                      |
| Backup files                      | `<file>.bak` next to the original (or `<file>.bak.<ts>` after rotation). |

## What snippets get redacted

The `[redact]` section of `config.toml` controls secret-scan
redaction:

```toml
[redact]
enabled = true
patterns = ["aws", "github", "openai", "anthropic", "high-entropy"]
```

Before any finding's `snippet` field is persisted, it passes through
the secret-scan redactor. High-entropy / known-key substrings are
replaced with `[REDACTED:CLASS]`. The replacement is permanent — the
original substring is never written to `runs.db`.

The pre-commit hook sets `LLM_SCRUB_PRE_COMMIT=1` in its environment.
When set, the engine treats secret-scan redactions as commit blockers
(plan v4 K3 lock): if a redaction fires, the hook exits non-zero and
the commit is refused. This catches API keys and similar accidents
before they land in git history.

## CI lint enforcement

The package's CI pipeline includes a lint step that asserts no
outbound HTTP in `core/` or `cli/`. The whitelist is `[]` for v0.1.
Future extras that hit external services (the deferred LLM-as-judge
extra, for example) will live behind opt-in extras and document their
network behavior here.

## What's not yours to remove

The `runs.db` SQLite file lives on your disk and you own it. Delete
it whenever you want — `llm-scrub init` will rebuild it from scratch.
The same applies to `<file>.bak` sidecars, `disabled-rules.log`, and
the entire `<data_dir>` directory. The package never holds anything
you can't delete with `rm`.
