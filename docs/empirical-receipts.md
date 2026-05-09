# Empirical receipts

This page is the audit trail behind the claims aurochs-llm-scrub
makes about itself. The framing is deliberate: in a category that
ships glossy benchmark numbers without the methodology, the only
durable trust signal is **a verifiable record of how the thing got
built and how it actually behaves**.

## Build receipts (T0 / v0.1.0)

T0 is the personal-MVP scope. By the time the public README went up,
the build had:

- **253 unit + integration tests passing** on the daily-use config
  (Python 3.13).
- **12 rules in the bundled default pack** — four mechanical (em
  dashes, `&mdash;` entity, double space, trailing whitespace) and
  eight English-coded P1 signposting rules. See
  [rules.md](rules.md) for the full table.
- **One schema migration** (`0001_initial.sql`) covering seven
  tables: `rule_packs`, `rules`, `scrub_runs`, `findings`,
  `rule_metrics`, `disabled_rules_log`, `schema_version`.
- **Four MCP tools**: `scrub_scan_text`, `scrub_scrub_text`,
  `scrub_list_rules`, `scrub_run_status`. Schemas locked in
  [contracts.md](contracts.md) under `SCHEMA_VERSION = "0.1.0"`.
- **Two slash commands** for the Claude Code plugin:
  `/aurochs-llm-scrub` (full CLI surface) and `/scrub <file>`
  (quick scan).

The numbers above aren't a brag. They're the floor: the package
actually has the surface area documented and the tests pass on it.

## What this v0.1 ships and what it doesn't

| Surface                                 | v0.1                                         |
| --------------------------------------- | -------------------------------------------- |
| CLI subcommands                         | 8 (`init`, `scan`, `scrub`, `install-precommit`, `rules`, `runs`, `verify`, `status`) |
| MCP tools                               | 4 (scan, scrub, list-rules, run-status)      |
| Default rules                           | 12 across P0 (4) and P1 (8)                  |
| Auto-fixable rules (locked table)       | 4 (`p0-em-dash`, `p0-mdash-entity`, `p0-double-space`, `p0-trailing-whitespace`) |
| Languages with rule coverage            | 1 (`en`) plus mechanical (always-on)         |
| ReDoS defense                           | Wall-clock timeout (always-on) + optional `[redos]` static check |
| LLM-as-judge                            | NOT shipped in v0.1; planned for v0.2        |
| Custom rule packs                       | Schema documented + loader wired; `rules register` CLI lands in v0.2 |
| Bench / benchmark                       | NOT shipped in v0.1; the rule pack is the spec, not a metric |
| Multilingual support                    | Mechanical rules run on any language; English-coded rules gated by langdetect |
| Pre-commit hook                         | Soft-mode (block on redacted secrets) + hard-mode (block on any P0) |

## BLOCKERs from plan v4 that were resolved

The build went through plan v4 with twenty-plus BLOCKER items
identified and folded. The ones load-bearing for the v0.1 surface:

| BLOCKER | What                                                                       | Resolution                                                                  |
| ------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| #1      | Cross-chunk dedup at chunk boundaries                                      | `(file_offset, length, rule_id)` triple keyed in finalize pass.             |
| #2      | Language-detect global + per-chunk fallback                                | `langdetect.detect_langs` global; per-chunk if confidence < 0.70.           |
| #3      | `verify --deep` integrity walk                                             | `core.recovery.VerifyReport` shipped; CLI wires to basic checks in v0.1, deep walk wires fully in v0.2. |
| #4      | Per-rule wall-clock timeout (the load-bearing ReDoS defense)               | 2.0s default per chunk; thread-based join-with-timeout; cross-platform.     |
| #5      | Pre-commit staged + unstaged conflict refusal                              | `_conflict_paths` check before scrub; exit code 2 with stash hint.          |
| #6      | File-relative finding offsets (not chunk-relative)                         | `chunk.start + len(chunk_text[:start_char].encode("utf-8"))` normalization. |
| #7      | Null-byte rejection in custom rule patterns                                | `RulePackLoadError` at TOML load time.                                      |
| #8      | Pragma scope (line + block + HTML-comment forms)                           | `parse_pragma_ranges` in `core/redact.py`; locked syntax.                   |
| #9      | `.bak` rotation policy                                                     | `--backup-mode rotate` default; `fail` and `overwrite` opt-in.              |
| #10     | `.scrubbed` collision under parallel runs                                  | Per-process `.scrubbed.<pid>.<ts>` temp + lock-protected rename.            |
| #13     | MigrationRunner — recall pattern inherited                                 | `MigrateLock` + `BEGIN EXCLUSIVE` + sequential enforcement + partial-state detection. |

