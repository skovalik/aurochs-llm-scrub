"""Core dataclasses for aurochs-llm-scrub.

This is the canonical home for ``RulePack``, ``Rule``, ``ScrubRun``,
``Finding``, ``RuleMetrics``, and ``DisabledRuleEntry``. Pipeline + CLI +
MCP surfaces all import from here, so the surface here is the contract.
All public types are slot-based, kw-only, stdlib-only.

Stable identity rules (plan v4):

* ``Finding.sha256`` is ``sha256(role || RS || normalize(snippet) || FS ||
  rule_id || FS || file_path || FS || str(file_offset))`` where RS is the
  ASCII record-separator (0x1F) and FS is the field-separator (0x1E).
  Computed automatically from the position-tuple if not supplied.
* ``RulePack.sha256`` is the content hash of the loaded rule file (caller
  computes it; the dataclass just stores). Used to detect rule-pack drift
  between scrub runs.
* ``Rule.rule_id`` is the stable identifier within a pack — never mutated.
  Pack-namespacing prevents rule_id collisions across packs (BLOCKER fix
  from the 5-agent QA fold; rule_id keyed by ``(pack_name, pack_version,
  rule_id)`` triple in disabled_rules_log).
* ``ScrubRun.id`` and ``Finding.id`` are SQLite-assigned PKs.

Dataclasses use ``slots=True, kw_only=True`` so callers MUST pass kwargs
(reduces "did I get the positional order right?" bugs at call sites
where fields look similar — e.g. rule_id vs pack_id) and instances are
memory-cheap.

Why ``frozen=False``: ``ScrubRun.status`` advances pending → running →
success/partial/failed/aborted as the run progresses; ``Rule.enabled``
flips as the auto-disable state machine runs. Use the dataclass as a
row-shaped value, not as a frozen ID.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Versioned constants
# ---------------------------------------------------------------------------

# Bumping this requires a finding sha256 migration; tracked alongside
# schema_version so old findings can be re-hashed under the new rule.
HASH_INPUT_VERSION: int = 1

_RECORD_SEPARATOR: str = "\x1f"
_FIELD_SEPARATOR: str = "\x1e"
_WHITESPACE_RUN: re.Pattern[str] = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Enums (locked vocabularies)
# ---------------------------------------------------------------------------


class ScrubStatus(StrEnum):
    """Status of a scrub run.

    Plan v4 locked vocabulary. ``StrEnum`` (Python 3.11+) gives clean
    round-trip through sqlite TEXT columns: the CHECK constraint in the
    baseline schema matches the enum's ``.value`` strings, and
    ``str(ScrubStatus.SUCCESS)`` yields ``"success"`` (not
    ``"ScrubStatus.SUCCESS"``).

    Status meanings:
      pending  — row created, run not yet started
      running  — pipeline executing (chunking / language gate / scan)
      success  — completed cleanly; no engine errors
      partial  — completed but at least one rule auto-disabled or
                 chunk dropped; surfaces in report footer
      failed   — engine error halted the run; ended_at set, finding
                 totals reflect partial state at failure point
      aborted  — user-initiated cancellation (Ctrl-C, --max-runtime)
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


class ScrubMode(StrEnum):
    """Scrub invocation mode.

    Plan v4 locked vocabulary. Determines the pipeline's write behavior
    on the target file:

      scan              — read-only; report-only output (default)
      scrub-in-place    — writes ``<file>.bak`` then overwrites the
                          original; mtime-checked + lockfile-protected
      scrub-to-stdout   — writes the cleaned text to stdout, leaves the
                          original untouched (pipeline-friendly)
      precommit         — invoked from a pre-commit hook; ``--in-place``
                          semantics + git stage re-add + non-zero exit
                          on redacted secret per K3 lock
    """

    SCAN = "scan"
    SCRUB_IN_PLACE = "scrub-in-place"
    SCRUB_TO_STDOUT = "scrub-to-stdout"
    PRECOMMIT = "precommit"


# ---------------------------------------------------------------------------
# Hash + identity helpers
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Canonicalize text for sha256 stability.

    Trims leading/trailing whitespace and collapses every internal
    whitespace run (spaces, tabs, newlines, NBSP) to a single ASCII
    space. This is the v1 normalization rule; bumping it requires a
    finding sha256 migration.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    return _WHITESPACE_RUN.sub(" ", text).strip()


