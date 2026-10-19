# Failure modes

What goes wrong, and what the system does about it.

## ReDoS / catastrophic backtracking

- **Symptom:** A rule's regex enters catastrophic backtracking on a
  pathological input. Without protection, the scan would hang.
- **Mitigation:** Per-rule wall-clock timeout (2.0s default per chunk;
  configurable via `<data_dir>/config.toml`). The regex runs in a
  worker thread; `thread.join(timeout=2.0)` enforces the budget. If
  the worker is still alive when the timeout fires:
  1. The rule is flipped to `enabled=0` in memory.
  2. A row is appended to `disabled_rules_log` with reason `timeout`
     and a 256-byte truncated snippet of the offending chunk text as
     `redos_evidence`.
  3. The corresponding row in the `rules` table is also flipped to
     `enabled=0`.
- **Detection:** Run `llm-scrub rules list --disabled` to surface
  rules currently disabled. Run `llm-scrub status` to see the count.
- **Recovery for default rules:** Auto-re-enabled on the next CLI
  invocation (defaults are trusted code; suppression masks engine
  bugs).
- **Recovery for custom rules:** Stay disabled until manually
  re-enabled via `llm-scrub rules enable <rule-id>`. Inspect the
  `disabled_rules_log` row to see the offending input; rewrite the
  pattern; re-enable.

The optional `[redos]` extra adds a static pre-check at rule-load
time. When installed, every rule's pattern is run through
`redos_detector.is_redos`; flagged patterns auto-disable with reason
`redos-static` before the engine starts. The static check is
**best-effort**; the runtime timeout catches everything it misses.

## Malformed regex (compile error)

- **Symptom:** A rule's pattern is invalid Python `re` syntax.
- **Mitigation:** `re.compile` raises `re.error` at engine load time.
  The rule is flipped to `enabled=0` and a `disabled_rules_log` row
  is appended with reason `compile-error` and the `re.error` message
  as `redos_evidence`.
- **Detection:** Same as above — `rules list --disabled`.
- **Recovery:** Edit the pattern in the source pack, bump the pack
  version, re-register.

## Null-byte in a pattern

