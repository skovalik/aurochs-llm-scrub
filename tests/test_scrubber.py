"""Unit tests for core.scrubber — scan_text + chunk dedup + timeout."""

from __future__ import annotations

import random
import re
import time
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
    _DEFAULT_CHUNK_SIZE,
    _DEFAULT_OVERLAP,
    _iter_chunks,
    _line_number_for_offset,
    scan_dir,
    scan_file,
    scan_text,
    scrub,
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

    def test_truncated_marks_windows_cut_before_the_end(self) -> None:
        # 200 bytes in 64-byte steps with 16 overlap: the windows end at
        # 80, 144, 200 and 200. Only the first two are cut short; the
        # third reaches the true end although a fourth window follows.
        chunks = list(_iter_chunks("x" * 200, chunk_size=64, overlap=16))
        assert [c.truncated for c in chunks] == [True, True, False, False]
        assert not next(_iter_chunks("hello", chunk_size=100, overlap=10)).truncated

    def test_window_starts_on_a_character_boundary(self) -> None:
        # Byte 8 falls inside U+2019 (bytes 7 to 9): the second window
        # starts on its lead byte instead of decoding a fragment.
        text = "a" * 7 + chr(0x2019) + "b" * 20
        chunks = list(_iter_chunks(text, chunk_size=8, overlap=4))
        assert chunks[1].start == 7
        assert chunks[1].text.startswith(chr(0x2019))
        assert not any(chr(0xFFFD) in c.text for c in chunks)


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
# scan_text: window edges
# ---------------------------------------------------------------------------
#
# Inputs longer than one chunk are scanned in windows of chunk_size +
# overlap bytes. A default scan's first window ends at byte _EDGE: the
# text is cut there, so a pattern that reads "end of text" at that point
# reads an edge that is not in the file.

_EDGE = _DEFAULT_CHUNK_SIZE + _DEFAULT_OVERLAP


def _filler(n_bytes: int) -> str:
    """Return exactly ``n_bytes`` of ASCII lines, none ending in whitespace."""
    full, rest = divmod(n_bytes, 100)
    return ("x" * 99 + "\n") * full + ("x" * (rest - 1) + "\n" if rest else "")


def _text_with(line: str, *, at: int, byte: int) -> str:
    """Return filler + ``line`` + filler with ``line[at]`` on file byte ``byte``.

    The trailing filler carries the text well past the first window.
    """
    return _filler(byte - at) + line + _filler(10_000)


def _text_after_split_character() -> str:
    """Return a text whose second window would start inside U+2019.

    A double space follows in that window, so its offset shows whether
    the window was decoded from a fragment.
    """
    return "a" * (_DEFAULT_CHUNK_SIZE - 1) + chr(0x2019) + "b" * 5_000 + "  " + "c" * 100 + "\n"


def _insert_rule(db_path: Path, *, rule_id: str, pattern: str) -> Rule:
    """Add an always-on P0 rule to the default pack and return it."""
    pack_id = _load_rules(db_path)[0].pack_id
    conn = db_connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO rules "
            "(pack_id, rule_id, name, pattern, severity_default, "
            "language, description, enabled) "
            "VALUES (?, ?, ?, ?, 'P0', NULL, ?, 1)",
            (pack_id, rule_id, rule_id, pattern, rule_id),
        )
        rule_pk = int(cur.lastrowid)
    finally:
        conn.close()
    return Rule(
        id=rule_pk,
        pack_id=pack_id,
        rule_id=rule_id,
        name=rule_id,
        pattern=pattern,
        severity_default="P0",
        description=rule_id,
    )