def compute_finding_sha256(
    *,
    snippet: str,
    rule_id: str,
    file_path: str,
    file_offset: int,
) -> str:
    """Compute the SHA-256 identity of a finding.

    Hash input format::

        sha256("finding" || RS || normalize(snippet) || FS || rule_id ||
               FS || file_path || FS || str(file_offset))

    The leading role tag (``"finding"``) reserves namespace so finding
    hashes cannot collide with rule-pack content hashes or any future
    role-tagged hash.

    The 4-tuple ``(snippet, rule_id, file_path, file_offset)`` makes the
    hash stable across re-runs that hit the same byte position with the
    same rule, AND distinct between two findings of the same rule at
    different offsets in the same file.

    Returns a 64-character lowercase hex digest.
    """
    payload = (
        f"finding{_RECORD_SEPARATOR}"
        f"{normalize_text(snippet)}{_FIELD_SEPARATOR}"
        f"{rule_id}{_FIELD_SEPARATOR}"
        f"{file_path}{_FIELD_SEPARATOR}"
        f"{file_offset}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_sha256_format(value: str, *, field_name: str) -> None:
    """Reject anything that isn't 64 lowercase hex chars."""
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(
            f"{field_name} must be 64 lowercase hex chars, got {value!r}"
        )


# ---------------------------------------------------------------------------
# RulePack
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class RulePack:
    """A loaded collection of detection rules.

    Rule packs are the unit of versioning: the default pack ships in the
    package, and users can register custom packs from
    ``~/.aurochs-llm-scrub/custom-rules.toml``. Each pack is uniquely
    identified by ``(name, version)`` — re-registering the same pair is
    a no-op (UNIQUE index in schema). Bumping ``version`` and
    re-registering creates a new row; old runs continue to reference the
    historical version through their ``rule_id`` FKs.

    The 5-agent QA fold locks: rule-pack snapshot at scrub-run init.
    Subsequent rule-pack updates don't affect in-flight runs; the pack
    is loaded into memory and never re-read mid-run.

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    name:
        Pack identifier (e.g. ``"default"``, ``"stefan-custom"``). Forms
        part of the UNIQUE constraint with ``version``.
    version:
        Semver-style pack version (e.g. ``"0.1.0"``). Bumps on rule
        additions, schema field additions, etc. Distinct from
        ``schema_version`` (which tracks the SQLite schema).
    schema_version:
        Schema version of the rule-pack file format itself. Plan v4
        locks ``"0.1.0"`` for v0.1; missing field defaults to ``"0.1.0"``
        in the loader; future bumps require ``llm-scrub rules migrate``.
    source_path:
        Filesystem path the pack was loaded from. ``None`` for the
        in-package default pack (no on-disk file).
    sha256:
        Content hash of the rule-pack file (or canonical serialization
        of the in-memory pack). Used to detect drift between runs.
    registered_at:
        Epoch seconds when the pack row was inserted.
    """

    id: int | None = None
    name: str
    version: str
    schema_version: str = "0.1.0"
    source_path: str | None = None
    sha256: str
    registered_at: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must be non-empty")
        if not self.version:
            raise ValueError("version must be non-empty")
        if not self.schema_version:
            raise ValueError("schema_version must be non-empty")
        if self.registered_at < 0:
            raise ValueError(
                f"registered_at must be non-negative, got {self.registered_at}"
            )
        _validate_sha256_format(self.sha256, field_name="sha256")


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class Rule:
    """A single detection rule within a rule pack.

    Rules carry ``language: str | None`` per plan v4 A1: ``None`` means
    truly mechanical (em dashes, whitespace) — runs regardless of the
    language gate; a string like ``"en"`` means English-coded — skipped
    when language gate fails.

    Severity defaults locked here are the pack author's intent;
    workspace ``[severity_overrides]`` config can promote/demote at
    runtime without mutating the row.

    Auto-disable lifecycle: when the ReDoS detector or per-chunk
    timeout trips a rule, ``enabled`` is flipped to ``False`` and an
    entry appended to ``disabled_rules_log``. Custom rules stay
    disabled until manually re-enabled (``llm-scrub rules enable
    <rule-id>``); default rules auto-re-enable on next CLI invocation
    per K2 carry-forward (defaults are trusted code; suppression masks
    engine bugs).

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    pack_id:
        FK into ``rule_packs.id``. Required.
    rule_id:
        Pack-stable identifier (e.g. ``"p0-em-dash"``,
        ``"stefan-pixel-perfect"``). UNIQUE within ``(pack_id, rule_id)``.
    name:
        Human-readable label (one line). Used in CLI output.
    pattern:
        The regex (or literal, if ``pattern_type = "literal"`` in the
        source TOML) string the rule matches. Stored verbatim; the
        engine compiles + applies wrappers (case-folding, word-boundary
        anchoring) at run time.
    severity_default:
        ``"P0" | "P1" | "P2"`` — the pack-author-intended severity.
        Workspace overrides can flip this at runtime (off-row; not
        persisted on the rule itself).
    language:
        ``None`` = truly mechanical, always runs. ``"en"`` = English-
        coded, gated by language detection. Future packs may use
        ``"de"``, ``"es"``, etc.
    description:
        Long-form rule rationale (one paragraph). Surfaced in
        ``llm-scrub rules show <rule-id>``.
    enabled:
        Runtime flag; flipped to ``False`` by the auto-disable state
        machine. Default ``True``. Disabled state persists across
        sessions for custom rules; default rules auto-re-enable.
    """

    id: int | None = None
    pack_id: int
    rule_id: str
    name: str
    pattern: str
    severity_default: str
    language: str | None = None
    description: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.pack_id <= 0:
            raise ValueError(f"pack_id must be positive, got {self.pack_id}")
        if not self.rule_id:
            raise ValueError("rule_id must be non-empty")
        if not self.name:
            raise ValueError("name must be non-empty")
        if not self.pattern:
            raise ValueError("pattern must be non-empty")
        if self.severity_default not in {"P0", "P1", "P2"}:
            raise ValueError(
                f"severity_default must be P0/P1/P2, got {self.severity_default!r}"
            )
        if self.language is not None and not self.language:
            raise ValueError("language must be non-empty string or None")
        if not isinstance(self.description, str):
            raise TypeError(
                f"description must be str, got {type(self.description).__name__}"
            )


# ---------------------------------------------------------------------------
# ScrubRun
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ScrubRun:
    """One invocation of the scrub pipeline.

    Tracks: when it ran, what status it ended in, what target it
    operated on, what mode it ran in, and aggregate finding counts.
    The single-writer invariant on ``runs.db`` (``runs.db.write.lock``)
    means concurrent invocations serialize at the OS level; this row
    is the audit trail.

    A ``status`` of ``partial`` is set when the run completed but at
    least one rule was auto-disabled or a chunk failed mid-run; the
    report footer surfaces the auto-disable count to close the silent-
    failure gap (K2 lock).

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    started_at:
        Epoch seconds when the run started.
    ended_at:
        Epoch seconds when the run ended; ``None`` while running.
    status:
        ``ScrubStatus`` enum — pending/running/success/partial/failed/aborted.
    target_path:
        Path of the file being scrubbed, or the literal string
        ``"<stdin>"`` for piped input.
    mode:
        ``ScrubMode`` enum — scan/scrub-in-place/scrub-to-stdout/precommit.
    total_findings:
        Aggregate count of findings emitted by this run (P0+P1+P2).
        Per-tier breakdown lives on the run's findings rows.
    total_redacted:
        Number of secret-scan redactions applied to the input before
        rules ran. K3-relevant: pre-commit mode exits non-zero when
        this is > 0.
    """

    id: int | None = None
    started_at: int
    ended_at: int | None = None
    status: ScrubStatus
    target_path: str
    mode: ScrubMode
    total_findings: int = 0
    total_redacted: int = 0

    def __post_init__(self) -> None:
        if self.started_at < 0:
            raise ValueError(
                f"started_at must be non-negative, got {self.started_at}"
            )
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError(
                f"ended_at ({self.ended_at}) must be >= started_at "
                f"({self.started_at})"
            )
        if not isinstance(self.status, ScrubStatus):
            raise TypeError(
                f"status must be ScrubStatus, got {type(self.status).__name__}"
            )
        if not isinstance(self.mode, ScrubMode):
            raise TypeError(
                f"mode must be ScrubMode, got {type(self.mode).__name__}"
            )
        if not self.target_path:
            raise ValueError("target_path must be non-empty")
        if self.total_findings < 0:
            raise ValueError(
                f"total_findings must be non-negative, got {self.total_findings}"
            )
        if self.total_redacted < 0:
            raise ValueError(
                f"total_redacted must be non-negative, got {self.total_redacted}"
            )


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class Finding:
    """A single rule match against a scrub run's input.

    Identity (``sha256``) is computed from the 4-tuple ``(snippet,
    rule_id, file_path, file_offset)`` so the same violation at the
    same byte position with the same rule is the same finding across
    re-runs (insert OR IGNORE on the unique index). Cross-chunk dedup
    relies on the ``(file_offset, length, rule_id)`` triple per BLOCKER
    fix #1 + #6 from the 5-agent QA fold.

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    run_id:
        FK into ``scrub_runs.id``. Required.
    rule_id:
        FK into ``rules.id``. Required.
    file_offset:
        Byte offset of the match within the file (file-relative; NOT
        chunk-relative — chunking discipline normalizes positions before
        emit per BLOCKER #6).
    length:
        Length of the matched text in bytes. Together with
        ``file_offset`` forms the unique position tuple for cross-chunk
        dedup.
    file_path:
        Path of the file the finding was emitted from. May equal
        ``ScrubRun.target_path``, but kept on the row to support multi-
        file scrub runs in v0.2 without a schema bump.
    line_number:
        1-based line number of the match. Computed by the engine from
        ``file_offset`` against the input. Stored for human-readable
        surfacing (CLI report, IDE plugin).
    severity:
        ``"P0" | "P1" | "P2"`` — runtime severity (after any
        ``[severity_overrides]`` resolution). May differ from the rule's
        ``severity_default``.
    snippet:
        Verbatim matched text plus surrounding context (line content
        with the match position highlightable). Subject to the secret-
        scan redaction map: any high-entropy / known-key substring is
        replaced with ``[REDACTED:CLASS]`` before the snippet leaves the
        engine.
    schema_version:
        ``HASH_INPUT_VERSION`` at write time. Lets re-hashing detect
        old rows on a future hash-rule bump.
    """

    id: int | None = None
    run_id: int
    rule_id: int
    file_offset: int
    length: int
    file_path: str
    line_number: int
    severity: str
    snippet: str
    schema_version: int = HASH_INPUT_VERSION

    def __post_init__(self) -> None:
        if self.run_id <= 0:
            raise ValueError(f"run_id must be positive, got {self.run_id}")
        if self.rule_id <= 0:
            raise ValueError(f"rule_id must be positive, got {self.rule_id}")
        if self.file_offset < 0:
            raise ValueError(
                f"file_offset must be non-negative, got {self.file_offset}"
            )
        if self.length <= 0:
            raise ValueError(f"length must be positive, got {self.length}")
        if not self.file_path:
            raise ValueError("file_path must be non-empty")
        if self.line_number <= 0:
            raise ValueError(
                f"line_number must be positive, got {self.line_number}"
            )
        if self.severity not in {"P0", "P1", "P2"}:
            raise ValueError(
                f"severity must be P0/P1/P2, got {self.severity!r}"
            )
        if not isinstance(self.snippet, str):
            raise TypeError(
                f"snippet must be str, got {type(self.snippet).__name__}"
            )
        if self.schema_version < 1:
            raise ValueError(
                f"schema_version must be >= 1, got {self.schema_version}"
            )


# ---------------------------------------------------------------------------
# RuleMetrics
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class RuleMetrics:
    """Per-rule per-run aggregate metrics.

    The metrics row is written once per ``(rule_id, run_id)`` pair at
    run completion; in-flight updates are accumulated in memory and
    flushed in a single statement to avoid hot-row contention with the
    findings INSERTs.

    ``hit_count`` and ``false_positive_count`` together drive the
    ``llm-scrub rules stats`` view that surfaces noisy rules; the
    ``latency_ms_total`` field surfaces engine-bug rules that consume
    disproportionate budget without finding much (a candidate for the
    auto-disable state machine).

    Orphan handling per MAJOR cluster fix from the 5-agent QA fold:
    when a rule is deleted from a pack, metrics rows are retained for
    historical queries; the rule_id FK uses ON DELETE CASCADE only when
    the pack is fully purged (rare). Normal rule deletion uses
    soft-archive in the ``rules`` table with ``archived = 1``; metrics
    survive.

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    rule_id:
        FK into ``rules.id``. Required.
    run_id:
        FK into ``scrub_runs.id``. Required. UNIQUE with rule_id.
    hit_count:
        Number of findings the rule emitted in this run.
    false_positive_count:
        Number of findings later marked as false-positive (e.g. via
        ``# scrub: ignore`` pragma, sidecar exemption, inline
        ``<!-- llm-scrub-redact: ignore -->``). Populated when the
        engine post-filters; rules that consistently false-positive
        bubble up via ``rules stats``.
    latency_ms_total:
        Total wall-clock milliseconds the rule consumed across all
        chunks of this run. Per-chunk timeout enforces a budget; this
        field surfaces "fast on most chunks, slow on one" patterns.
    """

    id: int | None = None
    rule_id: int
    run_id: int
    hit_count: int = 0
    false_positive_count: int = 0
    latency_ms_total: int = 0

    def __post_init__(self) -> None:
        if self.rule_id <= 0:
            raise ValueError(f"rule_id must be positive, got {self.rule_id}")
        if self.run_id <= 0:
            raise ValueError(f"run_id must be positive, got {self.run_id}")
        if self.hit_count < 0:
            raise ValueError(
                f"hit_count must be non-negative, got {self.hit_count}"
            )
        if self.false_positive_count < 0:
            raise ValueError(
                f"false_positive_count must be non-negative, "
                f"got {self.false_positive_count}"
            )
        if self.false_positive_count > self.hit_count:
            raise ValueError(
                f"false_positive_count ({self.false_positive_count}) cannot "
                f"exceed hit_count ({self.hit_count})"
            )
        if self.latency_ms_total < 0:
            raise ValueError(
                f"latency_ms_total must be non-negative, got "
                f"{self.latency_ms_total}"
            )


# ---------------------------------------------------------------------------
# DisabledRuleEntry
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class DisabledRuleEntry:
    """One audit-trail row for a rule auto-disabled by the engine.

    Plan v4 K1 lock framing: this is a *convenience cache for the rule
    engine, not a security boundary*. The mirror in
    ``~/.aurochs-llm-scrub/disabled-rules.log`` is editable by anyone
    with write access to the home directory; the SQL table is the
    authoritative source for in-process queries but neither surface is
    a security guarantee.

    Pack-namespacing on ``(pack_name, pack_version, rule_id)`` per
    MAJOR cluster fix from the 5-agent QA fold: prevents collisions
    when two distinct packs ship rules with the same ``rule_id``
    (e.g. ``"p0-em-dash"`` in default pack and a forked custom pack).

    Attributes
    ----------
    id:
        SQLite-assigned PK; ``None`` until inserted.
    pack_name:
        Name of the rule pack the disabled rule belongs to.
    pack_version:
        Version of the rule pack at the time of disable.
    rule_id:
        Pack-stable identifier of the rule (NOT the SQLite PK; this
        survives pack reloads).
    disabled_at:
        Epoch seconds when the disable happened.
    reason:
        Short token: ``"timeout"``, ``"redos-static"``,
        ``"manual"``, ``"engine-error"``. Free-form below the standard
        set; future runs SHOULD log new reasons here for diagnosis.
    redos_evidence:
        For ReDoS-related disables, the input snippet that tripped the
        timeout (truncated to 256 bytes, redacted via secret-scan).
        ``None`` for non-ReDoS reasons.
    """

    id: int | None = None
    pack_name: str
    pack_version: str
    rule_id: str
    disabled_at: int
    reason: str
    redos_evidence: str | None = None

    def __post_init__(self) -> None:
        if not self.pack_name:
            raise ValueError("pack_name must be non-empty")
        if not self.pack_version:
            raise ValueError("pack_version must be non-empty")
        if not self.rule_id:
            raise ValueError("rule_id must be non-empty")
        if self.disabled_at < 0:
            raise ValueError(
                f"disabled_at must be non-negative, got {self.disabled_at}"
            )
        if not self.reason:
            raise ValueError("reason must be non-empty")
        if self.redos_evidence is not None and not isinstance(
            self.redos_evidence, str
        ):
            raise TypeError(
                "redos_evidence must be str or None, got "
                f"{type(self.redos_evidence).__name__}"
            )
