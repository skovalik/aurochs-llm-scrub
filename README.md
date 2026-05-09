# aurochs-llm-scrub

**Detect and scrub LLM writing patterns from your prose.**

I kept noticing the same handful of LLM tells in my own writing — the unprompted "It's worth noting that...", the symmetric "not just X, but Y" cadence, the conclusion that summarized the conclusion. I'd written enough by hand to know which ones were mine and which were drift. So I built a regex-and-rules pipeline that catches the drift before it ships.

*MIT · v0.1 · public from commit 1 · Stefan Kovalik <stefan@aurochs.agency>*

---

`llm-scrub` is three things:

- **A rule pack** — pattern definitions for known LLM tells (overhedging, sycophantic openers, hollow conclusions, em-dash overuse, etc.). Customizable.
- **A scrubber** — runs the rules against your text; emits a structured report; optionally rewrites in-place with `.bak` rotation.
- **A pre-commit hook** — catches LLM drift before commit, with `# scrub: ignore` pragma for legitimate exceptions.

No LLM-as-judge in v0.1 — it's pure regex with ReDoS protection. The judgment is in the rule pack. Custom rules live in `~/.aurochs-llm-scrub/custom-rules.toml`.

**Privacy:** aurochs-llm-scrub does not transmit your data anywhere. There is no telemetry, no analytics, no crash reporting, no cloud calls. The runs.db file lives on your disk and you can read it with the standard SQLite CLI.

## 30-second quickstart

```bash
pip install aurochs-llm-scrub
llm-scrub init                          # creates runs.db, default rule pack
llm-scrub scan path/to/draft.md         # report findings, no modification
llm-scrub scrub --in-place draft.md     # apply fixes; backs up to draft.md.bak
llm-scrub install-precommit             # add to .git/hooks/pre-commit
```

## What this isn't

- Not a grammar checker
- Not a paraphraser; it removes patterns, doesn't rewrite voice
- Not LLM-judge in v0.1 — pure pattern detection (faster, deterministic, auditable)
- Not opinionated about whose voice you want; the rule pack is configurable
- Not cloud-dependent

## Use as a Claude Code plugin

```bash
/plugin install aurochs-llm-scrub
```

Then:
- `/aurochs-llm-scrub <args>` — full CLI surface
- `/scrub <file>` — quick scan (shortcut)

The plugin auto-wires the llm-scrub MCP server.

## Status

v0.1 alpha.

## License

MIT — see [LICENSE](LICENSE).
