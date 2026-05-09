# Migrations

aurochs-llm-scrub manages its SQLite schema through a runner that
takes migration safety seriously. The `MigrationRunner` is in
`aurochs_llm_scrub/core/db.py`; the migration files live under
`aurochs_llm_scrub/core/migrations/`.

## How it runs

- Migration files are named `NNNN_<description>.sql` where `NNNN` is a
  zero-padded integer (e.g. `0001_initial.sql`).
- `schema_version` table tracks the highest applied version + a
  `status` field (`applied` / `failed` / `in_progress`).
- The runner takes `BEGIN EXCLUSIVE` plus an OS-level advisory
  lockfile (`runs.db.migrate.lock`) for the duration.
- Migrations are sequentially enforced — version 3 cannot run unless
  version 2 is `applied`.
- The `--baseline-from-existing` flag (constructor argument, not CLI
  flag in v0.1) handles the case where a `runs.db` file exists with
  schema (manually created, copied from another machine) but no
  `schema_version` table. In that mode, applying migration 1 stamps
  `schema_version=1` without re-running the DDL.

## When migrations run

- On `llm-scrub init` — applies all pending migrations to bring the
  DB to the latest version.
- The runner is idempotent: if no new migrations apply, it returns
  immediately.
- If a migration is `in_progress` (interrupted mid-flight, lockfile
  released), the next runner detects the partial state and refuses
  with `MigrationError("Detected partial migration at version N.
  Manual recovery required.")`.
- If the runner detects a stale `MigrateLock` (PID dead per
  `psutil.pid_exists`), it logs an audit entry and force-releases.

## Failure handling

The baseline migration (version 1) wraps the DDL + the
`schema_version` stamp in a single `BEGIN EXCLUSIVE` transaction so a
crash mid-DDL leaves nothing partial. v2+ migrations pre-stamp
`status='in_progress'` before applying the script; success flips it
to `applied`, failure rolls back and leaves the tombstone for the
next run to detect.

`llm-scrub verify` exits with code 5 when it finds an `in_progress`
row; the operator's job is to inspect, decide on roll-forward vs.
drop-partial, and re-run.

## What `runs.db` looks like after migration 1

Seven tables, all defined in `0001_initial.sql`:

```
rule_packs            One row per loaded pack. UNIQUE on (name, version).
rules                 One row per rule. UNIQUE on (pack_id, rule_id).
scrub_runs            One row per scrub invocation.
findings              One row per rule match. FK to scrub_runs and rules.
rule_metrics          Per-(rule_id, run_id) aggregates.
disabled_rules_log    Audit trail of auto-disables. Pack-namespaced.
schema_version        Migration tracking. Mirror of voice/recall pattern.
```

Plus indexes for the hot read paths (rule lookup by pack, rules by
severity, findings by run, findings by file_path + run, findings
position-tuple for cross-chunk dedup, disabled-rules by pack /
rule / disabled_at / reason).

Full DDL: `aurochs_llm_scrub/core/migrations/0001_initial.sql`.

## Adding a new migration

1. Create `aurochs_llm_scrub/core/migrations/NNNN_<slug>.sql` where
   `NNNN` is the next integer (zero-padded).
2. Write idempotent DDL — `CREATE TABLE IF NOT EXISTS`, `INSERT OR
   IGNORE`, etc. The runner wraps the script in `BEGIN EXCLUSIVE`,
   but defensive idempotence guards against partial failures.
3. Bump `aurochs_llm_scrub/core/schema.py::CURRENT_SCHEMA_VERSION`
   to match the new file's prefix.
4. Bump `SCHEMA_VERSION` (the semver string) per the policy in
   [contracts.md](contracts.md):
   - **Patch** (`0.1.0` → `0.1.1`): additive optional fields only.
   - **Minor** (`0.1.x` → `0.2.0`): additive required fields, ship a
     migration helper.
   - **Major** (`0.x.y` → `1.0.0`): breaking rename / removal /
     semantic shift, ship a migration helper for one major back.
5. Add tests under `tests/test_db.py` covering the new migration
   path, including the `--baseline-from-existing` case.
6. Update `docs/contracts.md` if the migration changes any of the
   contract surfaces (MCP tool schemas, on-disk schema, public
   types).

## Schema-version contract surfaces

The schema version is recorded in three places that must agree:

1. `core/schema.py::SCHEMA_VERSION` (semver string, recorded in
   `manifest.json`).
2. `core/schema.py::CURRENT_SCHEMA_VERSION` (integer, matches the
   prefix on the latest migration file).
3. `mcp/schemas.py::SCHEMA_VERSION` (semver string for MCP tool
   responses; tracks the contract surface).

A test in `tests/test_mcp_scrub_server.py` cross-checks alignment
between `core.types.ScrubStatus` / `ScrubMode` enums and
`mcp.schemas.SCRUB_STATUS_VALUES` / `SCRUB_MODE_VALUES` so a divergence
fails loud at server startup.

## Cross-plugin manifest

`<data_dir>/manifest.json` is written by `llm-scrub init` and refreshed
on schema changes:

```json
{
  "schema_version": "0.1.0",
  "schema_version_int": 1,
  "runs_db_path": "/home/you/.local/share/aurochs-llm-scrub/runs.db",
  "written_at": 1735689600
}
```

Sister plugins (`aurochs-voice`, `aurochs-recall`, `aurochs-seo-geo`)
read this file to detect cross-plugin compat. Plugins refuse to
operate on a `schema_version` newer than their compiled-in version.
See [contracts.md](contracts.md) for the full bridge contract.

## TODO

- Migration list with version history filled in as v0.2 lands the
  first additive migration.
- Rollback notes for major migrations (helper-shipped one-major-back
  policy).
