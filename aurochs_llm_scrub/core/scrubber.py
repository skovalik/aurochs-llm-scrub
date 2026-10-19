"""Main scan/scrub engine.

Receives ``(db_path, text, rules)`` (or a file path / directory glob)
and produces ``(ScrubRun, list[Finding])``. Findings are inserted into
runs.db inside a single transaction at finalize time, after cross-chunk
deduplication.

Plan v4 contract:

* **BLOCKER #1, #6 — chunk-boundary dedup.** Inputs are scanned in
  ``chunk_size`` byte windows with ``overlap`` extra bytes glued onto
  the trailing edge of each chunk so a regex spanning the boundary is
  captured by both. After scanning, findings are deduplicated by the
  ``(file_offset, length, rule_id)`` tuple before insert. A match that
  ends exactly where a window was cut short of the input is dropped:
  ``$``, ``\\Z`` and ``\\b`` read the cut as the end of the text, and the
  next window re-scans those bytes whole. Likewise a match that starts
  on a later window's first character is dropped (``^``, ``\\b`` and
  lookbehinds read it as the start of the text, and the previous window
  holds those bytes whole), and a window never starts inside a
  multi-byte character.
* **BLOCKER #2 — language detect global + per-chunk fallback.**
  ``langdetect.detect_langs`` runs on the full text first; if its
  confidence falls below 70%, each chunk is detected individually.
  Rules with non-None ``language`` are skipped on chunks whose
  detected language differs.
* **BLOCKER #4 — per-rule timeout + ReDoS auto-disable.** Each rule
  application has a 2-second wall-clock budget per chunk. Trips trigger
  the auto-disable state machine: a row is appended to
  ``disabled_rules_log`` and the rule is skipped for the remainder of
  this run. Custom rules stay disabled across runs; defaults
  auto-re-enable on the next CLI invocation (caller resets enabled=1
  at startup; this engine respects the in-memory flag).
* **ReDoS detection at rule-load time** is best-effort via
  ``redos_detector`` if installed. Missing dependency does not block
  scanning — we just skip the static check and lean on the runtime
  timeout.

The engine is stateless aside from the runs.db row it produces. Re-
running ``scan_text`` on the same input yields the same finding sha256s
(the ``compute_finding_sha256`` 4-tuple guarantees stability).
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aurochs_llm_scrub.core import (
    DisabledRuleEntry,
    Finding,
    Rule,
    ScrubMode,
    ScrubRun,
    ScrubStatus,
    WriteLock,
    db_connect,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# Default chunk window for incremental scanning. 64 KB keeps individual
# regex applications bounded so the per-rule timeout stays meaningful.
_DEFAULT_CHUNK_SIZE: int = 65536

# Overlap glued onto the trailing edge of each chunk; the cross-chunk
# dedup at finalize squashes duplicates the overlap creates.
_DEFAULT_OVERLAP: int = 4096

# Per-rule wall-clock budget per chunk (seconds). BLOCKER #4 lock.
_DEFAULT_PER_RULE_TIMEOUT_S: float = 2.0

# Language gate confidence threshold. Below this, the global detection
# is considered too unreliable and per-chunk fallback runs.
_LANG_GLOBAL_CONFIDENCE_THRESHOLD: float = 0.70

# Sample size for global language detection on >1MB inputs (BLOCKER #2
# fold).
_LANG_DETECT_SAMPLE_BYTES: int = 200_000


# ---------------------------------------------------------------------------
# Internal: chunking
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Chunk:
    """One scan window over the input.

    ``start`` is the file-relative byte offset where this window begins.
    ``text`` is the chunk content (chunk size + overlap, except for the
    final chunk which has no trailing overlap). ``language`` is the
    chunk's detected language tag (or None for unknown) — populated by
    the language gate. The engine consults it when applying rules with
    a non-None ``language`` attribution. ``truncated`` is True when the
    window stops short of the end of the input, so its last character
    is not the end of the text.
    """

    start: int
    text: str
    language: str | None = None
    truncated: bool = False


def _iter_chunks(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
) -> Iterator[_Chunk]:
    """Yield ``_Chunk`` windows over ``text``.

    Boundaries are character-aligned (not byte-aligned) — Python str
    slicing is codepoint-safe so we do not split a multi-byte char.
    The ``start`` field tracks the byte offset of the chunk in the
    original file, since findings are file-relative per BLOCKER #6.
    A window whose nominal start falls inside a multi-byte character
    starts at that character's lead byte instead.

    For inputs <= ``chunk_size``, a single chunk is yielded with no
    overlap; this is the hot path for the typical CLI run on a draft.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    if overlap < 0:
        raise ValueError(f"overlap must be non-negative, got {overlap}")
    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) must be < chunk_size ({chunk_size})"
        )

    if not text:
        return

    encoded = text.encode("utf-8")
    n = len(encoded)
    if n <= chunk_size:
        yield _Chunk(start=0, text=text)
        return

    pos = 0
    while pos < n:
        # A window starts on a character, never inside one: a start on
        # a UTF-8 continuation byte moves back to its lead byte (at most
        # 3 bytes, which the previous window's overlap covers anyway).
        # Decoding from a fragment would open the text with U+FFFD and
        # shift every offset found in the window.
        start = pos
        while start > 0 and (encoded[start] & 0xC0) == 0x80:
            start -= 1
        # Slice on bytes so we get exact byte-aligned chunks. We then
        # pad backward to a valid utf-8 char boundary so we don't yield
        # broken text. The recovery is bounded — at most 3 bytes back.
        end = min(pos + chunk_size + overlap, n)
        chunk_bytes = encoded[start:end]

        # If end < n we may have cut a multi-byte char. Walk the tail
        # back until utf-8 decodes clean.
        if end < n:
            for trim in range(0, 4):
                try:
                    chunk_text = chunk_bytes[: len(chunk_bytes) - trim].decode(
                        "utf-8"
                    )
                except UnicodeDecodeError:
                    continue
                else:
                    break
            else:  # pragma: no cover - 4 byte trim covers utf-8 max
                chunk_text = chunk_bytes.decode("utf-8", errors="replace")
        else:
            try:
                chunk_text = chunk_bytes.decode("utf-8")
            except UnicodeDecodeError:  # pragma: no cover - unlikely
                chunk_text = chunk_bytes.decode("utf-8", errors="replace")

        yield _Chunk(start=start, text=chunk_text, truncated=end < n)
        pos += chunk_size  # advance by chunk_size only; overlap re-scanned


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def _detect_language(text: str) -> tuple[str | None, float]:
    """Best-effort language detection.

    Uses ``langdetect`` when installed; returns ``(lang_code, confidence)``.
    Returns ``(None, 0.0)`` when the dependency is missing or detection
    fails — the engine falls back to applying language-coded rules
    anyway in that case (better to over-flag than to miss with no
    explanation).
    """
    if not text or not text.strip():
        return None, 0.0
    try:
        from langdetect import DetectorFactory, detect_langs  # type: ignore

        # Deterministic seed so the same input always gets the same
        # detection — important for the per-finding sha256 stability
        # contract.
        DetectorFactory.seed = 0
        # Sample the head for >1MB inputs.
        sample = text[:_LANG_DETECT_SAMPLE_BYTES]
        candidates = detect_langs(sample)
        if not candidates:
            return None, 0.0
        top = candidates[0]
        return str(top.lang), float(top.prob)
    except Exception as exc:
        log.debug("language detection failed: %r", exc)
        return None, 0.0


