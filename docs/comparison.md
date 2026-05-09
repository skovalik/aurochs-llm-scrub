# Comparison

aurochs-llm-scrub sits in a different category from the AI-detection
tools you've probably heard of. Worth being explicit about what it
isn't, then about where it actually fits.

## Not in this category: AI-detection tools

**Originality.ai**, **GPTZero**, and **Copyleaks** are in the
AI-detection market. They take a piece of text and answer "did an
LLM write this?" — usually with a percentage score, sometimes with
a paragraph-by-paragraph breakdown. Their customer is a teacher
catching a student, an editor vetting a freelancer's submission, a
publisher screening for AI-written articles.

That's the **detection** category. aurochs-llm-scrub is not a
detection tool. It will not give you a score for someone else's
writing. It will not classify a paragraph as "human" or "AI." That
isn't what it's built to do.

## In this category: pre-publish scrubbers for the writer

aurochs-llm-scrub is for **the writer** removing **their own** LLM
artifacts before publishing. The user is someone who:

- Drafts with AI assistance (ChatGPT, Claude, Cursor, copilot, etc.).
- Notices the same handful of LLM tells creeping into their drafts —
  the unprompted "it's worth noting that...", the symmetric "not
  just X, but Y" cadence, the conclusion that summarizes the
  conclusion, the em-dash overuse.
- Wants to catch those tells before the draft ships, ideally with
  enough automation that they don't have to remember to look for
  them.

The category here is closer to **linters** than to detectors:

- ESLint catches your JS code smells before commit.
- pre-commit hooks catch trailing whitespace.
- aurochs-llm-scrub catches LLM tells in prose.

The judgment is in the rule pack, not in a model. The rule pack is
configurable. You can author your own pack with your own banned
phrasings (`stefan-pixel-perfect`, `corporate-jargon-of-yours`) and
the scrubber will catch them with the same machinery.

## Differences from grammar checkers

**Grammarly** and friends are grammar / style suggestions tools.
They flag passive voice, sentence-length, comma splices. They will
not flag "in conclusion" as an LLM tell, because "in conclusion"
isn't grammatically wrong. It's just *one* of the tells.

aurochs-llm-scrub doesn't try to be a grammar checker. It does one
thing: pattern-match for a specific class of LLM-output artifacts.
You can run it alongside Grammarly without conflict.

## Differences from AI paraphrasers

**Rephrase tools** take an AI-written paragraph and rewrite it to
"sound human." They paraphrase. aurochs-llm-scrub does not paraphrase
— it removes patterns. The voice is yours; the scrubber removes the
patterns that make it not sound like yours.

This is a load-bearing distinction. A paraphraser will rewrite
"This is critically important" to "This matters a lot," which may or
may not be what you want. The scrubber will flag both phrasings (if
you've added them to your custom pack) and let you decide what to
write instead.

## Why pure pattern detection in v0.1

The obvious objection: an LLM-as-judge could catch nuance the
regex can't. True. The reasons v0.1 is pure pattern detection
anyway:

- **Speed.** A 10 KB draft scans in milliseconds. An LLM call adds
  latency that breaks the pre-commit hook story.
- **Determinism.** The same input produces the same finding hashes
  every time. You can cite a finding, link to a rule, audit what
  triggered. With an LLM-as-judge you're chasing model-version
  drift.
- **Auditability.** Every flagged pattern points at a rule with a
  one-paragraph rationale (`description`). You can disagree with
  a rule, edit it, or disable it. With an LLM-as-judge you're
  arguing with a black box.
- **No cloud dependency.** v0.1 makes zero outbound network calls.
  Privacy isn't a feature you have to opt out of.

LLM-as-judge is on the v0.2 roadmap as an opt-in extra
(`[llm-judge]`). The defaults will stay regex-first; the LLM is
the second pass for cases the regex can't catch.

## Where this lands relative to `aurochs-voice`

`aurochs-voice` is the bigger sibling — a voice prosthesis that
applies BOOK-PROTOCOL rules, recipient-aware register selection,
and capture-delta routing. When both packages are installed, voice
imports llm-scrub's `ScrubReport` shape and adds an overlay layer:
promotes `we`-pronoun from P1 to P0, adds Stefan-banned-FOR-HIM
rules, adds register-mismatch rules. llm-scrub stays the
**universal** layer; voice is the **personalized** layer.

The cross-plugin contract is in [contracts.md](contracts.md). If
you're a writer who wants the universal LLM-scrubber and nothing
else, install llm-scrub on its own. If you want a fully personalized
voice prosthesis, install both. The dependency only goes one way:
voice depends on scrub, never the other way around.
