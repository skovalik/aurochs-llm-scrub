# Concurrency

aurochs-llm-scrub is single-writer, multi-reader. Concurrency primitives
come from `aurochs-core` and are explicit, not implicit.

## Primitives

- **`WriteLock`** — OS-level write lockfile (`runs.db.write.lock`)
  next to the database. ANY writer (CLI `scan` / `scrub`, MCP server
  when persisting a run, the redactor when rewriting the target file)
  acquires this before opening a write connection. Default timeout
  30 seconds.
- **`MigrateLock`** — OS-level migration lockfile
  (`runs.db.migrate.lock`). Held only by `MigrationRunner`. Coexists
  with read traffic but blocks new writers. Default timeout 60
  seconds.
- **`PRAGMA busy_timeout = 30000`** — readers wait up to 30s on
  contention before raising `OperationalError: database is locked`.
- **`PRAGMA journal_mode = WAL`** — concurrent reads do not block on
  writers.
- **`PRAGMA synchronous = NORMAL`** — WAL-safe; cheaper than the
  default `FULL` on bulk inserts.
- **`PRAGMA foreign_keys = ON`** — enforced on every connection so
  orphan FKs cannot land at insert time.

`WriteLock` and `MigrateLock` both record the holder PID and use
`psutil.pid_exists` to detect stale lockfiles. A crashed writer that
left a lockfile behind is force-released on the next acquisition with
an audit-log entry.

## What `runs.db` write paths exist

| Path                                           | Acquires                  |
| ---------------------------------------------- | ------------------------- |
| `MigrationRunner.run()`                        | `MigrateLock`             |
| `register_pack(...)`                           | `WriteLock`               |
| `scan_text(...)` (final INSERT transaction)    | `WriteLock`               |
| `apply_redactions(... mode=SCRUB_IN_PLACE)`    | `WriteLock` on the target file (not on `runs.db`) |
| `apply_redactions(... mode=SCRUB_TO_STDOUT)`   | `WriteLock` on the target file (sidecar finalize step) |
| `_cmd_disable` / `_cmd_enable` (CLI)           | None — relies on `busy_timeout` for the brief UPDATE |

Read paths (`status`, `verify`, `runs list`, `runs show`, `runs diff`,
`rules list`, `rules show`) take no lock; they rely on WAL mode for
consistent snapshots.

## What concurrent CLI runs look like

- **Two `scan` invocations on different files at once.** Both can
  proceed; the engine's commit at finalize time briefly serializes on
  `WriteLock`. Net effect: a few ms of contention at the end of each
  run. Both produce their own `scrub_runs` row.
- **Two `scan` invocations on the same file at once.** Same as above —
  the file is read independently by each process; the dedup happens
  per-run, not across runs. Both runs produce findings rows; if you
  diff them (`llm-scrub runs diff`), the snippet sets should match.
- **`scan` plus `scrub --in-place` on the same file.** The `scrub`
  process holds `WriteLock` on the target file during the rewrite; the
  `scan` process reads the target before that lock is taken. Race
  outcome depends on which process opens the file first; neither
  corrupts state, but the `scan` may report findings that the `scrub`
  has just removed.
- **`scan` plus `MigrationRunner.run()`.** Migration blocks new
  writers; reads proceed. Once migration commits, the next scan
  picks up the new schema.
- **`init` while `scan` is running.** `init` calls `MigrationRunner`
  which takes `MigrateLock`. The `scan` waits up to 30s on its
  `WriteLock` (which is separate from `MigrateLock`), so they can
  coexist as long as the migration finishes inside the window. If
  migration takes longer, the second process fails with `LockError`
  and exit code 3.

## Windows lockfile semantics

`aurochs-core` opens lockfile FDs with `close_fds=True` and a
non-inheritable handle flag, so child processes don't accidentally
extend the lock's lifetime. The lockfile records the holder PID; on
startup a fresh process checks `psutil.pid_exists(holder_pid)`. If the
holder is dead the lockfile is force-released with an audit log entry.

## What `llm-scrub status` shows

```
version:         0.1.0
runs.db:         /home/you/.local/share/aurochs-llm-scrub/runs.db  (132.4KB)
schema:          v1
rule packs:      1
rules:           12 active / 12 total (0 disabled)
scrub runs:      47
disabled log:    0
last run:        #47 status=success findings=3
```

`status` does not show lockfile state in v0.1 (recall does; that path
will land in scrub when the `core.recovery` lockfile-introspection
helper is wired). For lockfile diagnostics, look at the
`<runs.db>.write.lock` and `<runs.db>.migrate.lock` files directly —
their content is the holder PID.

## SIGINT handling

A scrub run interrupted by Ctrl-C exits with code 130. The
`scrub_runs` row may be left at status `running` if the interrupt
arrives between `INSERT scrub_runs` and the final `UPDATE`; on the
next `llm-scrub verify` run, the orphan-runs check will surface it.

Future v0.2 work: graceful checkpoint on SIGINT so the orphan window
narrows. Tracked.

## Per-rule timeout vs. concurrency

The per-rule wall-clock timeout (2.0s default) runs the regex in a
**worker thread**, not a separate process. This is intentional:
spawning a process per chunk per rule would be prohibitive; a worker
thread with `join(timeout=N)` is portable across Linux / macOS /
Windows where signal-based timeouts aren't.

The trade-off: when a rule trips the timeout, the orphaned worker
thread eventually returns. We accept the leaked work in exchange for
cross-platform support. The auto-disable state machine ensures the
same rule never runs again in the same scan, so a single ReDoS pattern
costs at most one timeout's worth of CPU per chunk before being shut
off.