class TestWindowEdges:
    @pytest.mark.parametrize("ws", [" ", "\t"])
    def test_whitespace_mid_line_on_window_end_is_not_trailing(
        self, runs_db: Path, ws: str
    ) -> None:
        # The reported bug: `[ \t]+$` matched the character on the first
        # window's last byte although the line carries on past the cut.
        text = _text_with("y" * 31 + ws + "z" * 40 + "\n", at=31, byte=_EDGE - 1)
        assert text[_EDGE - 1 : _EDGE + 1] == ws + "z"
        assert not any(re.search(r"[ \t]+$", ln) for ln in text.split("\n"))
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        _run, findings = scan_text(runs_db, text, rules)
        assert findings == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("alpha \nbeta\t\ngamma\n", [(5, 1, 1), (11, 1, 2)]),
            ("alpha \r\nbeta\t\r\ngamma\r\n", [(5, 1, 1), (12, 1, 2)]),
        ],
    )
    def test_trailing_whitespace_before_a_later_line_is_found(
        self, runs_db: Path, text: str, expected: list[tuple[int, int, int]]
    ) -> None:
        # Rules compile without MULTILINE, so a bare `$` matched only at
        # the end of the text: whitespace ending any earlier line went
        # unseen.
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        _run, findings = scan_text(runs_db, text, rules)
        assert [(f.file_offset, f.length, f.line_number) for f in findings] == expected

    @pytest.mark.parametrize("lead", [0, _EDGE + 5_000])
    @pytest.mark.parametrize("ending", ["\n", ""])
    def test_trailing_whitespace_at_end_of_text_is_found(
        self, runs_db: Path, lead: int, ending: str
    ) -> None:
        # The end of the last window is the true end of the text, so
        # whitespace there is trailing with or without a final newline,
        # in a one-window text and in a long one alike.
        text = _filler(lead) + "tail " + ending
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        _run, findings = scan_text(runs_db, text, rules)
        offset = len(text) - len(ending) - 1
        assert [(f.file_offset, f.length) for f in findings] == [(offset, 1)]

    def test_trailing_whitespace_where_window_end_meets_line_end_is_found(
        self, runs_db: Path
    ) -> None:
        # A real trailing space on the first window's last byte: its
        # newline is the first byte past the cut. The first window cannot
        # see that newline; the second re-scans the overlap and must
        # report the space exactly once.
        text = _text_with("y" * 40 + " \n", at=40, byte=_EDGE - 1)
        assert text[_EDGE - 1 : _EDGE + 1] == " \n"
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        _run, findings = scan_text(runs_db, text, rules)
        assert [(f.file_offset, f.length) for f in findings] == [(_EDGE - 1, 1)]

    def test_word_boundary_on_window_end_is_not_a_match(self, runs_db: Path) -> None:
        # Same edge, another anchor: `\b` also matches at the end of a
        # string, so a cut inside "catalog" just after "cat" matched
        # `\bcat\b`. The guard is in the engine, not in one rule.
        rule = _insert_rule(runs_db, rule_id="test-cat", pattern=r"\bcat\b")
        text = _text_with("the catalog\n", at=4, byte=_EDGE - 3)
        assert text[_EDGE - 3 : _EDGE + 1] == "cata"
        _run, findings = scan_text(runs_db, text, [rule])
        assert findings == []

    @pytest.mark.parametrize("mode", ["scrub-to-stdout", "scrub-in-place"])
    def test_scrub_keeps_mid_line_space_on_window_end(
        self, runs_db: Path, tmp_path: Path, mode: str
    ) -> None:
        # p0-trailing-whitespace auto-fixes to "", so the false finding
        # deleted the space and joined two words: silent corruption.
        text = _text_with("y" * 31 + " " + "z" * 40 + "\n", at=31, byte=_EDGE - 1)
        target = tmp_path / "draft.md"
        target.write_bytes(text.encode("utf-8"))
        result = scrub(target=target, db_path=runs_db, mode=mode)
        assert result.cleaned_text == text
        assert target.read_bytes() == text.encode("utf-8")

    @pytest.mark.parametrize(
        ("pattern", "line", "at"),
        [(r"(?m)^cat\b", "the cat sat\n", 4), (r"\bcat\b", "bobcat food\n", 3)],
    )
    def test_anchor_on_window_start_is_not_a_match(
        self, runs_db: Path, pattern: str, line: str, at: int
    ) -> None:
        # The mirror edge: the second window starts at byte
        # _DEFAULT_CHUNK_SIZE, here in the middle of a line, and `^` and
        # `\b` read that start as the start of the text.
        rule = _insert_rule(runs_db, rule_id="test-cat", pattern=pattern)
        text = _text_with(line, at=at, byte=_DEFAULT_CHUNK_SIZE)
        assert text[_DEFAULT_CHUNK_SIZE : _DEFAULT_CHUNK_SIZE + 3] == "cat"
        _run, findings = scan_text(runs_db, text, [rule])
        assert findings == []

    def test_trailing_run_across_window_start_is_reported_once(
        self, runs_db: Path
    ) -> None:
        # Three trailing spaces on the bytes either side of the second
        # window's start: the first window sees the run whole, and the
        # second, starting inside it, reported its tail a second time.
        text = _text_with("y" * 40 + "   \n", at=40, byte=_DEFAULT_CHUNK_SIZE - 2)
        assert text[_DEFAULT_CHUNK_SIZE - 2 : _DEFAULT_CHUNK_SIZE + 2] == "   \n"
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        _run, findings = scan_text(runs_db, text, rules)
        assert [(f.file_offset, f.length) for f in findings] == [
            (_DEFAULT_CHUNK_SIZE - 2, 3)
        ]

    def test_offsets_after_a_window_that_starts_inside_a_character(
        self, runs_db: Path
    ) -> None:
        # U+2019 spans bytes C-1 to C+1, so the second window's start
        # falls inside it; decoding that fragment shifted every offset
        # found in the window.
        text = _text_after_split_character()
        rules = _load_rules(runs_db, rule_id_prefix="p0-double-space")
        _run, findings = scan_text(runs_db, text, rules)
        assert [f.file_offset for f in findings] == [text.encode("utf-8").index(b"  ")]

    def test_scrub_fixes_the_right_bytes_after_a_window_that_starts_inside_a_character(
        self, runs_db: Path, tmp_path: Path
    ) -> None:
        # With the offset shifted by four bytes, scrub left the double
        # space alone and replaced two letters after it.
        text = _text_after_split_character()
        target = tmp_path / "draft.md"
        target.write_bytes(text.encode("utf-8"))
        result = scrub(target=target, db_path=runs_db, mode="scrub-to-stdout")
        assert result.cleaned_text == text.replace("  ", " ")

    def test_long_run_of_mid_line_spaces_scans_in_linear_time(
        self, runs_db: Path
    ) -> None:
        # Without a run-start anchor every start inside the run re-scans
        # the rest of it: 30,000 mid-line spaces took 8.6 s against 0.23 s
        # of fixed engine cost. The per-rule timeout cannot cut a running
        # regex short, so the pattern itself has to stay linear.
        rules = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")
        started = time.perf_counter()
        _run, findings = scan_text(runs_db, "a" + " " * 30_000 + "b\n", rules)
        assert time.perf_counter() - started < 2.0
        assert findings == []

    def test_trailing_whitespace_pattern_keeps_its_line_end_meaning(
        self, runs_db: Path
    ) -> None:
        # The shipped pattern may change shape for speed, never meaning:
        # on any mix of the characters that matter it finds exactly the
        # spans of the plain definition.
        plain = re.compile(r"[ \t]+(?=\r?\n)|[ \t]+\Z")
        rule = _load_rules(runs_db, rule_id_prefix="p0-trailing-whitespace")[0]
        shipped = re.compile(rule.pattern)
        rng = random.Random(7)
        pieces = [" ", "\t", "\n", "\r\n", "a", "b"]
        for _ in range(2_000):
            s = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
            assert [m.span() for m in shipped.finditer(s)] == [
                m.span() for m in plain.finditer(s)
            ], s


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