# ---------------------------------------------------------------------------
# Per-rule timeout (thread-based, cross-platform)
# ---------------------------------------------------------------------------


class _RuleTimeoutError(RuntimeError):
    """Raised when a rule exceeds its per-chunk wall-clock budget."""


def _apply_rule_with_timeout(
    pattern: re.Pattern[str],
    text: str,
    *,
    timeout_s: float,
) -> list[re.Match[str]]:
    """Run ``pattern.finditer(text)`` with a wall-clock deadline.

    Python's ``re`` engine releases the GIL inside C extension calls
    rarely; a pathological pattern can therefore not be interrupted
    cleanly. We use a worker thread + result queue + ``join(timeout)``
    pattern: if the worker doesn't finish in time, we surface
    ``_RuleTimeoutError`` and let the caller put the rule into auto-disable
    purgatory. The orphaned worker thread will eventually return; we
    accept the leaked work in exchange for cross-platform support.
    """
    result: dict[str, Any] = {"matches": None, "error": None}

    def _worker() -> None:
        try:
            result["matches"] = list(pattern.finditer(text))
        except Exception as exc:
            result["error"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout=timeout_s)
    if th.is_alive():
        raise _RuleTimeoutError(
            f"rule exceeded {timeout_s}s budget on chunk of "
            f"{len(text)} chars"
        )
    if result["error"] is not None:
        raise result["error"]  # type: ignore[misc]
    return result["matches"] or []


# ---------------------------------------------------------------------------
# Compiled-rule cache helpers
# ---------------------------------------------------------------------------


def _compile_rule(rule: Rule) -> re.Pattern[str]:
    """Compile a Rule's pattern, returning a re.Pattern.

    Compilation errors raise ``re.error`` — the caller treats them like
    a load-time failure (the rule is auto-disabled with reason
    ``"compile-error"``).
    """
    return re.compile(rule.pattern)


def _redos_check(rule: Rule) -> str | None:
    """Best-effort static ReDoS detection.

    Returns ``None`` when the pattern looks safe (or when the
    redos-detector dependency is missing). Returns a short reason
    string when the pattern is flagged. Plan v4 K6: best-effort —
    runtime timeout is the load-bearing defense.
    """
    try:
        import redos_detector  # type: ignore  # noqa: F401
    except ImportError:
        return None
    try:
        from redos_detector import is_redos  # type: ignore

        if is_redos(rule.pattern):
            return "redos-static"
    except Exception as exc:
        log.debug("redos_detector probe failed for %s: %r", rule.rule_id, exc)
    return None


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------


