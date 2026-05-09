"""Unit tests for core.redact — pragma scope, .bak rotation, in-place writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurochs_llm_scrub.core import (
    Rule,
    ScrubMode,
    db_connect,
)
from aurochs_llm_scrub.core.db import MigrationRunner
from aurochs_llm_scrub.core.redact import (
    apply_redactions,
    parse_pragma_ranges,
)
from aurochs_llm_scrub.core.rule_pack import register_default_pack
from aurochs_llm_scrub.core.scrubber import scan_file


@pytest.fixture
def runs_db(tmp_path: Path) -> Path:
    db = tmp_path / "runs.db"
    MigrationRunner(db).run()
    register_default_pack(db)
    return db


def _rules_and_lookup(db_path: Path) -> tuple[list[Rule], dict[int, str]]:
    """Load all enabled rules and build the rule_id_lookup."""
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id, r.pack_id, r.rule_id, r.name, r.pattern, "
            "r.severity_default, r.language, r.description, r.enabled "
            "FROM rules r JOIN rule_packs p ON p.id = r.pack_id "
            "WHERE p.name = 'default' AND r.enabled = 1"
        ).fetchall()
    finally:
        conn.close()
    rules = [
        Rule(
            id=int(r["id"]),
            pack_id=int(r["pack_id"]),
            rule_id=str(r["rule_id"]),
            name=str(r["name"]),
            pattern=str(r["pattern"]),
            severity_default=str(r["severity_default"]),
            language=r["language"],
            description=str(r["description"]),
            enabled=bool(r["enabled"]),
        )
        for r in rows
    ]
    lookup = {r.id: r.rule_id for r in rules if r.id is not None}
    return rules, lookup


# ---------------------------------------------------------------------------
# Pragma parser
# ---------------------------------------------------------------------------


class TestPragmaRanges:
    def test_no_pragma_returns_empty(self) -> None:
        assert parse_pragma_ranges("hello\nworld\n") == []

    def test_single_line_ignore(self) -> None:
        text = "line1\nline2  # scrub: ignore\nline3\n"
        ranges = parse_pragma_ranges(text)
        assert ranges == [(2, 2)]

    def test_html_comment_ignore(self) -> None:
        text = "line1\nline2 <!-- scrub: ignore -->\nline3\n"
        ranges = parse_pragma_ranges(text)
        assert ranges == [(2, 2)]

    def test_block_scope(self) -> None:
        text = (
            "before\n"
            "# scrub: ignore-block-start\n"
            "skip me\n"
            "skip too\n"
            "# scrub: ignore-block-end\n"
            "after\n"
        )
        ranges = parse_pragma_ranges(text)
        assert ranges == [(2, 5)]

    def test_unclosed_block_consumes_eof(self) -> None:
        text = (
            "line1\n"
            "# scrub: ignore-block-start\n"
            "line3\nline4\n"
        )
        ranges = parse_pragma_ranges(text)
        assert ranges == [(2, 4)]


# ---------------------------------------------------------------------------
# apply_redactions — scan mode (no-op)
# ---------------------------------------------------------------------------


class TestScanMode:
    def test_scan_mode_no_op(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        target.write_text("paused — then\n", encoding="utf-8")
        _run, findings = scan_file(runs_db, target, rules)
        result = apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCAN,
            rule_id_lookup=lookup,
        )
        # Scan never modifies.
        assert not result.modified
        assert result.applied_count == 0
        # Original content unchanged.
        assert "—" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# apply_redactions — in-place mode (.bak rotation)
# ---------------------------------------------------------------------------


class TestInPlaceMode:
    def test_em_dash_replaced(self, runs_db: Path, tmp_path: Path) -> None:
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        target.write_text("paused — then\n", encoding="utf-8")
        _run, findings = scan_file(runs_db, target, rules)
        result = apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCRUB_IN_PLACE,
            rule_id_lookup=lookup,
        )
        assert result.modified
        assert result.applied_count >= 1
        # File contains the replacement (", " for em-dash). Source had
        # spaces around the dash, so the literal output is
        # "paused ,  then" — the replacement is mechanical, not
        # whitespace-aware. (Real workflows then run the
        # double-space rule to clean up; tested separately.)
        new_text = target.read_text(encoding="utf-8")
        assert "—" not in new_text
        assert ", " in new_text

    def test_bak_created(self, runs_db: Path, tmp_path: Path) -> None:
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        original = "paused — then\n"
        target.write_text(original, encoding="utf-8")
        _run, findings = scan_file(runs_db, target, rules)
        result = apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCRUB_IN_PLACE,
            rule_id_lookup=lookup,
        )
        assert result.backup_path is not None
        # First-time bak is at .bak; rotated paths get suffixed.
        assert result.backup_path.exists()
        # Bak contains the ORIGINAL text, not the scrubbed one.
        assert result.backup_path.read_text(encoding="utf-8") == original

    def test_bak_collision_rotates(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        # BLOCKER #9: existing .bak gets rotated, not overwritten.
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        target.write_text("paused — then\n", encoding="utf-8")
        # Pre-create a .bak.
        bak = target.with_name(target.name + ".bak")
        bak.write_text("PRIOR BAK CONTENT\n", encoding="utf-8")

        _run, findings = scan_file(runs_db, target, rules)
        apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCRUB_IN_PLACE,
            rule_id_lookup=lookup,
        )
        # The original .bak now exists at a rotated suffix; new .bak
        # contains the just-replaced contents.
        rotated = sorted(target.parent.glob("draft.md.bak*"))
        # At least: draft.md.bak (current) + draft.md.bak.<ts> (rotated).
        assert len(rotated) >= 2

    def test_pragma_ignore_skips_finding(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        # BLOCKER #8: a line with `# scrub: ignore` is exempt.
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        target.write_text(
            "paused — first\n"
            "paused — second  # scrub: ignore\n",
            encoding="utf-8",
        )
        _run, findings = scan_file(runs_db, target, rules)
        result = apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCRUB_IN_PLACE,
            rule_id_lookup=lookup,
        )
        assert result.skipped_pragma >= 1
        new_text = target.read_text(encoding="utf-8")
        # First line was rewritten (em-dash → ", "), second line still
        # has the em-dash because the pragma exempted it.
        assert "paused ,  first" in new_text
        assert "paused — second" in new_text


# ---------------------------------------------------------------------------
# apply_redactions — stdout/sidecar mode
# ---------------------------------------------------------------------------


class TestStdoutMode:
    def test_writes_to_scrubbed_sibling(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        rules, lookup = _rules_and_lookup(runs_db)
        target = tmp_path / "draft.md"
        target.write_text("paused — then\n", encoding="utf-8")
        _run, findings = scan_file(runs_db, target, rules)
        result = apply_redactions(
            file_path=target,
            findings=findings,
            mode=ScrubMode.SCRUB_TO_STDOUT,
            rule_id_lookup=lookup,
        )
        # Output path is the .scrubbed sibling.
        assert result.output_path.name == "draft.md.scrubbed"
        assert result.output_path.exists()
        # Original untouched.
        assert "—" in target.read_text(encoding="utf-8")
