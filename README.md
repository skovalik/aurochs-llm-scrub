# aurochs-llm-scrub

**Detect and scrub LLM writing patterns from your prose.**

Regex-and-rules pipeline. Catches the AI tells before they ship. Local
SQLite. No LLM-as-judge in v0.1 — pure pattern detection, deterministic
and auditable.

*MIT · v0.1 · public from commit 1 · Stefan Kovalik <stefan@aurochs.agency>*

---

I kept noticing the same handful of LLM tells in my own writing — the
unprompted "It's worth noting that...", the symmetric "not just X, but
Y" cadence, the conclusion that summarized the conclusion, the em-dash
overuse. I'd written enough by hand to know which ones were mine and
which were drift. So I built a regex-and-rules pipeline that catches
the drift before it ships, plus a pre-commit hook so I don't have to
remember to run it.

The judgment is in the rule pack. The engine just runs the rules
quickly and deterministically, with a wall-clock timeout that kills
catastrophic regex backtracking before it kills your machine. Custom
rules go in `~/.aurochs-llm-scrub/custom-rules.toml`.

---

**Privacy:** aurochs-llm-scrub does not transmit your data anywhere.
There is no telemetry, no analytics, no crash reporting, no cloud
calls. v0.1 makes zero outbound network calls — pure regex +
langdetect. The `runs.db` file lives on your disk and you can read it
with the standard SQLite CLI.

## Use as a Claude Code plugin

```bash
/plugin install aurochs-llm-scrub
```

Then in any Claude Code session:

- `/aurochs-llm-scrub <args>` — full CLI surface (any subcommand).
- `/scrub <file>` — quick scan shortcut.

The plugin auto-wires the llm-scrub MCP server, so Claude can also
invoke the four scrub tools natively. See
[`docs/contracts.md`](docs/contracts.md) for the MCP tool surface.

## 30-second quickstart

```bash
pip install aurochs-llm-scrub
llm-scrub init                          # creates runs.db, default rule pack
llm-scrub scan path/to/draft.md         # report findings, no modification
llm-scrub scrub --in-place draft.md     # apply fixes; backs up to draft.md.bak
llm-scrub install-precommit             # add to .git/hooks/pre-commit
```

## What this isn't

