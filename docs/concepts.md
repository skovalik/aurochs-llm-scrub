# Concepts

aurochs-llm-scrub is structured around three pieces: a **rule pack**
(what to look for), a **scrubber** (the engine that runs the rules),
and a **runs database** (the audit trail of what was found and what
was disabled along the way).

## Architecture

| Piece          | Where it lives                              | Role                                         |
| -------------- | ------------------------------------------- | -------------------------------------------- |
| Rule pack      | `core/default_rules.toml` + custom TOML     | Pattern definitions; the only knob users tune |
| Rule registry  | `rule_packs` + `rules` tables in `runs.db`  | Persisted snapshot of loaded rules           |
| Scrubber       | `core/scrubber.py`                          | Compiles rules, scans chunks, dedups findings, writes the run |
| Redactor       | `core/redact.py`                            | Splices mechanical fixes back into the text  |
| Runs database  | `runs.db` (SQLite WAL)                      | One row per scan, one row per finding, audit trail of disables |
| Pre-commit hook| `.git/hooks/pre-commit` (installed)         | Wraps `llm-scrub scan` over staged files     |

The rule pack is the only part most users touch. The scrubber, the
redactor, and the runs DB are mechanism — they exist so the rules can
do their job without surprising you.

## What an LLM-tell is

An LLM-tell is a writing pattern that signals "this came out of a
language model, not a human." The default pack ships twelve of them.
They cluster into two kinds:

- **Mechanical** — language-independent, punctuation-shaped tells.
  Em dashes (`—`), the `&mdash;` HTML entity, double spaces, trailing
  whitespace. These run regardless of the language gate; they are
  always wrong in clean prose.
- **English-coded** — phrasings that signal LLM drift in English text.
  Sycophantic openers (`Great question`), hollow hedges (`it's worth
  noting`), the `not just X but Y` cadence, hollow conclusions (`in
  conclusion`), `let me be clear`, `that's the X` summarization
  signature, vague quantifiers (`various`, `myriad`), corporate filler
  (`robust`, `seamless`).

The pack-author's intent is the **default severity** (`P0` / `P1` /
`P2`). Your workspace `config.toml` can promote or demote at runtime
via `[severity_overrides]` without mutating the rule rows.

## What a rule is

A rule is a row in the `rules` table with these fields:

- `rule_id` — pack-stable identifier like `p0-em-dash`. Used in
  pragma exemptions and in the auto-fix table.
- `name` — human label.
- `pattern` — the regex string. Compiled once per scan, applied per
  chunk under a wall-clock timeout.
- `severity_default` — `P0` / `P1` / `P2`. The pack-author's intent.
- `language` — `NULL` for mechanical (always-on), `"en"` for
  English-coded (gated by language detection).
- `description` — one-paragraph rationale.
- `enabled` — runtime flag. The auto-disable state machine flips
  this to 0 when a rule trips the timeout or fails to compile.

Rules are owned by a **rule pack**. A pack has a `(name, version)`
unique identity. The default pack ships with the package; custom packs
live in `~/.aurochs-llm-scrub/custom-rules.toml` (Linux/macOS) or the
equivalent `%LOCALAPPDATA%` path on Windows. Bumping a pack's version
creates a new historical row; old findings continue to reference the
historical version through their `rule_id` foreign key.

See [rules.md](rules.md) for the full TOML schema and how to write
custom rules.

## What a finding is

A finding is a single rule match against a scrubbed input. The row
carries:

- `run_id` — FK into `scrub_runs`.
- `rule_id` — FK into `rules`.
- `file_offset` — byte offset of the match (file-relative, not
  chunk-relative).
- `length` — length of the matched text in bytes.
- `file_path` — denormalized from the run's target so a finding
  carries enough context to render in isolation.
- `line_number` — 1-based; computed from `file_offset`.
- `severity` — `P0` / `P1` / `P2` after any `[severity_overrides]`
  resolution.
- `snippet` — the verbatim matched text. Subject to secret-scan
  redaction: high-entropy / known-key substrings are replaced with
  `[REDACTED:CLASS]` before the snippet leaves the engine.

