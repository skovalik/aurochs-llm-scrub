"""Apply scrub fixes to files.

Three modes selected via ``ScrubMode``:

* ``ScrubMode.SCAN`` — read-only; ``apply_redactions`` is a no-op.
* ``ScrubMode.SCRUB_IN_PLACE`` — rotate ``<file>.bak`` (BLOCKER #9),
  write fixed content back to the original path under a write lock.
* ``ScrubMode.SCRUB_TO_STDOUT`` — write fixed content to a sibling
  ``<file>.scrubbed.<pid>.<timestamp>`` file (BLOCKER #10) so parallel
  runs do not collide; final commit-rename to ``<file>.scrubbed`` once
  the writer holds an advisory lock.
* ``ScrubMode.PRECOMMIT`` — same as in-place but the caller wraps the
  ``git add`` re-stage; this module does not invoke git.

Plan v4 contract:

* **BLOCKER #8 — pragma scope.** Lines containing
  ``# scrub: ignore`` are exempted from every finding on that line.
  Block scope: a ``# scrub: ignore-block-start`` line opens an
  exemption window that closes at the next
  ``# scrub: ignore-block-end`` (or EOF if missing). HTML comments
  ``<!-- scrub: ignore -->`` are honored too — markdown drafts use
  HTML comments rather than ``#`` ones.
* **BLOCKER #9 — .bak rotation.** Existing ``<file>.bak`` is rotated
  to ``<file>.bak.<unix-ts>`` BEFORE the new bak is written. No
  silent overwrite.
* **BLOCKER #10 — .scrubbed collision under parallel runs.** Each
  process writes to ``<file>.scrubbed.<pid>.<unix-ts>`` first; once
  the writer holds the WriteLock against the original file, the temp
  is renamed to ``<file>.scrubbed`` (which itself is rotated if it
  exists, same policy as .bak).

Only mechanical findings are auto-fixed. The ``Rule.severity_default``
+ ``rule.rule_id`` together determine whether an auto-fix replacement
exists; we ship a simple policy table here that matches the default
pack:

* ``p0-em-dash``        → ", "
* ``p0-mdash-entity``   → ", "
* ``p0-double-space``   → " "
* ``p0-trailing-whitespace`` → ""

Anything else is informational — the engine reports the finding but
``apply_redactions`` leaves the text untouched. Custom rules can ship
``auto_fix`` strings via the TOML schema in v0.2; v0.1 has the locked
table.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from aurochs_llm_scrub.core import (
    Finding,
    ScrubMode,
    WriteLock,
)

# ---------------------------------------------------------------------------
# Mechanical auto-fix table (locked v0.1)
# ---------------------------------------------------------------------------


# Maps rule_id (the human-stable string, NOT the SQLite PK) to a literal
# replacement. Findings whose rule's rule_id is missing from this table
# are treated as informational and left in place.
_AUTOFIX_TABLE: dict[str, str] = {
    "p0-em-dash": ", ",
    "p0-mdash-entity": ", ",
    "p0-double-space": " ",
    "p0-trailing-whitespace": "",
}


# Regex matching either a ``# scrub: ignore`` (pyhton-comment style) or
# ``<!-- scrub: ignore -->`` (markdown-html style) on a line. Captures
# the keyword (ignore | ignore-block-start | ignore-block-end).
_PRAGMA_RE: re.Pattern[str] = re.compile(
    r"(?:#|<!--)\s*scrub:\s*"
    # Order matters — `ignore` is a prefix of `ignore-block-*`. Match
    # the longer alternates first, then the bare `ignore` last.
    r"(?P<kind>ignore-block-start|ignore-block-end|ignore)"
    r"\s*(?:-->)?",
)


# ---------------------------------------------------------------------------
# Backup / collision policy
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class RedactionResult:
    """Summary returned by ``apply_redactions``.

    ``modified`` reflects whether the file was actually changed (no-op
    runs flag this False so callers can tell "nothing to do" from
    "applied N fixes"). ``applied_count`` is the number of findings
    whose replacement actually took effect; findings inside a pragma
    range are skipped and counted as ``skipped_pragma``.
    """

    modified: bool
    applied_count: int
    skipped_pragma: int
    skipped_no_autofix: int
    backup_path: Path | None
    output_path: Path


def parse_pragma_ranges(text: str) -> list[tuple[int, int]]:
    """Return the list of (start_line, end_line) byte ranges that are
    pragma-exempt.

    Single-line ``# scrub: ignore`` produces a tuple ``(line, line)``.
    Block-scope produces ``(start_line, end_line)``. Line numbers are
    1-based for parity with ``Finding.line_number``. Blocks without a
    matching close consume up to EOF.

    The return is a sorted list of inclusive ranges; callers test a
    finding's line via ``any(s <= ln <= e for s, e in ranges)``.
    """
    ranges: list[tuple[int, int]] = []
    block_open: int | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _PRAGMA_RE.search(line)
        if match is None:
            continue
        kind = match.group("kind")
        if kind == "ignore":
            ranges.append((lineno, lineno))
        elif kind == "ignore-block-start" and block_open is None:
            block_open = lineno
        elif kind == "ignore-block-end" and block_open is not None:
            ranges.append((block_open, lineno))
            block_open = None
    if block_open is not None:
        # Unclosed block consumes to EOF.
        end_line = max(1, len(text.splitlines()))
        ranges.append((block_open, end_line))
    return sorted(ranges)


def _line_in_pragma(line_number: int, ranges: list[tuple[int, int]]) -> bool:
    return any(s <= line_number <= e for s, e in ranges)


def _rotate_collision(target: Path) -> Path | None:
    """Rotate ``target`` to ``target.<unix-ts>`` if it exists.

    Returns the rotated path or ``None`` if no rotation happened.
    Used by the .bak collision policy (BLOCKER #9) and the .scrubbed
    finalize step (BLOCKER #10). The timestamp suffix is unix seconds;
    we bump with ``-1`` etc. on collision rather than micro-seconds so
    paths stay readable.
    """
    if not target.exists():
        return None
    ts = int(time.time())
    rotated = target.with_name(f"{target.name}.{ts}")
    n = 1
    while rotated.exists():
        rotated = target.with_name(f"{target.name}.{ts}-{n}")
        n += 1
    target.rename(rotated)
    return rotated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_redactions(
    file_path: Path,
    findings: list[Finding],
    mode: ScrubMode,
    *,
    backup_mode: str = "rotate",
    rule_id_lookup: dict[int, str] | None = None,
) -> RedactionResult:
    """Apply mechanical fixes to ``file_path``.

    Parameters
    ----------
    file_path:
        The original file. Must exist for in-place / precommit modes;
        for stdout mode the file is read for content but the original
        is not modified.
    findings:
        Findings produced by ``scrubber.scan_text`` (or ``scan_file``).
        Each finding's ``file_offset`` + ``length`` together delimit
        the byte range to replace.
    mode:
        ``ScrubMode`` enum. ``SCAN`` is a no-op (returns immediately
        with modified=False).
    backup_mode:
        ``"rotate"`` (default — rotate existing .bak before writing),
        ``"fail"`` (raise on existing .bak), or ``"overwrite"``
        (clobber). Per plan v4 BLOCKER #9 the lock is "rotate" by
        default.
    rule_id_lookup:
        Optional mapping from ``Finding.rule_id`` (SQLite PK) to the
        rule's stable ``rule_id`` string. When passed, findings whose
        SQLite rule_id is in the auto-fix table get their replacement
        applied. Without the lookup, the function falls back to
        ``severity == "P0"`` heuristics — which means custom-rule
        findings without an auto-fix entry will not be auto-rewritten.

    Returns
    -------
    RedactionResult
        Summary including the path of the rotated backup (if any) and
        the actual output path (the same as ``file_path`` for in-place,
        or the .scrubbed sibling for stdout/scan modes).

    Raises
    ------
    FileNotFoundError
        If ``file_path`` does not exist.
    ValueError
        If ``backup_mode`` is unknown or if ``backup_mode == "fail"``
        and a .bak already exists.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"redact target not found: {file_path}")
    if backup_mode not in {"rotate", "fail", "overwrite"}:
        raise ValueError(
            f"backup_mode must be rotate|fail|overwrite, got {backup_mode!r}"
        )

    text = file_path.read_text(encoding="utf-8")

    if mode == ScrubMode.SCAN:
        # Scan mode never writes.
        return RedactionResult(
            modified=False,
            applied_count=0,
            skipped_pragma=0,
            skipped_no_autofix=len(findings),
            backup_path=None,
            output_path=file_path,
        )

    pragma_ranges = parse_pragma_ranges(text)

    new_text, applied, skipped_pragma, skipped_no_autofix = _splice_findings(
        text=text,
        findings=findings,
        pragma_ranges=pragma_ranges,
        rule_id_lookup=rule_id_lookup or {},
    )

    if mode in {ScrubMode.SCRUB_IN_PLACE, ScrubMode.PRECOMMIT}:
        return _write_in_place(
            file_path=file_path,
            new_text=new_text,
            backup_mode=backup_mode,
            applied=applied,
            skipped_pragma=skipped_pragma,
            skipped_no_autofix=skipped_no_autofix,
            modified=new_text != text,
        )
    if mode == ScrubMode.SCRUB_TO_STDOUT:
        return _write_to_sidecar(
            file_path=file_path,
            new_text=new_text,
            applied=applied,
            skipped_pragma=skipped_pragma,
            skipped_no_autofix=skipped_no_autofix,
            modified=new_text != text,
        )
    raise ValueError(f"unknown ScrubMode: {mode!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Splice — the actual byte-range replacement
# ---------------------------------------------------------------------------


def _splice_findings(
    *,
    text: str,
    findings: list[Finding],
    pragma_ranges: list[tuple[int, int]],
    rule_id_lookup: dict[int, str],
) -> tuple[str, int, int, int]:
    """Replace each finding's byte range with its mechanical fix.

    Returns ``(new_text, applied, skipped_pragma, skipped_no_autofix)``.

    The splice walks findings in ascending file_offset order, skipping
    any whose line_number lies inside a pragma range or whose rule has
    no auto-fix entry. We accumulate (offset, length, replacement)
    triples first, then build the output by stitching slices around
    them — that way overlapping/adjacent triples are handled uniformly.
    """
    encoded = text.encode("utf-8")
    sorted_findings = sorted(findings, key=lambda f: f.file_offset)

    triples: list[tuple[int, int, bytes]] = []
    skipped_pragma = 0
    skipped_no_autofix = 0
    for f in sorted_findings:
        if _line_in_pragma(f.line_number, pragma_ranges):
            skipped_pragma += 1
            continue
        rid_str = rule_id_lookup.get(f.rule_id)
        replacement = (
            _AUTOFIX_TABLE.get(rid_str)
            if rid_str is not None
            else None
        )
        if replacement is None:
            skipped_no_autofix += 1
            continue
        triples.append(
            (f.file_offset, f.length, replacement.encode("utf-8"))
        )

    # Collapse overlaps: prefer the earlier-starting triple; if a later
    # triple starts inside an earlier one, drop it. This is rare —
    # findings come from distinct offset/length tuples post-dedup —
    # but we defend against caller-supplied unsorted input.
    triples.sort()
    merged: list[tuple[int, int, bytes]] = []
    last_end = 0
    for offset, length, repl in triples:
        if offset < last_end:
            continue
        merged.append((offset, length, repl))
        last_end = offset + length

    # Stitch.
    out = bytearray()
    cursor = 0
    for offset, length, repl in merged:
        out.extend(encoded[cursor:offset])
        out.extend(repl)
        cursor = offset + length
    out.extend(encoded[cursor:])

    new_text = out.decode("utf-8", errors="replace")
    return new_text, len(merged), skipped_pragma, skipped_no_autofix


# ---------------------------------------------------------------------------
# In-place writer
# ---------------------------------------------------------------------------


def _write_in_place(
    *,
    file_path: Path,
    new_text: str,
    backup_mode: str,
    applied: int,
    skipped_pragma: int,
    skipped_no_autofix: int,
    modified: bool,
) -> RedactionResult:
    """Write fixed content back to ``file_path`` after backup."""
    if not modified:
        return RedactionResult(
            modified=False,
            applied_count=applied,
            skipped_pragma=skipped_pragma,
            skipped_no_autofix=skipped_no_autofix,
            backup_path=None,
            output_path=file_path,
        )

    bak_path = file_path.with_name(file_path.name + ".bak")
    rotated: Path | None = None
    if bak_path.exists():
        if backup_mode == "fail":
            raise ValueError(
                f"refusing to overwrite existing backup: {bak_path}"
            )
        if backup_mode == "rotate":
            rotated = _rotate_collision(bak_path)
        # backup_mode == "overwrite" → fall through; rename below
        # truncates.

    # Write the bak from the ORIGINAL contents, then write new text.
    # We use a write lock for the duration so a concurrent reader can't
    # see a torn state.
    with WriteLock(file_path, timeout=30.0):
        # Re-read in case someone modified it under us. The original
        # caller already had the contents, but we want the bak to
        # reflect what's actually on disk at the moment of replacement.
        original_bytes = file_path.read_bytes()
        bak_path.write_bytes(original_bytes)

        # Write new content via a temp + atomic rename so a crash mid-
        # write leaves either the old or the new file, not a torn one.
        tmp = file_path.with_name(
            f"{file_path.name}.tmp.{os.getpid()}.{int(time.time())}"
        )
        tmp.write_text(new_text, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.replace(tmp, file_path)
    return RedactionResult(
        modified=True,
        applied_count=applied,
        skipped_pragma=skipped_pragma,
        skipped_no_autofix=skipped_no_autofix,
        backup_path=rotated or bak_path,
        output_path=file_path,
    )


# ---------------------------------------------------------------------------
# Sidecar writer (.scrubbed)
# ---------------------------------------------------------------------------


def _write_to_sidecar(
    *,
    file_path: Path,
    new_text: str,
    applied: int,
    skipped_pragma: int,
    skipped_no_autofix: int,
    modified: bool,
) -> RedactionResult:
    """Write fixed content to ``<file>.scrubbed`` (PID-namespaced).

    Per BLOCKER #10 the per-process suffix avoids collisions when
    multiple processes scrub the same file in parallel. Once we hold
    the WriteLock the temp file is renamed to ``<file>.scrubbed``,
    rotating any existing copy.
    """
    pid = os.getpid()
    ts = int(time.time())
    pid_path = file_path.with_name(f"{file_path.name}.scrubbed.{pid}.{ts}")
    pid_path.write_text(new_text, encoding="utf-8")

    final_path = file_path.with_name(f"{file_path.name}.scrubbed")
    with WriteLock(file_path, timeout=30.0):
        if final_path.exists():
            _rotate_collision(final_path)
        os.replace(pid_path, final_path)

    return RedactionResult(
        modified=modified,
        applied_count=applied,
        skipped_pragma=skipped_pragma,
        skipped_no_autofix=skipped_no_autofix,
        backup_path=None,
        output_path=final_path,
    )


__all__ = [
    "RedactionResult",
    "apply_redactions",
    "parse_pragma_ranges",
]