- **Symptom:** A custom pack's TOML pattern contains `\x00`.
- **Mitigation:** The TOML loader rejects the pack at load time
  (BLOCKER #7) with a `RulePackLoadError` naming the offending rule.
- **Recovery:** Remove the null byte from the pattern. If you genuinely
  need to match `\x00` in your input, file an issue — v0.2 may add a
  controlled escape.

## Unicode / multi-byte char at chunk boundary

- **Symptom:** The chunker's 64 KB window cuts through a UTF-8
  multi-byte character, at the window's end or at its start.
- **Mitigation:** At the end, the chunker walks the tail of the byte
  slice backward up to 3 bytes until the slice decodes cleanly. At the
  start, a window whose start lands on a continuation byte starts at
  that character's lead byte instead, so the window never opens with
  U+FFFD and every finding keeps its true file offset. The next chunk's
  overlap (4 KB) re-scans the same region so a pattern spanning the
  boundary is captured by both chunks; the cross-chunk dedup pass
  collapses duplicates by `(file_offset, length, rule_id)`.

## Anchors and runs at a chunk boundary

- **Symptom:** A window's edges are not the text's edges. At a cut end,
  `$`, `\Z` and `\b` match as if the text ended there, and a greedy run
  stops there. At a later window's start, `^`, `\b` and lookbehinds see
  no text before it, and a run that crosses the start is matched again
  from its middle.
- **Mitigation:** The engine drops a match that ends exactly where a
  window was cut short, and a match that starts on a later window's
  first character. The neighboring window holds the same bytes with
  real context on both sides, so nothing shorter than the 4 KB overlap
  is lost. Keep a custom rule's matches and lookarounds well under 4 KB.

## Empty input

- **Symptom:** `llm-scrub scan` against an empty file.
- **Behavior:** A `scrub_runs` row is recorded with zero findings.
  Exit code 0. Plain output is the empty findings list plus a footer
  with `P0=0 P1=0 P2=0`.

## Very large input (>2 MB)

- **Symptom:** A draft larger than the chunker's natural window.
- **Behavior:** The chunker yields successive 64 KB windows with 4 KB
  overlap. Memory stays bounded; the per-rule timeout caps the worst
  case at `chunks × rules × 2.0s`. For multi-MB inputs use the CLI
  rather than the MCP layer (MCP tools cap at the 1 MB string limit
  per the JSON-RPC payload constraint; see [contracts.md](contracts.md)).

## langdetect unavailable / fails

- **Symptom:** `langdetect` import fails or detection raises on
  pathological input.
- **Mitigation:** The engine catches the exception and treats the
  language as unknown. English-coded rules (`language="en"`) still
  run on chunks with unknown language — the design decision is "better
  to over-flag than to miss with no explanation." Mechanical rules
  (`language=NULL`) are unaffected.

## Schema drift

- **Symptom:** The `manifest.json` records a `schema_version` newer
  than the installed plugin's `SCHEMA_VERSION` constant.
- **Mitigation:** The MCP server refuses to operate on a manifest
  schema_version newer than its compiled-in version. The CLI surfaces
  this as exit code 4 ("schema mismatch") with a `MigrationError`
  containing `schema_version` in the message.
- **Recovery:** Either upgrade the plugin (`pip install -U
  aurochs-llm-scrub`) or pin to the older major. See
  [migrations.md](migrations.md).

## Orphan migration state

- **Symptom:** A migration was interrupted mid-flight (SIGKILL, power
  loss). The `schema_version` table has a row with `status='in_progress'`.
- **Mitigation:** The next `MigrationRunner.run()` detects the orphan
  row and refuses to proceed with `MigrationError("Detected partial
  migration at version N. Manual recovery required.")`.
- **Recovery:** Inspect `schema_version`, decide whether to roll
  forward or drop the partial state, then re-run the migration.
  `llm-scrub verify` flags this with exit code 5.

## Stale lockfile

- **Symptom:** `llm-scrub scan` blocks with `LockError` but no other
  scrub process is running.
- **Mitigation:** `aurochs-core` records the holder PID in the
  lockfile and checks `psutil.pid_exists` on acquisition. A dead
  holder triggers force-release with an audit-log entry. Default
  acquisition timeout is 30s.
- **Recovery:** Wait the timeout (the lockfile auto-clears on
  PID-dead detection). If the holder PID is still alive but stuck,
  `kill` it; the next scrub will force-release.

## Backup collision

- **Symptom:** Running `llm-scrub scrub --in-place file.md` when
  `file.md.bak` already exists.
- **Behavior:** Default `--backup-mode rotate` renames the existing
  `.bak` to `.bak.<unix-ts>` before writing the new one. `--backup-mode
  fail` raises `ValueError`. `--backup-mode overwrite` clobbers.
- **Recovery:** None needed; `rotate` is the safe default.

## .scrubbed sibling collision under parallel runs

- **Symptom:** Two `scrub` processes running on the same file at the
  same time would race on `<file>.scrubbed`.
- **Mitigation:** Each process writes to
  `<file>.scrubbed.<pid>.<unix-ts>` first; once the writer holds
  `WriteLock` against the original file, the temp is renamed to
  `<file>.scrubbed`, rotating any existing copy. Plan v4 BLOCKER #10.

## Pre-commit hook + unstaged changes

- **Symptom:** A staged file also has unstaged changes; the hook would
  silently incorporate the unstaged work into the commit if it
  rewrote the file.
- **Mitigation:** `run_precommit` calls `git diff --name-only`
  (working-tree vs. index) and `git diff --cached --name-only` (index
  vs. HEAD). Any path in BOTH lists triggers refusal with exit code 2:
  `refusing to scrub files with both staged and unstaged changes —
  stash unstaged changes first.`
- **Recovery:** `git stash --keep-index`, retry the commit.

## Disk full during write

- **Symptom:** SQLite write fails with `SQLITE_FULL`.
- **Mitigation:** `BEGIN IMMEDIATE` transaction wraps the run-finalize
  INSERT block. The transaction rolls back cleanly. Locks release on
  abort.
- **Recovery:** Free space, retry.

## TODO

- Surface lockfile state in `llm-scrub status` (recall pattern; not
  yet wired in scrub v0.1).
- Wire `core.recovery.verify(deep=True)` into `llm-scrub verify
  --deep` end-to-end.
- Add a graceful checkpoint on SIGINT so the orphan-runs window
  narrows.
