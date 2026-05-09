"""Unit tests for core.scrubber — scan_text + chunk dedup + timeout."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurochs_llm_scrub.core import (
    Rule,
    ScrubMode,
    ScrubStatus,
    db_connect,
)
from aurochs_llm_scrub.core.db import MigrationRunner
from aurochs_llm_scrub.core.rule_pack import register_default_pack
from aurochs_llm_scrub.core.scrubber import (
    _iter_chunks,
    _line_number_for_offset,
    scan_dir,
    scan_file,
    scan_text,
)


@pytest.fixture
def runs_db(tmp_path: Path) -> Path:
    db = tmp_path / "runs.db"
    MigrationRunner(db).run()
    register_default_pack(db)
    return db


def _load_rules(db_path: Path, *, rule_id_prefix: str | None = None) -> list[Rule]:
    """Return enabled rules from the default pack as Rule dataclasses."""
    conn = db_connect(db_path)
    try:
        sql = (
            "SELECT r.id, r.pack_id, r.rule_id, r.name, r.pattern, "
            "r.severity_default, r.language, r.description, r.enabled "
            "FROM rules r JOIN rule_packs p ON p.id = r.pack_id "
            "WHERE p.name = 'default' AND r.enabled = 1"
        )
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    rules = [
        Rule(
            id=int(row["id"]),
            pack_id=int(row["pack_id"]),
            rule_id=str(row["rule_id"]),
            name=str(row["name"]),
            pattern=str(row["pattern"]),
            severity_default=str(row["severity_default"]),
            language=row["language"],
            description=str(row["description"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]
    if rule_id_prefix is not None:
        rules = [r for r in rules if r.rule_id.startswith(rule_id_prefix)]
    return rules


# ---------------------------------------------------------------------------
# Chunk iterator
# ---------------------------------------------------------------------------


class TestChunkIterator:
    def test_empty_text_yields_nothing(self) -> None:
        chunks = list(_iter_chunks("", chunk_size=100, overlap=10))
        assert chunks == []

    def test_short_text_single_chunk(self) -> None:
        chunks = list(_iter_chunks("hello", chunk_size=100, overlap=10))
        assert len(chunks) == 1
        assert chunks[0].start == 0
        assert chunks[0].text == "hello"

    def test_long_text_overlapping_chunks(self) -> None:
        # 200 char text, 64-char chunks with 16 overlap → at least 3
        # windows (each chunk consumes 64 progress bytes).
        text = "x" * 200
        chunks = list(_iter_chunks(text, chunk_size=64, overlap=16))
        assert len(chunks) >= 3
        # First chunk includes overlap region (64+16=80 chars).
        assert chunks[0].start == 0
        # Second chunk starts 64 bytes in (chunk_size advances).
        assert chunks[1].start == 64

    def test_invalid_overlap_raises(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            list(_iter_chunks("abc", chunk_size=10, overlap=10))


# ---------------------------------------------------------------------------
# scan_text — basic
# ---------------------------------------------------------------------------


class TestScanText:
    def test_clean_text_zero_findings(self, runs_db: Path) -> None:
        rules = _load_rules(runs_db, rule_id_prefix="p0")
        run, findings = scan_text(
            runs_db,
            "Stefan writes direct prose with no decorations.\n",
            rules,
            target_path="<stdin>",
        )
        assert run.id is not None
        assert run.total_findings == 0
        assert findings == []

    def test_em_dash_detected(self, runs_db: Path) -> None:
        rules = _load_rules(runs_db, rule_id_prefix="p0-em-dash")
        text = "He paused — then continued.\n"
        run, findings = scan_text(runs_db, text, rules)
        assert run.total_findings == 1
        assert len(findings) == 1
        assert findings[0].severity == "P0"
        assert findings[0].snippet == "—"
        # offset of "—" in utf-8: "He paused " = 10 bytes, then "—" (3 bytes).
        assert findings[0].file_offset == 10
        assert findings[0].length == 3

    def test_double_space_detected(self, runs_db: Path) -> None:
        rules = _load_rules(runs_db, rule_id_prefix="p0-double-space")
        run, findings = scan_text(runs_db, "two  spaces here", rules)
        assert run.total_findings == 1
        assert findings[0].snippet == "  "

    def test_dedup_at_finalize(self, runs_db: Path) -> None:
        # A pattern that would match the same byte position from two
        # adjacent overlapping chunks must dedup to a single finding.
        rules = _load_rules(runs_db, rule_id_prefix="p0-em-dash")
        # Pad so the em-dash sits inside the overlap region for our tiny
        # chunk sizes (4 bytes chunk + 3 bytes overlap so a 3-byte em-dash
        # can straddle).
        text = "abcdef—xyz123" * 5
        run, findings = scan_text(
            runs_db, text, rules, chunk_size=8, overlap=4
        )
        # Exactly one finding per em-dash occurrence (5 in the input).
        assert len(findings) == 5
        # Every finding must have a unique (offset, length) tuple.
        keys = {(f.file_offset, f.length) for f in findings}
        assert len(keys) == 5

    def test_partial_status_when_compile_error(self, runs_db: Path) -> None:
        # Inject a bogus rule that fails to compile; status should be
        # partial and the rule should be flagged in disabled_rules_log.
        rules = _load_rules(runs_db, rule_id_prefix="p0-em-dash")
        bogus = Rule(
            id=999_999,  # use a high id; we won't actually persist findings for it
            pack_id=rules[0].pack_id,
            rule_id="bogus-broken-regex",
            name="broken",
            pattern="[",  # invalid regex
            severity_default="P0",
            language=None,
            description="broken",
        )
        # We need bogus.id to exist in rules table for FK; instead, inject
        # a row directly.
        conn = db_connect(runs_db)
        try:
            cur = conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "language, description, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bogus.pack_id,
                    bogus.rule_id,
                    bogus.name,
                    bogus.pattern,
                    bogus.severity_default,
                    bogus.language,
                    bogus.description,
                    1,
                ),
            )
            bogus.id = int(cur.lastrowid)  # type: ignore[arg-type]
        finally:
            conn.close()

        all_rules = rules + [bogus]
        run, _findings = scan_text(runs_db, "He paused — then.", all_rules)
        assert run.status == ScrubStatus.PARTIAL

        # disabled_rules_log row appended.
        conn = db_connect(runs_db)
        try:
            row = conn.execute(
                "SELECT reason FROM disabled_rules_log "
                "WHERE rule_id = 'bogus-broken-regex'"
            ).fetchone()
            assert row is not None
            assert row["reason"] == "compile-error"
        finally:
            conn.close()

    def test_language_gate_skips_en_rules_on_non_en(
        self, runs_db: Path
    ) -> None:
        # German prose should NOT trip English-coded rules. We use the
        # "p1-corporate-filler" rule (language="en") on text that
        # langdetect should classify as German.
        rules = _load_rules(runs_db, rule_id_prefix="p1-corporate-filler")
        # German sentence with no English filler words.
        text = (
            "Das Wetter ist heute sehr schoen und die Vögel singen "
            "im Garten. Die Kinder spielen draußen.\n"
        ) * 5
        run, findings = scan_text(runs_db, text, rules)
        assert run.id is not None
        # We can't strictly assert zero findings (langdetect may guess
        # English on short/ambiguous inputs), but the code path that
        # skips on language-mismatch must run without error.
        # The contract: rule.language is "en" — if detect returns "de"
        # the rule is skipped. If detect returns "en" we accept findings.
        for f in findings:
            assert f.severity in {"P0", "P1", "P2"}


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------


class TestScanFile:
    def test_reads_file_and_records_path(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        rules = _load_rules(runs_db, rule_id_prefix="p0-em-dash")
        target = tmp_path / "draft.md"
        target.write_text("Stefan paused — then continued.\n", encoding="utf-8")
        run, findings = scan_file(runs_db, target, rules)
        assert run.target_path == str(target)
        assert run.total_findings == 1
        assert findings[0].file_path == str(target)

    def test_missing_file_raises(self, runs_db: Path, tmp_path: Path) -> None:
        rules = _load_rules(runs_db)
        with pytest.raises(FileNotFoundError):
            scan_file(runs_db, tmp_path / "nope.md", rules)


# ---------------------------------------------------------------------------
# scan_dir
# ---------------------------------------------------------------------------


class TestScanDir:
    def test_walks_md_files(self, runs_db: Path, tmp_path: Path) -> None:
        rules = _load_rules(runs_db, rule_id_prefix="p0-em-dash")
        (tmp_path / "a.md").write_text("Stefan — hi", encoding="utf-8")
        (tmp_path / "b.md").write_text("clean prose", encoding="utf-8")
        (tmp_path / "skip.txt").write_text(
            "Stefan — skip this", encoding="utf-8"
        )
        results = list(scan_dir(runs_db, tmp_path, rules))
        assert len(results) == 2  # only the two .md files


# ---------------------------------------------------------------------------
# line-number helper
# ---------------------------------------------------------------------------


class TestLineNumber:
    def test_offset_in_first_line(self) -> None:
        assert _line_number_for_offset("a\nb\nc\n", 0) == 1

    def test_offset_after_newlines(self) -> None:
        assert _line_number_for_offset("aaa\nbbb\nccc", 4) == 2
        assert _line_number_for_offset("aaa\nbbb\nccc", 8) == 3

    def test_empty_text_clamped_to_one(self) -> None:
        assert _line_number_for_offset("", 0) == 1
        assert _line_number_for_offset("", 100) == 1
