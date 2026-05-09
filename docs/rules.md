# Rules

A rule is a regex paired with a severity, a language tag, and a
description. A rule pack is a collection of rules with a `(name,
version)` identity. The default pack ships nine hand-picked rules
covering the LLM tells Stefan was scrubbing by hand from his own
drafts. You can author your own pack and drop it in.

## TOML schema (v0.1.0)

```toml
schema_version = "0.1.0"          # optional; defaults to "0.1.0"
name = "stefan-custom"            # required; pack identifier
version = "0.1.0"                 # required; semver-style

[[rule]]
rule_id = "stefan-pixel-perfect"  # required; pack-stable id
name = "Pixel-perfect"            # required; one-line label
severity = "P0"                   # required; P0 / P1 / P2
language = ""                     # optional; "" = mechanical (always-on), "en" / etc.
pattern = "(?i)\\bpixel-perfect\\b"  # required; regex string
description = "Banned per BOOK-PROTOCOL"  # required; one-line rationale
```

Field-by-field:

- **`schema_version`** — pack TOML schema version. Locked at `"0.1.0"`
  for v0.1; future bumps require `llm-scrub rules migrate`. Missing
  field defaults to `"0.1.0"`.
- **`name`** — pack identity. Forms part of the UNIQUE
  `(name, version)` constraint in the `rule_packs` table. Use lowercase
  + hyphens (e.g. `stefan-custom`, `book-protocol`).
- **`version`** — semver-style pack version. Bumping this when you add
  or change rules creates a new historical row; old findings continue
  to reference the prior version through their FK.
- **`rule_id`** — pack-stable rule identifier. UNIQUE within the pack.
  Convention: `<severity>-<short-tag>` (e.g. `p0-em-dash`,
  `p1-corporate-filler`). The auto-fix table keys on this string, not
  on the SQLite PK.
- **`name`** — human-readable label.
- **`severity`** — `P0` / `P1` / `P2`. The pack-author's intent. The
  workspace `[severity_overrides]` config can promote or demote at
  runtime without mutating the row.
- **`language`** — `""` for mechanical (always-on regardless of the
  language gate), `"en"` for English-coded (skipped when the chunk's
  detected language differs). Future packs may use `"de"`, `"es"`,
  etc. The TOML loader treats empty-string as the always-on sentinel;
  the dataclass uses `None` for the same concept.
- **`pattern`** — the regex string. Stored verbatim; the engine
  compiles it via `re.compile(rule.pattern)` and runs it under a
  per-chunk wall-clock timeout.
- **`description`** — one-paragraph rationale. Surfaced in
  `llm-scrub rules show <rule-id>`.

## The default pack

Twelve rules, locked at v0.1.0:

| Rule id                    | Severity | Language | What it catches                                   |
| -------------------------- | -------- | -------- | ------------------------------------------------- |
| `p0-em-dash`               | P0       | -        | Em dash `—`. Auto-fix `, `.                       |
| `p0-mdash-entity`          | P0       | -        | `&mdash;` HTML entity. Auto-fix `, `.             |
| `p0-double-space`          | P0       | -        | Two or more consecutive spaces. Auto-fix ` `.     |
| `p0-trailing-whitespace`   | P0       | -        | Whitespace at end of line. Auto-fix `` (strip).   |
| `p1-sycophantic-opener`    | P1       | en       | `Great question`, `excellent question`, etc.      |
| `p1-its-worth-noting`      | P1       | en       | `it's worth noting / mentioning / pointing out`.  |
| `p1-not-just-but`          | P1       | en       | `not just X but Y` cadence.                       |
| `p1-hollow-conclusion`     | P1       | en       | `in conclusion`, `to sum up`, `in summary`.       |
| `p1-let-me-be-clear`       | P1       | en       | `let me be clear`, `to be clear`, `put simply`.   |
| `p1-thats-the`             | P1       | en       | `that's the real / whole / entire / true / key`.  |
| `p1-vague-quantifier`      | P1       | en       | `various`, `myriad`, `numerous`, `several`.       |
| `p1-corporate-filler`      | P1       | en       | `robust`, `seamless`, `cutting-edge`, etc.        |

Source: `aurochs_llm_scrub/core/default_rules.toml`.

## Writing a custom rule pack

`llm-scrub init` scaffolds an empty `custom-rules.toml` next to your
`runs.db`. Edit it, then re-register the pack via `llm-scrub rules
list` to verify it loads. (A future `rules register` command lands in
v0.2; v0.1 wires registration through the engine on first scan.)

A minimal custom pack:

```toml
schema_version = "0.1.0"
name = "stefan-custom"
version = "0.1.0"

[[rule]]
rule_id = "stefan-banned-buzzword"
name = "Banned buzzword: synergy"
severity = "P0"
language = "en"
pattern = "(?i)\\bsynergy\\b"
description = "Banned per BOOK-PROTOCOL. Replace with a concrete claim."
```