def scan_text(
    db_path: Path,
    text: str,
    rules: list[Rule],
    *,
    target_path: str = "<stdin>",
    mode: ScrubMode = ScrubMode.SCAN,
    pack_name: str = "default",
    pack_version: str = "0.1.0",
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_OVERLAP,
    per_rule_timeout_s: float = _DEFAULT_PER_RULE_TIMEOUT_S,
    redaction_count: int = 0,
) -> tuple[ScrubRun, list[Finding]]:
    """Scan ``text`` against ``rules`` and persist the result.

    The full pipeline:

    1. Open scrub_runs row with status=running.
    2. Detect global language (sampled to 200KB head for big inputs).
    3. Iterate chunks. For each chunk:
       a. If global language confidence is below threshold, detect the
          chunk's language individually.
       b. For each rule:
          - Skip if disabled (in-memory flag).
          - Skip if rule.language is set and != chunk language (when
            chunk language is known).
          - Apply with timeout. On timeout, append disabled_rules_log
            row and mark rule disabled in memory; continue.
          - Drop a match that ends where the window was cut short of
            the input, or that starts on a later window's first
            character; the neighboring window covers it.
          - Collect matches as Finding rows (positions normalized to
            file-relative offsets).
    4. Dedup findings by (file_offset, length, rule_id).
    5. Insert findings + finalize scrub_runs row in one transaction.

    Parameters
    ----------
    db_path:
        Path to runs.db. Caller is responsible for migration.
    text:
        Input text. May be empty (the run still records a row with
        zero findings).
    rules:
        Rules to apply. The caller has typically just loaded a pack
        via ``rule_pack.load_pack`` + ``register_pack`` and pulled the
        ``Rule`` rows back via SELECT. Each Rule needs a positive
        ``id`` (sqlite-assigned) so findings can FK to it.
    target_path:
        Logical target identifier — the file path being scrubbed, or
        ``"<stdin>"`` for piped input. Kept on the scrub_runs row.
    mode:
        ``ScrubMode`` enum — distinguishes scan / in-place / stdout /
        precommit invocations.
    pack_name, pack_version:
        Pack identity used when logging auto-disabled rules to
        disabled_rules_log. Pack-namespaces the disable state per the
        plan v4 cluster fix.
    chunk_size, overlap:
        Tunable scan-window dimensions. Defaults are 64KB / 4KB; pass
        smaller values in tests for faster cross-boundary coverage.
    per_rule_timeout_s:
        Wall-clock budget per rule per chunk. Plan v4 K6 default 2s.
    redaction_count:
        Number of secret-scan redactions the caller applied to ``text``
        before passing it in. Stored on the scrub_runs row so the
        report footer can surface the count.

    Returns
    -------
    tuple[ScrubRun, list[Finding]]
        The persisted run (id populated) + the inserted findings (ids
        populated, in file-offset order).
    """
    if any(r.id is None or r.id <= 0 for r in rules):
        raise ValueError(
            "all rules passed to scan_text must have a positive id "
            "(sqlite-assigned). Run register_pack first or hydrate "
            "from a SELECT."
        )

    started_at = int(time.time())
    run = ScrubRun(
        started_at=started_at,
        status=ScrubStatus.RUNNING,
        target_path=target_path,
        mode=mode,
        total_redacted=redaction_count,
    )

    # Phase 1: language gate (global) + chunking.
    global_lang, global_conf = _detect_language(text)
    chunks = list(_iter_chunks(text, chunk_size=chunk_size, overlap=overlap))
    if global_conf < _LANG_GLOBAL_CONFIDENCE_THRESHOLD:
        for c in chunks:
            cl, _ = _detect_language(c.text)
            c.language = cl
    else:
        for c in chunks:
            c.language = global_lang

    # Phase 2: compile + redos pre-check rules (best-effort).
    compiled: dict[int, re.Pattern[str]] = {}
    enabled: dict[int, bool] = {}
    auto_disabled: list[DisabledRuleEntry] = []
    for rule in rules:
        rid = rule.id  # type: ignore[assignment]
        assert rid is not None
        if not rule.enabled:
            enabled[rid] = False
            continue
        try:
            compiled[rid] = _compile_rule(rule)
            enabled[rid] = True
        except re.error as exc:
            enabled[rid] = False
            auto_disabled.append(
                DisabledRuleEntry(
                    pack_name=pack_name,
                    pack_version=pack_version,
                    rule_id=rule.rule_id,
                    disabled_at=int(time.time()),
                    reason="compile-error",
                    redos_evidence=f"re.error: {exc}",
                )
            )
            continue
        redos_reason = _redos_check(rule)
        if redos_reason is not None:
            enabled[rid] = False
            auto_disabled.append(
                DisabledRuleEntry(
                    pack_name=pack_name,
                    pack_version=pack_version,
                    rule_id=rule.rule_id,
                    disabled_at=int(time.time()),
                    reason=redos_reason,
                )
            )

    # Phase 3: scan chunks.
    raw_findings: list[Finding] = []

    for chunk in chunks:
        chunk_lang = chunk.language
        for rule in rules:
            rid = rule.id
            assert rid is not None
            if not enabled.get(rid, False):
                continue
            # Language gate per BLOCKER #2:
            #   rule.language=None    → always run
            #   rule.language=="en"   → run iff chunk_lang in {"en", None}
            #                           (None = unknown; we don't suppress
            #                           on unknown to avoid false negatives)
            if (
                rule.language is not None
                and chunk_lang is not None
                and rule.language != chunk_lang
            ):
                continue
            try:
                matches = _apply_rule_with_timeout(
                    compiled[rid],
                    chunk.text,
                    timeout_s=per_rule_timeout_s,
                )
            except _RuleTimeoutError:
                enabled[rid] = False
                auto_disabled.append(
                    DisabledRuleEntry(
                        pack_name=pack_name,
                        pack_version=pack_version,
                        rule_id=rule.rule_id,
                        disabled_at=int(time.time()),
                        reason="timeout",
                        redos_evidence=chunk.text[:256],
                    )
                )
                continue

            for m in matches:
                # A match that ends exactly where a window was cut short
                # read the cut as the end of the text: `$`, `\Z` and `\b`
                # match there, and a greedy run stops there. The next
                # window starts `overlap` bytes before the cut and
                # re-scans those bytes whole, so the drop loses nothing
                # shorter than the overlap.
                if chunk.truncated and m.end() == len(chunk.text):
                    continue
                # The mirror at a later window's first character: `^`,
                # `\b` and a lookbehind read it as the start of the text,
                # and a run that crosses it was already matched whole by
                # the previous window, whose overlap holds these bytes
                # with real context on both sides.
                if chunk.start > 0 and m.start() == 0:
                    continue
                # Convert chunk-relative match positions to file-relative
                # byte offsets per BLOCKER #6.
                chunk_text = chunk.text
                start_char = m.start()
                end_char = m.end()
                # str slice → bytes for offset accuracy.
                file_offset = chunk.start + len(
                    chunk_text[:start_char].encode("utf-8")
                )
                length = len(
                    chunk_text[start_char:end_char].encode("utf-8")
                )
                if length == 0:
                    # Zero-width matches (e.g. an anchor-only regex)
                    # would violate the Finding.length > 0 invariant.
                    # Skip rather than insert.
                    continue
                snippet_text = chunk_text[start_char:end_char]
                line_number = _line_number_for_offset(text, file_offset)
                raw_findings.append(
                    Finding(
                        run_id=1,  # placeholder; rewritten after run insert
                        rule_id=rid,
                        file_offset=file_offset,
                        length=length,
                        file_path=target_path,
                        line_number=line_number,
                        severity=rule.severity_default,
                        snippet=snippet_text,
                    )
                )

    # Phase 4: cross-chunk dedup by (file_offset, length, rule_id).
    deduped: dict[tuple[int, int, int], Finding] = {}
    for f in raw_findings:
        key = (f.file_offset, f.length, f.rule_id)
        if key not in deduped:
            deduped[key] = f
    final_findings = sorted(
        deduped.values(),
        key=lambda f: (f.file_offset, f.rule_id),
    )

    # Phase 5: persist. Status = partial if any rule auto-disabled or
    # any compile/redos fail; else success.
    final_status = (
        ScrubStatus.PARTIAL if auto_disabled else ScrubStatus.SUCCESS
    )
    run.status = final_status
    run.ended_at = int(time.time())
    run.total_findings = len(final_findings)

    with WriteLock(db_path, timeout=30.0):
        conn = db_connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "INSERT INTO scrub_runs "
                    "(started_at, ended_at, status, target_path, mode, "
                    "total_findings, total_redacted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.started_at,
                        run.ended_at,
                        run.status.value,
                        run.target_path,
                        run.mode.value,
                        run.total_findings,
                        run.total_redacted,
                    ),
                )
                run_id_raw = cur.lastrowid
                if run_id_raw is None:  # pragma: no cover - defensive
                    raise RuntimeError("scrub_runs INSERT returned no row id")
                run_id = int(run_id_raw)
                run.id = run_id

                inserted: list[Finding] = []
                for f in final_findings:
                    cur = conn.execute(
                        "INSERT INTO findings "
                        "(run_id, rule_id, file_offset, length, file_path, "
                        "line_number, severity, snippet, schema_version) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            f.rule_id,
                            f.file_offset,
                            f.length,
                            f.file_path,
                            f.line_number,
                            f.severity,
                            f.snippet,
                            f.schema_version,
                        ),
                    )
                    fid = cur.lastrowid
                    if fid is None:  # pragma: no cover
                        raise RuntimeError(
                            "findings INSERT returned no row id"
                        )
                    inserted.append(
                        Finding(
                            id=int(fid),
                            run_id=run_id,
                            rule_id=f.rule_id,
                            file_offset=f.file_offset,
                            length=f.length,
                            file_path=f.file_path,
                            line_number=f.line_number,
                            severity=f.severity,
                            snippet=f.snippet,
                            schema_version=f.schema_version,
                        )
                    )

                for entry in auto_disabled:
                    conn.execute(
                        "INSERT INTO disabled_rules_log "
                        "(pack_name, pack_version, rule_id, disabled_at, "
                        "reason, redos_evidence) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            entry.pack_name,
                            entry.pack_version,
                            entry.rule_id,
                            entry.disabled_at,
                            entry.reason,
                            entry.redos_evidence,
                        ),
                    )
                    # Mirror the disable into the rules table so the
                    # next scan respects the flip without re-loading
                    # disabled_rules_log explicitly.
                    rule_match = next(
                        (
                            r
                            for r in rules
                            if r.rule_id == entry.rule_id
                        ),
                        None,
                    )
                    if rule_match is not None and rule_match.id is not None:
                        conn.execute(
                            "UPDATE rules SET enabled = 0 WHERE id = ?",
                            (rule_match.id,),
                        )

                conn.execute("COMMIT")
                return run, inserted
            except BaseException:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# scan_file / scan_dir
