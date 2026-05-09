"""``llm-scrub verify --deep`` analog.

Runs an integrity sweep over runs.db and emits a ``VerifyReport``
describing what's clean, what's degraded, and what needs operator
attention. Mirrors voice's pattern.

Plan v4 BLOCKER #3 closure: ``llm-scrub verify --deep`` walks the DB,
validates checksums, and reports orphan findings + dangling rule
metrics so callers can recover from corrupt state without losing all
history.

Quick mode (``deep=False``):
* sqlite ``PRAGMA integrity_check`` (catches gross corruption).
* Schema-version sanity (existence of expected tables).
* Orphan ``scrub_runs`` enumeration (status=running past the orphan
  window).

Deep mode (``deep=True``) additionally:
* ``PRAGMA wal_checkpoint(PASSIVE)`` to size the WAL.
* Findings whose rule FK references a non-existent rules row (orphan
  findings — should be zero given ON DELETE RESTRICT, but we check).
* Rule metrics whose run FK references a non-existent scrub_runs row
  (dangling — would normally cascade, but a manually-edited DB could
  leave them).
* disabled_rules_log entries whose ``rule_id`` doesn't appear in any
  registered pack (a rule was disabled, the pack was removed, the
  log row stayed — diagnostic only, not a blocker).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from aurochs_llm_scrub.core import (
    ScrubStatus,
    db_connect,
)

# A "running" scrub_run older than this is orphaned (vs. in-flight).
_ORPHAN_AGE_SECONDS: int = 60 * 60  # 1 hour


@dataclass(slots=True, kw_only=True)
class VerifyReport:
    """Result of ``verify(...)`` — operator-readable integrity summary.

    ``ok`` is the bottom-line green/red flag: True means no errors and
    no orphans. Warnings (large WAL, dangling metrics) don't flip
    ``ok``.

    The dataclass is iterable: ``for issue in report`` yields each
    error then each warning. CLI callers can ``list(report)`` and
    treat it as "things to print." A clean report iterates as an
    empty sequence.

    Attributes
    ----------
    ok:
        ``True`` if no errors, no orphan_findings, no orphan runs.
    errors:
        Hard problems — schema mismatch, missing tables, FK violations,
        WAL corruption.
    warnings:
        Soft problems — large WAL, dangling rule metrics, disabled-log
        entries with no matching pack.
    orphan_findings:
        Count of findings whose rule_id does not resolve to a row in
        the rules table.
    dangling_rule_metrics:
        Count of rule_metrics rows whose run_id has no matching
        scrub_runs row.
    orphan_runs:
        Count of scrub_runs older than the orphan threshold whose
        status is still ``running``.
    wal_size_pages:
        WAL file size in pages (``-1`` if WAL absent / not measurable).
    deep:
        Whether this report came from a deep walk.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    orphan_findings: int = 0
    dangling_rule_metrics: int = 0
    orphan_runs: int = 0
    wal_size_pages: int = -1
    deep: bool = False

    def __iter__(self) -> Iterator[str]:
        yield from self.errors
        yield from self.warnings

    def __bool__(self) -> bool:
        """Truthy iff there are issues to report (errors or warnings)."""
        return bool(self.errors) or bool(self.warnings)


def verify(db_path: Path, *, deep: bool = False) -> VerifyReport:
    """Run an integrity sweep on runs.db.

    Parameters
    ----------
    db_path:
        Path to runs.db.
    deep:
        Enable deep checks (orphan FK walks, dangling-metric scan,
        WAL size). Adds a few ms on a typical-size DB.

    Returns
    -------
    VerifyReport
        Populated; ``ok=True`` only when ``errors`` and orphan counts
        are all zero.
    """
    errors: list[str] = []
    warnings: list[str] = []
    orphan_findings = 0
    dangling_metrics = 0
    orphan_runs = 0
    wal_pages = -1

    if not db_path.exists():
        errors.append(f"runs.db not found at {db_path}")
        return VerifyReport(
            ok=False,
            errors=errors,
            warnings=warnings,
            orphan_findings=0,
            dangling_rule_metrics=0,
            orphan_runs=0,
            wal_size_pages=-1,
            deep=deep,
        )

    conn = db_connect(db_path)
    try:
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            errors.append(f"integrity_check raised {exc!r}")
            integrity = None
        if integrity is not None and integrity[0] != "ok":
            errors.append(f"integrity_check: {integrity[0]!r}")

        actual_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for required in (
            "rule_packs",
            "rules",
            "scrub_runs",
            "findings",
            "rule_metrics",
            "disabled_rules_log",
            "schema_version",
        ):
            if required not in actual_tables:
                errors.append(f"missing table: {required}")

        # Orphan runs: status='running' older than threshold.
        cutoff = int(time.time()) - _ORPHAN_AGE_SECONDS
        orow = conn.execute(
            "SELECT COUNT(*) FROM scrub_runs "
            "WHERE status = ? AND started_at < ?",
            (ScrubStatus.RUNNING.value, cutoff),
        ).fetchone()
        orphan_runs = int(orow[0]) if orow else 0
        if orphan_runs > 0:
            warnings.append(
                f"{orphan_runs} scrub run(s) stuck in 'running' state "
                "older than 1 hour — likely interrupted"
            )

        if deep:
            try:
                ck = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if ck is not None and ck[1] is not None:
                    wal_pages = int(ck[1])
                    if wal_pages > 1000:
                        warnings.append(
                            f"WAL is large ({wal_pages} pages); "
                            "consider running 'llm-scrub checkpoint'"
                        )
            except sqlite3.DatabaseError as exc:
                warnings.append(f"wal_checkpoint failed: {exc!r}")

            # Orphan findings: rule_id not in rules table.
            row = conn.execute(
                "SELECT COUNT(*) FROM findings f "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM rules r WHERE r.id = f.rule_id"
                ")"
            ).fetchone()
            orphan_findings = int(row[0]) if row else 0
            if orphan_findings > 0:
                errors.append(
                    f"{orphan_findings} finding(s) reference a missing "
                    "rule row — restore rules table or delete orphans"
                )

            # Dangling rule_metrics: run_id not in scrub_runs.
            row = conn.execute(
                "SELECT COUNT(*) FROM rule_metrics m "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM scrub_runs r WHERE r.id = m.run_id"
                ")"
            ).fetchone()
            dangling_metrics = int(row[0]) if row else 0
            if dangling_metrics > 0:
                warnings.append(
                    f"{dangling_metrics} rule_metrics row(s) reference a "
                    "missing scrub_run — manual cleanup recommended"
                )

            # Disabled-log entries with no matching pack — informational.
            row = conn.execute(
                "SELECT COUNT(*) FROM disabled_rules_log d "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM rule_packs p "
                "  WHERE p.name = d.pack_name AND p.version = d.pack_version"
                ")"
            ).fetchone()
            unmatched = int(row[0]) if row else 0
            if unmatched > 0:
                warnings.append(
                    f"{unmatched} disabled_rules_log row(s) reference a "
                    "pack/version that is no longer registered"
                )
    finally:
        conn.close()

    ok = (
        not errors
        and orphan_findings == 0
        and orphan_runs == 0
    )
    return VerifyReport(
        ok=ok,
        errors=errors,
        warnings=warnings,
        orphan_findings=orphan_findings,
        dangling_rule_metrics=dangling_metrics,
        orphan_runs=orphan_runs,
        wal_size_pages=wal_pages,
        deep=deep,
    )


__all__ = [
    "VerifyReport",
    "verify",
]
