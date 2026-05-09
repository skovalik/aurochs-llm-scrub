# aurochs-llm-scrub

**Detect and scrub LLM writing patterns from your prose.**

Regex-and-rules pipeline. Catches the AI tells before they ship. Local
SQLite. No LLM-as-judge in v0.1 — pure pattern detection, deterministic
and auditable.

*MIT · v0.1 · public from commit 1 · Stefan Kovalik <stefan@aurochs.agency>*

---

## What this is

A three-piece tool: a rule pack (pattern definitions for known LLM
tells), a scrubber (runs the rules and optionally rewrites in place),
and a pre-commit hook (catches drift before commit). Findings are
written to a local `runs.db` file; nothing leaves your disk.

- **[Concepts](concepts.md)** — what a rule is, what a finding is,
  what `runs.db` stores, how the engine fits together.
- **[Install](install.md)** — `pip install aurochs-llm-scrub` and the
  optional extras.
- **[CLI](cli.md)** — every subcommand, every flag, every exit code.
- **[Rules](rules.md)** — the rule schema, custom rule packs, the
  per-rule wall-clock timeout, the optional ReDoS static check.
- **[Privacy](privacy.md)** — explicit no-telemetry posture; no LLM
  calls in v0.1.
- **[Concurrency](concurrency.md)** — single-writer SQLite + the
  `WriteLock` / `MigrateLock` primitives from `aurochs-core`.
- **[Failure modes](failure-modes.md)** — ReDoS, malformed regex,
  unicode edge cases, large files, langdetect outages.
- **[Migrations](migrations.md)** — schema versioning via
  `MigrationRunner`; how to add a new migration.
- **[External contracts](contracts.md)** — MCP tool schemas, error
  codes, cross-plugin manifest, bridge to `aurochs-voice`.
- **[Comparison](comparison.md)** — where this sits relative to
  Originality.ai / GPTZero / Copyleaks. Different category.
- **[Empirical receipts](empirical-receipts.md)** — build receipts,
  the BLOCKERs that were resolved, what's in v0.1 and what isn't.
- **[FAQ](faq.md)** — common questions.

## What this isn't

- Not a grammar checker.
- Not a paraphraser; it removes patterns, doesn't rewrite voice.
- Not LLM-as-judge in v0.1 — pure pattern detection (faster,
  deterministic, auditable).
- Not opinionated about whose voice you want; the rule pack is
  configurable.
- Not cloud-dependent. The `runs.db` file lives on your disk and you
  can read it with the standard SQLite CLI.
- Not an AI-detection tool for catching other people's text.
  Originality.ai and friends serve that market. This one is for the
  writer scrubbing their own LLM artifacts before publishing.

## 30-second quickstart

```bash
pip install aurochs-llm-scrub
llm-scrub init                          # creates runs.db, default rule pack
llm-scrub scan path/to/draft.md         # report findings, no modification
llm-scrub scrub --in-place draft.md     # apply fixes; backs up to draft.md.bak
llm-scrub install-precommit             # add to .git/hooks/pre-commit
```

Continue with [Install](install.md) for the full setup, or
[Concepts](concepts.md) if you want the architecture overview first.
