-- aurochs-llm-scrub — baseline schema (v0.1.0)
-- Plan v4 reference implementation, T0 spine scope.
--
-- T0 SCOPE NOTE: this baseline includes the six primary tables
-- (rule_packs, rules, scrub_runs, findings, rule_metrics,
-- disabled_rules_log) plus the schema_version operational table.
-- Rule pack snapshot rows + WAL-recovery checkpoint state + opt-in
-- history surfaces (`rule_set_versions` per the plan's data-model
-- section) are deferred to a follow-up migration — they are not on
-- the spine read path for the T0 MVP.

-- ============================================================================
-- Rule packs (the rule-set versioning axis)
-- ============================================================================
-- (name, version) is the unique identity. Re-registering the same
-- pair is a no-op via INSERT OR IGNORE; bumping version creates a
-- new row, and old findings continue to reference the historical
-- version through their rule.pack_id FK.

CREATE TABLE IF NOT EXISTS rule_packs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  schema_version TEXT NOT NULL DEFAULT '0.1.0',
  source_path TEXT,
  sha256 TEXT NOT NULL,
  registered_at INTEGER NOT NULL,
  CHECK (LENGTH(name) > 0),
  CHECK (LENGTH(version) > 0),
  CHECK (LENGTH(schema_version) > 0),
  CHECK (LENGTH(sha256) = 64),
  CHECK (registered_at >= 0),
  UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_packs_name
  ON rule_packs(name);
CREATE INDEX IF NOT EXISTS idx_rule_packs_registered_at
  ON rule_packs(registered_at);

-- ============================================================================
-- Rules (the detection units)
-- ============================================================================
-- rule_id is the pack-stable identifier (e.g. "p0-em-dash"); the
-- (pack_id, rule_id) pair is unique per pack. severity_default is
-- the pack-author-intended severity; runtime [severity_overrides]
-- can promote/demote without mutating the row. language is NULL for
-- mechanical (always-on) rules and a string like "en" for
-- language-coded rules per plan v4 A1.

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  pack_id INTEGER NOT NULL,
  rule_id TEXT NOT NULL,
  name TEXT NOT NULL,
  pattern TEXT NOT NULL,
  severity_default TEXT NOT NULL,
  language TEXT,
  description TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  CHECK (LENGTH(rule_id) > 0),
  CHECK (LENGTH(name) > 0),
  CHECK (LENGTH(pattern) > 0),
  CHECK (severity_default IN ('P0', 'P1', 'P2')),
  CHECK (language IS NULL OR LENGTH(language) > 0),
  CHECK (enabled IN (0, 1)),
  UNIQUE (pack_id, rule_id),
  FOREIGN KEY (pack_id) REFERENCES rule_packs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rules_pack
  ON rules(pack_id);
CREATE INDEX IF NOT EXISTS idx_rules_severity
  ON rules(severity_default);
CREATE INDEX IF NOT EXISTS idx_rules_enabled
  ON rules(enabled) WHERE enabled = 1;
CREATE INDEX IF NOT EXISTS idx_rules_language
  ON rules(language);

-- ============================================================================
-- Scrub runs (one row per pipeline invocation)
-- ============================================================================
-- status enum locked per plan v4 ScrubStatus.
-- mode enum locked per plan v4 ScrubMode.
-- target_path may be a file path or the literal string "<stdin>".

CREATE TABLE IF NOT EXISTS scrub_runs (
  id INTEGER PRIMARY KEY,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  status TEXT NOT NULL,
  target_path TEXT NOT NULL,
  mode TEXT NOT NULL,
  total_findings INTEGER NOT NULL DEFAULT 0,
  total_redacted INTEGER NOT NULL DEFAULT 0,
  CHECK (status IN ('pending', 'running', 'success', 'partial', 'failed', 'aborted')),
  CHECK (mode IN ('scan', 'scrub-in-place', 'scrub-to-stdout', 'precommit')),
  CHECK (started_at >= 0),
  CHECK (ended_at IS NULL OR ended_at >= started_at),
  CHECK (LENGTH(target_path) > 0),
  CHECK (total_findings >= 0),
  CHECK (total_redacted >= 0)
);

CREATE INDEX IF NOT EXISTS idx_scrub_runs_status
  ON scrub_runs(status);
CREATE INDEX IF NOT EXISTS idx_scrub_runs_started_at
  ON scrub_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_scrub_runs_target_path
  ON scrub_runs(target_path);
CREATE INDEX IF NOT EXISTS idx_scrub_runs_mode
  ON scrub_runs(mode);

