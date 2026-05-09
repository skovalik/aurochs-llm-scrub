# FAQ

## Does this call an LLM?

No. v0.1 is pure regex + langdetect. There are zero outbound network
calls — `core/` and `cli/` make none, and the `[mcp]` server is a
local stdio JSON-RPC endpoint, not an HTTP service. LLM-as-judge is
on the v0.2 roadmap as an opt-in extra; the defaults will stay
regex-first.

## Does it work for non-English prose?

Mechanical rules (em dashes, `&mdash;`, double spaces, trailing
whitespace) work for any language — they're punctuation patterns
that don't depend on the language gate.

English-coded rules (`language="en"`) are gated by `langdetect`. On
chunks the gate identifies as English, the rules run. On chunks
identified as a different language, they're skipped. On chunks where
the gate fails or returns low confidence, the rules still run — the
design decision is "better to over-flag than to miss with no
explanation."

The default pack ships English-coded rules only. To add coverage for
another language, author a custom pack with `language = "de"` (or
whatever) on the rules. See [rules.md](rules.md).

## Can I add my own patterns?

Yes. `llm-scrub init` scaffolds a `custom-rules.toml` next to your
`runs.db`. Add `[[rule]]` entries with the same schema as the default
pack. The TOML format is documented in [rules.md](rules.md). A
minimal custom rule:

```toml
[[rule]]
rule_id = "stefan-banned-buzzword"
name = "Banned: synergy"
severity = "P0"
language = "en"
pattern = "(?i)\\bsynergy\\b"
description = "Banned per BOOK-PROTOCOL."
```

## How do I integrate with pre-commit?

```bash
llm-scrub install-precommit             # soft mode (default)
llm-scrub install-precommit --mode hard # also block on any P0
```

The hook script invokes `llm-scrub scan` against each staged file. In
soft mode, the commit proceeds unless a secret-scan redaction fires
(plan v4 K3). In hard mode, any P0 finding blocks the commit too.

The hook is portable across POSIX shells; on Windows, git for Windows
runs `.git/hooks/pre-commit` via the bundled MSYS bash. If you need
to integrate with the third-party `pre-commit` framework
(<https://pre-commit.com>), wrap `llm-scrub scan $FILES` in a hook
config — there's no native config file in v0.1 because the bundled
hook installer is the canonical path.

## What's the difference between this and Originality.ai / GPTZero / Copyleaks?

Different category. Those are AI-detection tools — they take someone
else's text and answer "did an LLM write this?" Their customer is a
teacher, an editor, a publisher. aurochs-llm-scrub is a pre-publish
scrubber for the writer who wants to remove their own LLM artifacts
before shipping. See [comparison.md](comparison.md) for the full
breakdown.

## Why is the default pack so small?

Twelve rules. The pack is hand-curated by the author against months of
his own LLM-assisted drafts, keeping only rules that earned their
place: catch real tells, low false-positive rate, easy to explain.

If you want broader coverage, the custom-pack TOML schema is the
intended extension point. If a rule from your own pack is a clear
universal tell (not Stefan-specific), file an issue with examples and
it's a candidate for the next minor bump of the default pack.

## What happens when a rule's regex catastrophically backtracks?

The per-rule wall-clock timeout (2.0s default per chunk) catches it.
The rule is auto-disabled for the rest of the run, an audit row goes
into `disabled_rules_log`, and a warning surfaces in the report
footer. Default rules auto-re-enable on the next CLI invocation;
custom rules stay disabled until you manually re-enable them. See
[failure-modes.md](failure-modes.md).

The optional `[redos]` extra adds a static pre-check at rule-load
time. It's best-effort; the runtime timeout is the load-bearing
defense.

## Is `runs.db` portable across machines?

Yes. It's a standard SQLite file with WAL pragmas. Copy it; open it
with `sqlite3` on the other machine; query it directly. The schema
is documented in [migrations.md](migrations.md). On a fresh install,
copying the file in place is enough — `llm-scrub status` will pick
it up.

## Does this work alongside Grammarly / linters?

Yes. Grammarly is a grammar / style suggestions tool; it doesn't flag
LLM tells. Linters like ESLint check code, not prose. There's no
overlap with either.

## What if I want LLM-as-judge for the cases regex can't catch?

That's on the v0.2 roadmap. The plan: an opt-in extra (`[llm-judge]`)
that pipes findings (or unflagged passages) through a BYOK LLM for
nuance the regex can't catch. Defaults will stay regex-first; the LLM
is the second pass.

If you want this sooner, run `llm-scrub scan --json file.md` and
pipe the output through your own LLM judge — the JSON shape is locked
in [contracts.md](contracts.md).

## Can I use this in CI?

Yes. The CLI exit codes are locked:

```bash
llm-scrub scan --hard-fail my-draft.md
# Exits 5 if any P0 finding is emitted; otherwise 0.
```

Drop that into your CI step. Or use the pre-commit hook with `--mode
hard`.

## What about my Claude Code workflow?

```bash
/plugin install aurochs-llm-scrub
```

Then in any Claude Code session:

- `/aurochs-llm-scrub <args>` — full CLI surface (any subcommand).
- `/scrub <file>` — quick scan shortcut.

The plugin auto-wires the llm-scrub MCP server, so Claude can also
invoke the four scrub tools natively (`scrub_scan_text`,
`scrub_scrub_text`, `scrub_list_rules`, `scrub_run_status`). See
[contracts.md](contracts.md) for the tool schemas.

## Where do I report a bug?

GitHub issues at
<https://github.com/skovalik/aurochs-llm-scrub/issues>.

If it's a security-sensitive bug (ReDoS pattern, FK violation,
something that lets a malicious rule pack tank the engine), email
<stefan@aurochs.agency> directly with `aurochs-llm-scrub security`
in the subject.