- Not a grammar checker (Grammarly's market). Doesn't flag passive
  voice or comma splices.
- Not an AI-detection tool for catching other people's writing
  (Originality.ai / GPTZero / Copyleaks market). Won't tell you if
  someone else's essay was written by GPT.
- Not a paraphraser. It removes patterns; it doesn't rewrite voice.
  The voice is yours.
- Not LLM-as-judge in v0.1 — pure pattern detection (faster,
  deterministic, auditable). LLM-as-judge is a v0.2 opt-in extra.
- Not opinionated about whose voice you want. The default rule pack is
  Stefan's curation; the custom-rules path is the extension point.
- Not cloud-dependent. The `runs.db` file lives on your disk. You can
  read it with `sqlite3` and delete it with `rm`.

## Install

```bash
pip install aurochs-llm-scrub              # core: CLI + MCP server + default pack + runs.db + redactor + hook
pip install "aurochs-llm-scrub[redos]"     # plus static ReDoS pre-check
pip install "aurochs-llm-scrub[all]"       # everything above at once
```

Optional extras:

| Extra      | What it adds                                                                  |
| ---------- | ----------------------------------------------------------------------------- |
| `[redos]`  | `redos-detector` — static ReDoS pre-check at rule-load time. Best-effort; the engine's load-bearing defense is the per-rule wall-clock timeout. |
| `[dev]`    | pytest, pytest-cov, ruff, mypy, types-psutil.                                 |
| `[all]`    | Currently equivalent to `[redos]`; will broaden in v0.2.                      |

Requires Python 3.13 or newer. Linux / macOS / Windows.

## Usage

```bash
llm-scrub init                             # bootstrap runs.db + sidecar configs (idempotent)
llm-scrub scan draft.md                    # default mode: report findings to stdout
llm-scrub scan draft.md --severity P0      # only show hard violations
llm-scrub scan draft.md --json             # ScrubReport JSON
llm-scrub scan ./drafts/ --recursive       # walk a directory of *.md
llm-scrub scrub draft.md --in-place        # rewrite + .bak rotation
llm-scrub scrub draft.md --to-stdout       # print cleaned text; leave file
echo "great question" | llm-scrub scan -   # stdin mode

llm-scrub install-precommit                # soft mode: only fail on redacted secrets
llm-scrub install-precommit --mode hard    # also fail on any P0 finding

llm-scrub rules list                       # registered rules + status
llm-scrub rules show p0-em-dash            # full definition for one rule
llm-scrub rules disable p1-corporate-filler --reason "editorial style"
llm-scrub rules enable p1-corporate-filler

llm-scrub runs list --limit 20             # recent scrub-run history
llm-scrub runs show 42                     # one run with all findings
llm-scrub runs diff 41 42                  # what changed between runs

llm-scrub status                           # version + DB stats + rule counts
llm-scrub verify                           # FK + format integrity checks
llm-scrub verify --deep                    # adds WAL walk + content-hash checks
```

The CLI is the source of truth. The MCP server and the Claude Code
plugin both call into the same `core/` API. Full flag reference:
[`docs/cli.md`](docs/cli.md).

Exit codes are locked:

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| 0    | Success.                                                             |
| 1    | Generic failure.                                                     |
| 2    | Usage error / invalid arguments.                                     |
| 3    | DB lock contention.                                                  |
| 4    | Schema version mismatch.                                             |
| 5    | `scan`: findings matched `--severity` or `--hard-fail` saw a P0.     |
| 6    | Pre-commit hook found findings (P0 = blocker).                       |

## Configuration

`llm-scrub init` writes four files into your data directory:

- `runs.db` — SQLite store for runs, findings, rules, audit log.
- `custom-rules.toml` — your rule pack. Empty starter; add `[[rule]]`
  entries.
- `exemptions.toml` — workspace-wide rule exemptions.
- `config.toml` — engine config: timeout, chunk size, language gate
  threshold, secret-scan patterns, severity overrides.

Default location:

| OS      | Path                                                          |
| ------- | ------------------------------------------------------------- |
| Linux   | `~/.local/share/aurochs-llm-scrub/`                           |
| macOS   | `~/Library/Application Support/aurochs-llm-scrub/`            |
| Windows | `%LOCALAPPDATA%\aurochs-llm-scrub\`                           |

Override with `--db-path` on any command, or set
`AUROCHS_LLM_SCRUB_DB` (database path) /
`AUROCHS_LLM_SCRUB_HOME` (data dir) in the environment.

Custom rule TOML schema:

```toml
[[rule]]
rule_id = "stefan-banned-buzzword"     # pack-stable id
name = "Banned: synergy"                # one-line label
severity = "P0"                         # P0 / P1 / P2
language = "en"                         # "" = mechanical (always-on), "en"/etc.
pattern = "(?i)\\bsynergy\\b"           # regex string
description = "Banned per BOOK-PROTOCOL."
```

Full schema: [`docs/rules.md`](docs/rules.md).

## Documentation

Full docs live under [`docs/`](docs/):

- [Concepts](docs/concepts.md) — the engine, the rule pack, the
  runs database
- [Install](docs/install.md) — extras table + first-run + data
  locations
- [CLI reference](docs/cli.md) — every subcommand, every flag, every
  exit code
- [Rules](docs/rules.md) — TOML schema, default pack, custom packs,
  per-rule timeout, optional ReDoS static check
- [Privacy](docs/privacy.md) — explicit no-telemetry, no-LLM posture
- [Concurrency](docs/concurrency.md) — single-writer SQLite +
  WriteLock / MigrateLock primitives
- [Failure modes](docs/failure-modes.md) — ReDoS, malformed regex,
  unicode edge cases, large files, langdetect outages
- [Migrations](docs/migrations.md) — schema versioning, how to add
  a migration, what runs.db looks like
- [External contracts](docs/contracts.md) — MCP schemas, error codes,
  cross-plugin manifest, bridge to aurochs-voice
- [Comparison](docs/comparison.md) — where this sits relative to
  Originality.ai / GPTZero / Copyleaks (different category)
- [Empirical receipts](docs/empirical-receipts.md) — build receipts,
  the BLOCKERs that were resolved
- [FAQ](docs/faq.md) — common questions

## Status

v0.1 alpha. 253 tests passing. Twelve rules in the bundled default
pack. Four MCP tools. Pre-commit hook installer. Fully documented
contract surface in [`docs/contracts.md`](docs/contracts.md).

The package has been in continuous daily use as the author's
pre-publish scrubber for months before being factored out into a
public release. That's the prior on "alpha but production-ready in
the author's hands."

## License

MIT. See [LICENSE](LICENSE).