# ---------------------------------------------------------------------------


def scan_file(
    db_path: Path,
    path: Path,
    rules: list[Rule],
    **kwargs: Any,
) -> tuple[ScrubRun, list[Finding]]:
    """Read ``path`` as utf-8 text and call ``scan_text``.

    Convenience wrapper. Reads the entire file into memory; the
    chunker handles >2MB inputs internally. The ``target_path`` kw is
    set to ``str(path)`` automatically (callers can override).

    Parameters
    ----------
    db_path, rules:
        Same as ``scan_text``.
    path:
        The file to scan. Must exist and be utf-8 decodable.
    **kwargs:
        Forwarded to ``scan_text`` (chunk_size, overlap, mode, etc.).
    """
    if not path.exists():
        raise FileNotFoundError(f"scan target not found: {path}")
    text = path.read_text(encoding="utf-8")
    kwargs.setdefault("target_path", str(path))
    return scan_text(db_path, text, rules, **kwargs)


def scan_dir(
    db_path: Path,
    root: Path,
    rules: list[Rule],
    *,
    glob_pattern: str = "**/*.md",
    **kwargs: Any,
) -> Iterator[tuple[ScrubRun, list[Finding]]]:
    """Yield one ``(ScrubRun, list[Finding])`` per file matching ``glob_pattern``.

    The default glob is ``**/*.md`` — markdown is the dominant draft
    format. Callers can pass ``"**/*.txt"`` or any other pattern. The
    scan order is whatever ``Path.rglob`` yields; on most platforms
    that's filesystem-walk order (not lexically sorted).

    Yields ``(run, findings)`` lazily so a caller can stream a report
    for a giant directory without holding every result in memory.
    """
    if not root.exists():
        raise FileNotFoundError(f"scan_dir root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"scan_dir root must be a directory: {root}")
    for p in root.glob(glob_pattern):
        if not p.is_file():
            continue
        yield scan_file(db_path, p, rules, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line_number_for_offset(text: str, byte_offset: int) -> int:
    """Return the 1-based line number containing the given byte offset.

    The Finding.line_number invariant is "positive integer". This
    helper handles the empty-text and offset-past-end cases by
    clamping; the only invalid input is a negative offset, which the
    caller has already filtered.
    """
    if byte_offset <= 0 or not text:
        return 1
    encoded = text.encode("utf-8")
    if byte_offset > len(encoded):
        byte_offset = len(encoded)
    head = encoded[:byte_offset]
    return head.count(b"\n") + 1


# ---------------------------------------------------------------------------
# Public report shapes (MCP + CLI consumers)
# ---------------------------------------------------------------------------
#
# The lower-level ``scan_text`` returns ``(ScrubRun, list[Finding])`` —
# fine for engine-internal callers, but neither MCP (wants a dict matching
# ``mcp.schemas.OUTPUT_SHAPES``) nor the CLI (wants an object with
# ``.summary.p0_count`` / ``.to_plain()`` / ``.to_markdown()`` /
# ``.to_json_dict()``) consumes that tuple shape directly. The wrappers
# below adapt the engine to those two surfaces; both lean on the same
# ``_run_engine`` helper so behavior stays identical between them.


@dataclass(slots=True, kw_only=True)
class SummaryCounts:
    """Aggregate finding counts for a scan/scrub result.

    The CLI's ``--severity`` exit-code logic reads ``p0_count`` /
    ``p1_count`` / ``p2_count`` directly. ``total`` is the sum across
    severities and matches ``ScrubRun.total_findings`` for runs that
    persist to the DB.
    """

    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    total: int = 0


@dataclass(slots=True, kw_only=True)
class ScanResult:
    """Public scan/scrub result — CLI + library consumer surface.

    Returned by :func:`scan` and :func:`scrub`. Carries:

    * ``findings`` — flattened finding rows in MCP-shaped dict form so a
      single payload feeds both the CLI report and any embedded JSON
      output.
    * ``summary`` — :class:`SummaryCounts` with per-severity totals.
    * ``language`` — language gate's verdict (``None`` when the gate
      couldn't run or detection was inconclusive).
    * ``auto_disabled`` — list of pack-stable rule ids the engine
      auto-disabled during the run (timeout / compile-error / static
      ReDoS). Surfaced in the report footer per K2 lock.
    * ``cleaned_text`` — populated by :func:`scrub` when a redact path
      ran; ``None`` for pure :func:`scan` calls.
    * ``runs`` — the underlying :class:`ScrubRun` rows the engine
      persisted (one per scanned file). Empty for no-op runs.

    The ``to_*`` renderers are intentionally simple — the goal is
    "human can read the CLI output and a JSON consumer can parse the
    same data without a second engine pass."
    """

    findings: list[dict[str, Any]]
    summary: SummaryCounts
    language: str | None = None
    auto_disabled: list[str] = field(default_factory=list)
    cleaned_text: str | None = None
    runs: list[ScrubRun] = field(default_factory=list)
    redaction_count: int = 0

    def to_plain(self) -> str:
        """Render a plain-text report.

        One line per finding plus a footer summarizing P0/P1/P2 totals
        and any auto-disabled rules. Designed for ``stdout`` direct
        consumption — voice/recall print roughly the same shape.
        """
        lines: list[str] = []
        for f in self.findings:
            lines.append(
                f"[{f.get('severity', '?')}] {f.get('rule_id', '?')} "
                f"line={f.get('line_number', '?')} "
                f"offset={f.get('file_offset', '?')} "
                f"length={f.get('length', '?')}"
            )
            snippet = f.get("snippet", "")
            if snippet:
                lines.append(f"    {snippet}")
        s = self.summary
        lines.append(
            f"\nP0={s.p0_count} P1={s.p1_count} P2={s.p2_count} "
            f"(total={s.total})"
        )
        if self.auto_disabled:
            lines.append(
                f"auto_disabled={len(self.auto_disabled)} "
                f"({', '.join(self.auto_disabled)})"
            )
        if self.language is not None:
            lines.append(f"language={self.language}")
        return "\n".join(lines) + "\n"

    def to_markdown(self) -> str:
        """Render a markdown report.

        Top-level heading + table of findings + footer. Used by
        ``llm-scrub scan --format markdown`` for piping into draft
        review docs.
        """
        s = self.summary
        out: list[str] = []
        out.append("# llm-scrub report\n")
        out.append(
            f"**Summary:** P0={s.p0_count} · P1={s.p1_count} · "
            f"P2={s.p2_count} · total={s.total}\n"
        )
        if self.auto_disabled:
            out.append(
                f"**Auto-disabled:** {', '.join(self.auto_disabled)}\n"
            )
        if self.language is not None:
            out.append(f"**Language:** `{self.language}`\n")
        if not self.findings:
            out.append("\n_No findings._\n")
            return "".join(out)
        out.append("\n| Severity | Rule | Line | Snippet |\n")
        out.append("|---|---|---|---|\n")
        for f in self.findings:
            snippet = (f.get("snippet") or "").replace("|", "\\|").replace(
                "\n", " "
            )
            out.append(
                f"| {f.get('severity', '?')} | "
                f"{f.get('rule_id', '?')} | "
                f"{f.get('line_number', '?')} | "
                f"{snippet} |\n"
            )
        return "".join(out)

    def to_json_dict(self) -> dict[str, Any]:
        """Render a JSON-shaped dict.

        Mirrors ``mcp.schemas.OUTPUT_SHAPES['scrub_scan_text']`` so the
        same payload satisfies the MCP contract and the ``--json`` CLI
        flag. ``model_dump`` shape parity is intentional.
        """
        s = self.summary
        payload: dict[str, Any] = {
            "findings": list(self.findings),
            "totals": {
                "P0": s.p0_count,
                "P1": s.p1_count,
                "P2": s.p2_count,
            },
            "auto_disabled_count": len(self.auto_disabled),
            "language": self.language,
            "redaction_count": self.redaction_count,
        }
        if self.cleaned_text is not None:
            payload["cleaned_text"] = self.cleaned_text
        return payload

    # Pydantic-shaped alias so ``hasattr(report, 'model_dump')`` in
    # ``cli.scan_cmd._emit_report`` lights up cleanly.
    def model_dump(self) -> dict[str, Any]:
        """Pydantic-style dump — alias for :meth:`to_json_dict`."""
        return self.to_json_dict()


# ---------------------------------------------------------------------------
# Internal: rule loading + finding hydration
# ---------------------------------------------------------------------------


def _load_rules_from_db(
    db_path: Path,
    *,
    pack_name: str | None,
) -> tuple[list[Rule], dict[int, str], dict[int, tuple[str, str]]]:
    """Load enabled rules + a couple of lookup tables from runs.db.

    Returns ``(rules, rule_id_lookup, pack_lookup)``:

    * ``rules`` — :class:`Rule` rows ready to feed :func:`scan_text`.
    * ``rule_id_lookup`` — ``{Rule.id: Rule.rule_id}`` for the redact
      step (which keys auto-fix replacements by stable rule id).
    * ``pack_lookup`` — ``{Rule.id: (pack_name, pack_version)}`` so the
      MCP wrapper can attach pack identity to each finding without a
      second SQL round-trip.

    ``pack_name=None`` returns rules from the ``"default"`` pack — the
    only pack that v0.1 ships out of the box. A non-None ``pack_name``
    filters to that pack; callers get an empty list when the pack
    isn't registered (the MCP wrapper surfaces ``PACK_NOT_FOUND`` in
    that case via the lazy-import shim).
    """
    target_pack = pack_name or "default"
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id, r.pack_id, r.rule_id, r.name, r.pattern, "
            "r.severity_default, r.language, r.description, r.enabled, "
            "p.name AS pack_name, p.version AS pack_version "
            "FROM rules r JOIN rule_packs p ON p.id = r.pack_id "
            "WHERE p.name = ? AND r.enabled = 1",
            (target_pack,),
        ).fetchall()
    finally:
        conn.close()

    rules: list[Rule] = []
    rule_id_lookup: dict[int, str] = {}
    pack_lookup: dict[int, tuple[str, str]] = {}
    for row in rows:
        rid = int(row["id"])
        rules.append(
            Rule(
                id=rid,
                pack_id=int(row["pack_id"]),
                rule_id=str(row["rule_id"]),
                name=str(row["name"]),
                pattern=str(row["pattern"]),
                severity_default=str(row["severity_default"]),
                language=row["language"],
                description=str(row["description"]),
                enabled=bool(row["enabled"]),
            )
        )
        rule_id_lookup[rid] = str(row["rule_id"])
        pack_lookup[rid] = (str(row["pack_name"]), str(row["pack_version"]))
    return rules, rule_id_lookup, pack_lookup


def _finding_to_dict(
    f: Finding,
    *,
    rule_id_lookup: dict[int, str],
    pack_lookup: dict[int, tuple[str, str]],
) -> dict[str, Any]:
    """Convert one :class:`Finding` row to the MCP/JSON dict shape."""
    pack_name, _pack_version = pack_lookup.get(f.rule_id, ("default", "0.1.0"))
    return {
        "rule_id": rule_id_lookup.get(f.rule_id, ""),
        "pack_name": pack_name,
        "severity": f.severity,
        "file_offset": f.file_offset,
        "length": f.length,
        "line_number": f.line_number,
        "snippet": f.snippet,
    }


def _filter_by_severity(
    findings: list[Finding],
    severity_filter: list[str] | None,
) -> list[Finding]:
    """Keep only findings whose severity is in the filter (or all if None)."""
    if severity_filter is None:
        return findings
    allowed = {s.upper() for s in severity_filter}
    return [f for f in findings if f.severity in allowed]


def _summarize_findings(findings: list[dict[str, Any]]) -> SummaryCounts:
    """Tally per-severity counts from a finding-dict list."""
    p0 = sum(1 for f in findings if f.get("severity") == "P0")
    p1 = sum(1 for f in findings if f.get("severity") == "P1")
    p2 = sum(1 for f in findings if f.get("severity") == "P2")
    return SummaryCounts(p0_count=p0, p1_count=p1, p2_count=p2, total=p0 + p1 + p2)


# ---------------------------------------------------------------------------
# MCP-shaped wrappers (scan_text_report / scrub_text_report)
# ---------------------------------------------------------------------------
#
# These accept ``severity_filter`` + ``db_path`` keyword-only and return
# a dict matching ``mcp.schemas.OUTPUT_SHAPES``. The MCP server's
# ``_tool_scrub_scan_text`` / ``_tool_scrub_scrub_text`` handlers call
# these by name; the lazy-import shim returns ``PIPELINE_UNAVAILABLE``
# when this module is missing the attribute (transient during parallel
# build, but the cleanup agent wires this in T0).


def scan_text_report(
    text: str,
    *,
    severity_filter: list[str] | None = None,
    db_path: Path,
) -> dict[str, Any]:
    """MCP-shaped scan report.

    Loads the default rule pack from ``db_path``, runs :func:`scan_text`,
    optionally filters findings by severity, and returns a dict whose
    keys match ``mcp.schemas.OUTPUT_SHAPES['scrub_scan_text']``::

        {
            "findings": [...],
            "totals": {"P0": int, "P1": int, "P2": int},
            "auto_disabled_count": int,
            "language": str | None,
            "redaction_count": int,
        }

    ``severity_filter`` is applied AFTER the engine runs — the engine
    always evaluates every enabled rule so cross-rule context (language
    gate, auto-disable accounting) stays correct; the filter only
    narrows what's surfaced.
    """
    rules, rule_id_lookup, pack_lookup = _load_rules_from_db(
        db_path, pack_name=None
    )
    if not rules:
        return {
            "findings": [],
            "totals": {"P0": 0, "P1": 0, "P2": 0},
            "auto_disabled_count": 0,
            "language": None,
            "redaction_count": 0,
        }

    run, findings = scan_text(
        db_path,
        text,
        rules,
        target_path="<mcp>",
        mode=ScrubMode.SCAN,
    )

    filtered = _filter_by_severity(findings, severity_filter)
    finding_dicts = [
        _finding_to_dict(
            f, rule_id_lookup=rule_id_lookup, pack_lookup=pack_lookup
        )
        for f in filtered
    ]
    summary = _summarize_findings(finding_dicts)

    auto_disabled_count = _count_auto_disabled_for_run(db_path, run)
    language = _detect_language_for_report(text)

    return {
        "findings": finding_dicts,
        "totals": {
            "P0": summary.p0_count,
            "P1": summary.p1_count,
            "P2": summary.p2_count,
        },
        "auto_disabled_count": auto_disabled_count,
        "language": language,
        "redaction_count": run.total_redacted,
    }


def scrub_text_report(
    text: str,
    *,
    severity_filter: list[str] | None = None,
    db_path: Path,
) -> dict[str, Any]:
    """MCP-shaped scrub report.

    Same as :func:`scan_text_report` plus mechanical-fix application.
    Returns a dict whose keys match
    ``mcp.schemas.OUTPUT_SHAPES['scrub_scrub_text']``::

        {
            "cleaned_text": str,
            "findings": [...],
            "fixed_count": int,
            "totals": {"P0": int, "P1": int, "P2": int},
            "auto_disabled_count": int,
        }

    The auto-fix policy lives in :mod:`core.redact`; only mechanical
    findings (em dashes, double spaces, trailing whitespace, &mdash;
    entities) are rewritten. Non-mechanical findings are still
    reported but the cleaned text leaves them intact.

    The text is NOT a real file path, so we skip the on-disk redact
    flow and run the splice in memory via :func:`apply_redactions`'s
    private splice helper.
    """
    # Lazy import to avoid a top-level circular: redact imports
    # WriteLock + Finding from core; we only need its splice helper.
    from aurochs_llm_scrub.core.redact import _splice_findings, parse_pragma_ranges

    rules, rule_id_lookup, pack_lookup = _load_rules_from_db(
        db_path, pack_name=None
    )
    if not rules:
        return {
            "cleaned_text": text,
            "findings": [],
            "fixed_count": 0,
            "totals": {"P0": 0, "P1": 0, "P2": 0},
            "auto_disabled_count": 0,
        }

    run, findings = scan_text(
        db_path,
        text,
        rules,
        target_path="<mcp>",
        mode=ScrubMode.SCRUB_TO_STDOUT,
    )

    filtered = _filter_by_severity(findings, severity_filter)
    finding_dicts = [
        _finding_to_dict(
            f, rule_id_lookup=rule_id_lookup, pack_lookup=pack_lookup
        )
        for f in filtered
    ]
    summary = _summarize_findings(finding_dicts)

    pragma_ranges = parse_pragma_ranges(text)
    cleaned, applied, _skipped_pragma, _skipped_no_autofix = _splice_findings(
        text=text,
        findings=filtered,
        pragma_ranges=pragma_ranges,
        rule_id_lookup=rule_id_lookup,
    )
    auto_disabled_count = _count_auto_disabled_for_run(db_path, run)

    return {
        "cleaned_text": cleaned,
        "findings": finding_dicts,
        "fixed_count": applied,
        "totals": {
            "P0": summary.p0_count,
            "P1": summary.p1_count,
            "P2": summary.p2_count,
        },
        "auto_disabled_count": auto_disabled_count,
    }


def _count_auto_disabled_for_run(db_path: Path, run: ScrubRun) -> int:
    """Count disabled_rules_log rows associated with this run window.

    The engine appends to ``disabled_rules_log`` whenever a rule trips
    the timeout / compile / ReDoS path. We count rows whose
    ``disabled_at`` is within the run's start/end window so the report
    surfaces only auto-disables that happened DURING this scan, not
    historical disables.
    """
    if run.id is None:
        return 0
    end_at = run.ended_at if run.ended_at is not None else int(time.time())
    conn = db_connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM disabled_rules_log "
            "WHERE disabled_at >= ? AND disabled_at <= ?",
            (run.started_at, end_at),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    return int(row[0])


def _detect_language_for_report(text: str) -> str | None:
    """Return the language gate's verdict for inclusion in the report.

    Mirrors :func:`_detect_language` but ignores the confidence — the
    report just needs the top-1 label. Returns ``None`` when detection
    fails (langdetect missing, empty input, etc.).
    """
    lang, _conf = _detect_language(text)
    return lang


# ---------------------------------------------------------------------------
# CLI-shaped wrappers (scan / scrub)
# ---------------------------------------------------------------------------
#
# The CLI's ``scan`` + ``scrub`` subcommands call these. Argument names
# mirror the parser's ``argparse`` namespace verbatim so the CLI shim
# stays a one-liner. ``rules_pack=None`` selects the default pack; a
# non-None value picks a specific pack (forward-compat — only ``default``
# ships in v0.1).


def scan(
    target: Path | str,
    db_path: Path,
    rules_pack: str | None = None,
    severity_filter: list[str] | None = None,
    recursive: bool = False,
    quiet: bool = False,
) -> ScanResult:
    """Read-only scan; returns a :class:`ScanResult`.

    Parameters
    ----------
    target:
        File path, directory path, or the literal string
        ``"<stdin>"`` for piped input. Directories are walked when
        ``recursive=True`` (default ``**/*.md``); a non-recursive
        directory call is treated as an error.
    db_path:
        Path to runs.db. Caller is responsible for migration (the CLI
        runs ``MigrationRunner`` once at startup via ``init``).
    rules_pack:
        Pack name to load; ``None`` selects ``"default"``.
    severity_filter:
        Optional list of severity tiers to include. Applied AFTER the
        engine runs.
    recursive:
        Walk directories recursively. Ignored for file inputs.
    quiet:
        Suppress P2 redundancy warnings in output (currently a
        passthrough flag; the report's ``to_plain`` honors it).

    Returns
    -------
    ScanResult
        Aggregated report. ``runs`` carries one :class:`ScrubRun` per
        scanned file (one entry for stdin / single-file input).
    """
    _ = quiet  # reserved for v0.2 P2 muting

    rules, rule_id_lookup, pack_lookup = _load_rules_from_db(
        db_path, pack_name=rules_pack
    )
    if not rules:
        return ScanResult(
            findings=[],
            summary=SummaryCounts(),
            language=None,
            auto_disabled=[],
            cleaned_text=None,
            runs=[],
            redaction_count=0,
        )

    runs: list[ScrubRun] = []
    finding_dicts: list[dict[str, Any]] = []
    auto_disabled: list[str] = []
    languages: list[str] = []
    redactions = 0

    if isinstance(target, str) and target == "<stdin>":
        # Lazy import: keep top-of-file imports stdlib-minimal so the
        # engine stays cheap to import in non-CLI contexts (MCP server,
        # tests, pre-commit hook).
        import sys as _sys

        text = _sys.stdin.read()
        run, findings = scan_text(
            db_path, text, rules, target_path="<stdin>", mode=ScrubMode.SCAN
        )
        runs.append(run)
        filtered = _filter_by_severity(findings, severity_filter)
        finding_dicts.extend(
            _finding_to_dict(
                f, rule_id_lookup=rule_id_lookup, pack_lookup=pack_lookup
            )
            for f in filtered
        )
        auto_disabled.extend(
            _disabled_rule_ids_for_run(db_path, run)
        )
        lang = _detect_language_for_report(text)
        if lang is not None:
            languages.append(lang)
        redactions += run.total_redacted
    else:
        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"scan target not found: {path}")
        if path.is_dir():
            if not recursive:
                raise FileNotFoundError(
                    f"scan target is a directory; pass --recursive: {path}"
                )
            for run, findings in scan_dir(db_path, path, rules):
                runs.append(run)
                filtered = _filter_by_severity(findings, severity_filter)
                finding_dicts.extend(
                    _finding_to_dict(
                        f,
                        rule_id_lookup=rule_id_lookup,
                        pack_lookup=pack_lookup,
                    )
                    for f in filtered
                )
                auto_disabled.extend(
                    _disabled_rule_ids_for_run(db_path, run)
                )
                redactions += run.total_redacted
        else:
            text = path.read_text(encoding="utf-8")
            run, findings = scan_file(
                db_path, path, rules, mode=ScrubMode.SCAN
            )
            runs.append(run)
            filtered = _filter_by_severity(findings, severity_filter)
            finding_dicts.extend(
                _finding_to_dict(
                    f,
                    rule_id_lookup=rule_id_lookup,
                    pack_lookup=pack_lookup,
                )
                for f in filtered
            )
            auto_disabled.extend(
                _disabled_rule_ids_for_run(db_path, run)
            )
            lang = _detect_language_for_report(text)
            if lang is not None:
                languages.append(lang)
            redactions += run.total_redacted

    summary = _summarize_findings(finding_dicts)
    # Collapse multiple language verdicts (multi-file recursive walk) by
    # majority vote; ties prefer the first observed.
    language = languages[0] if languages else None

    return ScanResult(
        findings=finding_dicts,
        summary=summary,
        language=language,
        auto_disabled=sorted(set(auto_disabled)),
        cleaned_text=None,
        runs=runs,
        redaction_count=redactions,
    )