Cross-chunk dedup uses the `(file_offset, length, rule_id)` triple.
Stable identity across re-runs uses a SHA-256 over the 4-tuple
`(snippet, rule_id, file_path, file_offset)` — re-running the scrubber
on the same input yields the same finding hashes.

## What `runs.db` stores

| Table                | Role                                                             |
| -------------------- | ---------------------------------------------------------------- |
| `rule_packs`         | One row per loaded pack. UNIQUE on `(name, version)`.            |
| `rules`              | One row per rule. UNIQUE on `(pack_id, rule_id)`.                |
| `scrub_runs`         | One row per `scan` / `scrub` invocation.                         |
| `findings`           | One row per rule match. FK to `scrub_runs` and `rules`.          |
| `rule_metrics`       | Per-`(rule_id, run_id)` aggregates. Drives `rules stats`.        |
| `disabled_rules_log` | Audit trail of auto-disables. Pack-namespaced.                   |
| `schema_version`     | Migration tracking with `applied` / `failed` / `in_progress`.    |

Full DDL lives in `aurochs_llm_scrub/core/migrations/0001_initial.sql`.
The schema is contract surface — see [contracts.md](contracts.md) and
[migrations.md](migrations.md).

## What `severity` means

`P0` / `P1` / `P2` is a closed three-tier vocabulary.

- **P0 — Hard violations.** Em dashes, double space, trailing
  whitespace, banned buzzwords. Mechanical P0s have an auto-fix in
  the locked v0.1 table; English-coded P0s do not.
- **P1 — LLM signposting.** Sycophantic openers, hollow conclusions,
  `let me be clear`, hedging, vague quantifiers, corporate filler.
  Reported, not auto-fixed.
- **P2 — Style suggestions.** Reserved in v0.1 — the default pack
  ships no P2 rules. Future packs (and `aurochs-voice`'s overlay
  layer) will use P2 for sentence-variance and significance-inflation
  patterns that need NLP-aware detection rather than pattern matching.

JSON output uses the codes. Markdown output uses both codes and
human-readable labels (`## Hard Violations (P0)`). Plain-text terminal
output uses labels only.

## What a pragma is

A pragma is an inline comment that exempts a line (or a block) from
finding emission. Two forms:

- `# scrub: ignore` — exempts the line it's on.
- `<!-- scrub: ignore -->` — same, for markdown drafts.
- `# scrub: ignore-block-start` and `# scrub: ignore-block-end` —
  open and close a block-scope exemption window. An unclosed
  `ignore-block-start` consumes to EOF.

Pragma resolution happens in the redactor before any auto-fix splice;
findings whose `line_number` lies inside a pragma range are reported
but never rewritten. The pragma syntax is locked per plan v4
BLOCKER #8.

## How a scan works

```
input text
   |
   v
[ language gate ]   detect_langs() — global; per-chunk fallback if confidence < 0.70
   |
   v
[ chunker ]         64 KB windows + 4 KB overlap; UTF-8-boundary-safe
   |
   v
[ rule loop ]       per chunk × per rule:
                      - skip if rule disabled
                      - skip if rule.language != chunk.language (when known)
                      - apply with 2.0s wall-clock timeout
                      - timeout → auto-disable + disabled_rules_log
   |
   v
[ dedup ]           collapse on (file_offset, length, rule_id)
   |
   v
[ persist ]         INSERT scrub_runs + INSERT findings + UPDATE rules
   |
   v
[ report ]          plain / json / markdown
```

The wall-clock timeout is the load-bearing ReDoS defense. The optional
`[redos]` extra adds a static pre-check that's best-effort; the
runtime timeout catches everything the static check misses. See
[failure-modes.md](failure-modes.md).

## Bridge to `aurochs-voice`

When both packages are installed, voice's scrub mode imports the
`ScrubReport` shape from llm-scrub and applies its own overlay layer
(promotes `we`-pronoun from P1 to P0, adds Stefan-banned-FOR-HIM
rules, adds register-mismatch rules). llm-scrub never depends on
voice; the cross-plugin contract lives in
[contracts.md](contracts.md).