The cross-cutting fixes (numbered separately in plan v4):

- **#14** — canonical schema versioning enforced via `manifest.json`.
- **#16** — every MCP tool gets a strict JSON Schema, validated by the
  lowlevel `Server.call_tool(validate_input=True)` decorator before
  dispatch.
- **#19** — `platformdirs` cross-OS data-dir resolution.

K-locks from the same plan:

- **K1** — `disabled_rules_log` is convenience cache, not a security
  boundary. Per-rule disable persists for custom rules; defaults
  auto-re-enable. Documented in [rules.md](rules.md).
- **K2** — silent-failure gap closed: `auto_disabled` count surfaces
  in every report footer and in `scrub_runs.status='partial'`.
- **K3** — pre-commit mode treats secret-scan redactions as blockers;
  `LLM_SCRUB_PRE_COMMIT=1` is the env signal.
- **K6** — runtime timeout is the load-bearing ReDoS defense; static
  check is best-effort.
- **K7** — `redaction_count` on `scrub_runs` so the report footer
  surfaces secret-scan activity.

## What the bench would look like (deferred)

aurochs-recall ships a benchmark (LongMemEval reproduction + personal
corpus template). aurochs-llm-scrub doesn't, in v0.1, because the
problem shape is different:

- **Recall**'s claim is "this retrieves better than baseline X." That's
  a measurable claim with a known eval methodology.
- **Scrub**'s claim is "these rules catch the patterns I want
  caught." That's a claim about the **rule pack**, not about the
  engine. The engine either runs the rules correctly or it doesn't —
  and the test suite covers that.

A future bench could measure precision/recall of the default pack
against a labeled corpus of LLM-written vs. human-written prose. That
would tell you whether the rule pack catches LLM tells reliably and
doesn't false-positive on human writing. Building that corpus
honestly (not just GPT-4 outputs labeled "LLM") is the hard part;
it's on the v0.2+ roadmap.

What you can do **today** to verify the rules work for your writing:

1. `pip install aurochs-llm-scrub`
2. `llm-scrub init`
3. `llm-scrub scan path/to/your-draft.md`
4. Read the findings; decide whether each pattern is actually a tell
   in your voice or a false positive.
5. For false positives: edit the rule (custom pack) or disable it
   via `llm-scrub rules disable <rule-id>`.

That's the personal-MVP loop. It's how the default pack got curated
in the first place — months of running scrub on Stefan's own drafts,
keeping rules that helped, dropping rules that didn't.

## T0 daily-use receipts

What "daily use" looks like for the author:

- **Pre-publish scan.** Every blog post, chapter, audit report, or
  pitch artifact runs through `llm-scrub scan` before deploy. The
  pre-commit hook catches drafts that snuck in.
- **Ad-hoc scrub.** Cold emails, DM drafts, README rewrites get a
  quick `llm-scrub scrub --in-place` to remove em dashes and
  whitespace artifacts.
- **Custom-pack iteration.** When a new LLM tell shows up
  consistently in drafts (e.g. `that's the real X` started appearing
  in 4.7-era drafts), the pattern lands in `custom-rules.toml` and
  the default pack gets a candidate for the next minor bump.
- **Voice-overlay integration.** `aurochs-voice` calls llm-scrub's
  engine for every voice-mode draft and applies its overlay rules
  on top of the universal report.

The package didn't get factored into a public release until the
daily-use shape was stable enough to be worth sharing. That's the
prior on "alpha but production-ready in the author's hands."

## Verification by runtime, not by reading

The general discipline (inherited from `aurochs-recall`): reading the
code tells you what it *should* do; running it tells you what it
*actually* does. Every BLOCKER above has a corresponding test in
`tests/`. Test count is the floor: 253 at v0.1.0. Add a regression
test for any bug you find before fixing it; the docs in this repo
should reflect what the tests verify, not what the author hopes is
true.