Validation rules (enforced by the loader, raises `RulePackLoadError`):

- Duplicate `rule_id` within a pack — rejected.
- Empty `name`, `version`, `rule_id`, `pattern`, or `description` —
  rejected.
- Severity outside `{"P0", "P1", "P2"}` — rejected.
- Pattern containing a raw `\x00` byte — rejected (BLOCKER #7).
- Malformed regex — surfaces as `re.error` at compile time; the rule
  is auto-disabled with reason `compile-error` and an entry is
  appended to `disabled_rules_log`.

## Per-rule wall-clock timeout

Every rule application has a **2.0-second wall-clock budget per
chunk** (BLOCKER #4). When a rule trips the budget:

1. The engine raises `_RuleTimeoutError` internally.
2. The rule is flipped to `enabled=0` in memory for the rest of the
   run.
3. A row is appended to `disabled_rules_log` with reason `timeout` and
   a 256-byte truncated snippet of the chunk text as
   `redos_evidence`.
4. The corresponding row in the `rules` table is also flipped to
   `enabled=0` so the next scan respects the disable without
   re-loading the audit log.

Custom rules **stay disabled across runs** until manually re-enabled
via `llm-scrub rules enable <rule-id>`. Default rules **auto-re-enable
on next CLI invocation** (defaults are trusted code; suppression masks
engine bugs — K2 carry-forward).

The chunk window is 64 KB with 4 KB overlap. The timeout is per chunk,
not per scan, so a slow rule on one chunk doesn't kill the whole run
— it just gets disabled for subsequent chunks.

## Optional ReDoS static check

The `[redos]` extra installs `redos-detector` for a static pre-check
at rule-load time. When installed, every rule's pattern is run through
`redos_detector.is_redos(rule.pattern)` before the engine starts
scanning. Patterns flagged as ReDoS-prone are auto-disabled with reason
`redos-static`.

```bash
pip install "aurochs-llm-scrub[redos]"
```

The static check is **best-effort**. The engine's load-bearing defense
is the runtime wall-clock timeout — `_redos_check` swallows
`ImportError` and returns `None` when the dependency is missing, so
the engine runs fine without it. Plan v4 K6 frames the static check
as a second line of defense, not a precondition.

When both are active:

- Static check fires → rule disabled at load time → never executes.
- Timeout fires → rule disabled at runtime → subsequent chunks skip it.

A rule that the static check approves but trips the runtime timeout
in production is the expected ReDoS catch — both layers exist because
neither is sufficient on its own.

## Severity overrides

Workspace-level promotion or demotion lives in `<data_dir>/config.toml`:

```toml
[severity_overrides]
# pronoun-we = "P0"            # voice-equivalent strict mode
p1-corporate-filler = "P0"
```

Overrides resolve at runtime; the `rules.severity_default` row is
never mutated. The override map is read once per scan; changes take
effect on the next invocation.

## Inline pragmas

Per-finding exemptions live in the source file:

```markdown
This sentence contains an em dash — but it's intentional. <!-- scrub: ignore -->

# scrub: ignore-block-start
Everything in this block is exempt from every rule, including
em-dash detection — and the corporate filler `robust` and `seamless`.
# scrub: ignore-block-end
```

Pragmas are resolved per-line; findings whose `line_number` lies inside
a pragma range are reported but never auto-fixed. Block-scope is
inclusive on both ends. An unclosed `ignore-block-start` consumes to
EOF.

## Workspace exemptions

`<data_dir>/exemptions.toml` holds workspace-wide rule exemptions:

```toml
schema_version = "0.1.0"

[[exempt]]
rule = "p0-em-dash"
reason = "Editorial style: em dashes are intentional"
```

Cascade order: workspace (`exemptions.toml`) → directory
(`.scrub-exempt`) → sidecar (`<file>.scrub-exempt`). Sidecar exemptions
are not yet wired in v0.1; the `exemptions.toml` is honored.

## Rule pack drift detection

Each registered pack carries a SHA-256 of the file bytes
(`rule_packs.sha256`). Re-registering the same `(name, version)` pair
is a no-op via `INSERT OR IGNORE`. Bumping the `version` and
re-registering creates a new historical row, leaving prior findings
tied to the old version through their FK.

If you edit a pack file without bumping the version, the new content
hash will not match the registered hash — but the registered rules
take precedence (the engine reads from `runs.db`, not the file). To
pick up changes: bump `version`, re-run any command that registers
packs (`llm-scrub init`, `llm-scrub scan`).

The snapshot-at-init invariant (5-agent QA fold lock) means rule-pack
edits don't affect in-flight runs. Once a scan starts, the rule set
is frozen for that run.