-- ============================================================================
-- Findings (one row per rule match)
-- ============================================================================
-- (file_offset, length, rule_id) triple is the cross-chunk dedup key
-- per BLOCKER #1 + #6 from the 5-agent QA fold; the ON DELETE
-- CASCADE on run_id keeps findings tied to their run.
-- file_path is denormalized from scrub_runs.target_path so v0.2 can
-- support multi-file runs without a schema bump (and so a finding
-- carries enough context to render in isolation).

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  rule_id INTEGER NOT NULL,
  file_offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  file_path TEXT NOT NULL,
  line_number INTEGER NOT NULL,
  severity TEXT NOT NULL,
  snippet TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  CHECK (file_offset >= 0),
  CHECK (length > 0),
  CHECK (LENGTH(file_path) > 0),
  CHECK (line_number > 0),
  CHECK (severity IN ('P0', 'P1', 'P2')),
  CHECK (schema_version >= 1),
  FOREIGN KEY (run_id) REFERENCES scrub_runs(id) ON DELETE CASCADE,
  FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_findings_run
  ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_file_path_run
  ON findings(file_path, run_id);
CREATE INDEX IF NOT EXISTS idx_findings_rule
  ON findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_findings_severity
  ON findings(severity);
-- Cross-chunk dedup support: position-tuple identity within a run.
-- Not UNIQUE because pre-dedup INSERTs may stage duplicates that the
-- engine's dedup pass collapses; uniqueness is enforced in code at
-- finalize time, not at insert time.
CREATE INDEX IF NOT EXISTS idx_findings_position
  ON findings(run_id, file_offset, length, rule_id);

-- ============================================================================
-- Rule metrics (per-rule per-run aggregates)
-- ============================================================================
-- Written once per (rule_id, run_id) pair at run completion;
-- in-flight updates are accumulated in memory and flushed in a
-- single statement to avoid hot-row contention with findings
-- INSERTs. (rule_id, run_id) is unique to enforce the one-row-per-
-- pair contract.

CREATE TABLE IF NOT EXISTS rule_metrics (
  id INTEGER PRIMARY KEY,
  rule_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0,
  false_positive_count INTEGER NOT NULL DEFAULT 0,
  latency_ms_total INTEGER NOT NULL DEFAULT 0,
  CHECK (hit_count >= 0),
  CHECK (false_positive_count >= 0),
  CHECK (false_positive_count <= hit_count),
  CHECK (latency_ms_total >= 0),
  UNIQUE (rule_id, run_id),
  FOREIGN KEY (rule_id) REFERENCES rules(id) ON DELETE RESTRICT,
  FOREIGN KEY (run_id) REFERENCES scrub_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rule_metrics_rule
  ON rule_metrics(rule_id);
CREATE INDEX IF NOT EXISTS idx_rule_metrics_run
  ON rule_metrics(run_id);
-- Hot path: "show me rules that are slow" — partial index on
-- positive latency keeps the trivial-case cost down.
CREATE INDEX IF NOT EXISTS idx_rule_metrics_latency
  ON rule_metrics(latency_ms_total) WHERE latency_ms_total > 0;

-- ============================================================================
-- Disabled rules log (audit trail mirror of disabled-rules.log)
-- ============================================================================
-- Pack-namespaced via (pack_name, pack_version, rule_id) per MAJOR
-- cluster fix from the 5-agent QA fold (rule_id collisions across
-- packs). NOT a security boundary per K1 lock — see
-- docs/security.md. The on-disk plain-text log is the user-facing
-- mirror; this table is the authoritative in-process query surface.

CREATE TABLE IF NOT EXISTS disabled_rules_log (
  id INTEGER PRIMARY KEY,
  pack_name TEXT NOT NULL,
  pack_version TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  disabled_at INTEGER NOT NULL,
  reason TEXT NOT NULL,
  redos_evidence TEXT,
  CHECK (LENGTH(pack_name) > 0),
  CHECK (LENGTH(pack_version) > 0),
  CHECK (LENGTH(rule_id) > 0),
  CHECK (disabled_at >= 0),
  CHECK (LENGTH(reason) > 0)
);

CREATE INDEX IF NOT EXISTS idx_disabled_rules_pack
  ON disabled_rules_log(pack_name, pack_version);
CREATE INDEX IF NOT EXISTS idx_disabled_rules_rule
  ON disabled_rules_log(rule_id);
CREATE INDEX IF NOT EXISTS idx_disabled_rules_disabled_at
  ON disabled_rules_log(disabled_at);
CREATE INDEX IF NOT EXISTS idx_disabled_rules_reason
  ON disabled_rules_log(reason);

-- ============================================================================
-- Operational: schema_version
-- ============================================================================
-- Tracks migration state with a status field so partial migrations
-- (interrupted mid-run) can be detected on the next attempt. Mirror
-- of voice/recall pattern.

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL,
  description TEXT,
  status TEXT NOT NULL,
  CHECK (status IN ('in_progress', 'applied', 'failed'))
);