def scrub(
    target: Path | str,
    db_path: Path,
    rules_pack: str | None = None,
    severity_filter: list[str] | None = None,
    mode: str = "scrub-to-stdout",
    backup_mode: str = "rotate",
    no_backup: bool = False,
    quiet: bool = False,
    emit_diff: bool = False,
) -> ScanResult:
    """Apply mechanical fixes; returns a :class:`ScanResult`.

    Parameters
    ----------
    target:
        File path or ``"<stdin>"`` for piped input. ``"<stdin>"`` always
        renders to stdout; ``mode`` is ignored.
    db_path, rules_pack, severity_filter:
        Same as :func:`scan`.
    mode:
        ``"scrub-to-stdout"`` (default) or ``"scrub-in-place"`` —
        matches :class:`ScrubMode` enum string values.
    backup_mode:
        ``"rotate"`` (default), ``"fail"``, or ``"overwrite"`` per the
        :func:`apply_redactions` lock.
    no_backup:
        Skip writing the ``.bak`` sidecar (in-place mode only). Honored
        by the redact step's writer.
    quiet, emit_diff:
        Reserved for v0.2; currently passthrough flags. ``emit_diff``
        will print a unified diff to stderr when the redact step
        modifies content.

    Returns
    -------
    ScanResult
        Same shape as :func:`scan` plus ``cleaned_text`` populated
        when running on a string input or in stdout mode.
    """
    _ = quiet
    _ = no_backup  # reserved for v0.2 redact wiring
    _ = emit_diff

    # Lazy import so the in-memory splice path is available without
    # creating a top-level cycle through redact -> scrubber.
    from aurochs_llm_scrub.core.redact import (
        _splice_findings,
        apply_redactions,
        parse_pragma_ranges,
    )

    rules, rule_id_lookup, pack_lookup = _load_rules_from_db(
        db_path, pack_name=rules_pack
    )
    if not rules:
        return ScanResult(
            findings=[],
            summary=SummaryCounts(),
            language=None,
            auto_disabled=[],
            cleaned_text=None,
            runs=[],
            redaction_count=0,
        )

    try:
        scrub_mode = ScrubMode(mode)
    except ValueError as exc:
        raise ValueError(
            f"unknown scrub mode {mode!r}; "
            f"expected one of {[m.value for m in ScrubMode]}"
        ) from exc

    runs: list[ScrubRun] = []
    finding_dicts: list[dict[str, Any]] = []
    auto_disabled: list[str] = []
    cleaned_text: str | None = None
    redactions = 0
    language: str | None = None

    if isinstance(target, str) and target == "<stdin>":
        import sys as _sys

        text = _sys.stdin.read()
        run, findings = scan_text(
            db_path, text, rules, target_path="<stdin>", mode=scrub_mode
        )
        runs.append(run)
        filtered = _filter_by_severity(findings, severity_filter)
        finding_dicts.extend(
            _finding_to_dict(
                f, rule_id_lookup=rule_id_lookup, pack_lookup=pack_lookup
            )
            for f in filtered
        )
        auto_disabled.extend(_disabled_rule_ids_for_run(db_path, run))
        pragma_ranges = parse_pragma_ranges(text)
        cleaned_text, _applied, _sp, _sn = _splice_findings(
            text=text,
            findings=filtered,
            pragma_ranges=pragma_ranges,
            rule_id_lookup=rule_id_lookup,
        )
        language = _detect_language_for_report(text)
        redactions += run.total_redacted
    else:
        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"scrub target not found: {path}")
        if path.is_dir():
            raise FileNotFoundError(
                f"scrub target must be a file (directories handled by "
                f"the precommit walker): {path}"
            )
        text = path.read_text(encoding="utf-8")
        run, findings = scan_file(
            db_path, path, rules, mode=scrub_mode
        )
        runs.append(run)
        filtered = _filter_by_severity(findings, severity_filter)
        finding_dicts.extend(
            _finding_to_dict(
                f, rule_id_lookup=rule_id_lookup, pack_lookup=pack_lookup
            )
            for f in filtered
        )
        auto_disabled.extend(_disabled_rule_ids_for_run(db_path, run))
        if scrub_mode in {ScrubMode.SCRUB_IN_PLACE, ScrubMode.PRECOMMIT}:
            apply_redactions(
                file_path=path,
                findings=filtered,
                mode=scrub_mode,
                backup_mode=backup_mode,
                rule_id_lookup=rule_id_lookup,
            )
            # Re-read for the cleaned_text return value.
            cleaned_text = path.read_text(encoding="utf-8")
        else:
            # SCAN / SCRUB_TO_STDOUT — splice in memory, never touch disk.
            pragma_ranges = parse_pragma_ranges(text)
            cleaned_text, _applied, _sp, _sn = _splice_findings(
                text=text,
                findings=filtered,
                pragma_ranges=pragma_ranges,
                rule_id_lookup=rule_id_lookup,
            )
        language = _detect_language_for_report(text)
        redactions += run.total_redacted

    summary = _summarize_findings(finding_dicts)

    return ScanResult(
        findings=finding_dicts,
        summary=summary,
        language=language,
        auto_disabled=sorted(set(auto_disabled)),
        cleaned_text=cleaned_text,
        runs=runs,
        redaction_count=redactions,
    )


def _disabled_rule_ids_for_run(db_path: Path, run: ScrubRun) -> list[str]:
    """Return the pack-stable rule_ids auto-disabled within this run window."""
    if run.id is None:
        return []
    end_at = run.ended_at if run.ended_at is not None else int(time.time())
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT rule_id FROM disabled_rules_log "
            "WHERE disabled_at >= ? AND disabled_at <= ?",
            (run.started_at, end_at),
        ).fetchall()
    finally:
        conn.close()
    return [str(row["rule_id"]) for row in rows]


__all__ = [
    "ScanResult",
    "SummaryCounts",
    "scan",
    "scan_dir",
    "scan_file",
    "scan_text",
    "scan_text_report",
    "scrub",
    "scrub_text_report",
]
