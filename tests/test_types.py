"""Unit tests for core.types — dataclass invariants + sha256 stability."""

from __future__ import annotations

import dataclasses

import pytest

from aurochs_llm_scrub.core.types import (
    HASH_INPUT_VERSION,
    DisabledRuleEntry,
    Finding,
    Rule,
    RuleMetrics,
    RulePack,
    ScrubMode,
    ScrubRun,
    ScrubStatus,
    compute_finding_sha256,
    normalize_text,
)

_VALID_SHA256 = "a" * 64


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


class TestNormalizeText:
    def test_collapses_runs(self) -> None:
        assert normalize_text("a  b\t\tc\n\nd") == "a b c d"

    def test_strips_ends(self) -> None:
        assert normalize_text("   hello   ") == "hello"

    def test_idempotent(self) -> None:
        once = normalize_text("foo  bar\nbaz")
        assert normalize_text(once) == once

    def test_empty(self) -> None:
        assert normalize_text("") == ""
        assert normalize_text("   \n\t  ") == ""

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            normalize_text(b"bytes-not-str")  # type: ignore[arg-type]


class TestComputeFindingSha256:
    def test_deterministic(self) -> None:
        h1 = compute_finding_sha256(
            snippet="leverage synergy",
            rule_id="p0-buzzword",
            file_path="draft.md",
            file_offset=42,
        )
        h2 = compute_finding_sha256(
            snippet="leverage synergy",
            rule_id="p0-buzzword",
            file_path="draft.md",
            file_offset=42,
        )
        assert h1 == h2

    def test_offset_changes_hash(self) -> None:
        # Same rule, same snippet, different offset = different
        # finding.
        h_42 = compute_finding_sha256(
            snippet="x", rule_id="r", file_path="f", file_offset=42
        )
        h_43 = compute_finding_sha256(
            snippet="x", rule_id="r", file_path="f", file_offset=43
        )
        assert h_42 != h_43

    def test_rule_id_changes_hash(self) -> None:
        h_a = compute_finding_sha256(
            snippet="x", rule_id="rule-a", file_path="f", file_offset=0
        )
        h_b = compute_finding_sha256(
            snippet="x", rule_id="rule-b", file_path="f", file_offset=0
        )
        assert h_a != h_b

    def test_file_path_changes_hash(self) -> None:
        h_1 = compute_finding_sha256(
            snippet="x", rule_id="r", file_path="a.md", file_offset=0
        )
        h_2 = compute_finding_sha256(
            snippet="x", rule_id="r", file_path="b.md", file_offset=0
        )
        assert h_1 != h_2

    def test_whitespace_invariant_on_snippet(self) -> None:
        # Snippet runs through normalize_text before hashing.
        h_normal = compute_finding_sha256(
            snippet="hello world",
            rule_id="r",
            file_path="f",
            file_offset=0,
        )
        h_extra_ws = compute_finding_sha256(
            snippet="hello   world",
            rule_id="r",
            file_path="f",
            file_offset=0,
        )
        h_newlines = compute_finding_sha256(
            snippet="hello\n\nworld",
            rule_id="r",
            file_path="f",
            file_offset=0,
        )
        assert h_normal == h_extra_ws == h_newlines

    def test_returns_64_hex(self) -> None:
        h = compute_finding_sha256(
            snippet="anything",
            rule_id="any",
            file_path="anywhere",
            file_offset=0,
        )
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestScrubStatus:
    def test_vocabulary_locked(self) -> None:
        # Plan v4 locked vocabulary.
        expected = {
            "pending",
            "running",
            "success",
            "partial",
            "failed",
            "aborted",
        }
        actual = {s.value for s in ScrubStatus}
        assert actual == expected

    def test_strenum_round_trip(self) -> None:
        # StrEnum values cleanly survive sqlite TEXT round-trip + CHECK
        # constraint match.
        assert ScrubStatus.SUCCESS == "success"
        assert str(ScrubStatus.SUCCESS) == "success"
        assert ScrubStatus.SUCCESS.value == "success"


class TestScrubMode:
    def test_vocabulary_locked(self) -> None:
        # Plan v4 locked vocabulary.
        expected = {
            "scan",
            "scrub-in-place",
            "scrub-to-stdout",
            "precommit",
        }
        actual = {m.value for m in ScrubMode}
        assert actual == expected

    def test_strenum_round_trip(self) -> None:
        assert ScrubMode.SCAN == "scan"
        assert str(ScrubMode.SCRUB_IN_PLACE) == "scrub-in-place"


# ---------------------------------------------------------------------------
# RulePack
# ---------------------------------------------------------------------------


class TestRulePack:
    def test_minimal_construction(self) -> None:
        p = RulePack(
            name="default",
            version="0.1.0",
            sha256=_VALID_SHA256,
            registered_at=1700_000_000,
        )
        assert p.name == "default"
        assert p.version == "0.1.0"
        assert p.schema_version == "0.1.0"
        assert p.source_path is None
        assert p.id is None

    def test_kw_only(self) -> None:
        with pytest.raises(TypeError):
            RulePack("default", "0.1.0", _VALID_SHA256, 0)  # type: ignore[misc]

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="",
                version="0.1.0",
                sha256=_VALID_SHA256,
                registered_at=0,
            )

    def test_rejects_empty_version(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="default",
                version="",
                sha256=_VALID_SHA256,
                registered_at=0,
            )

    def test_rejects_empty_schema_version(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="default",
                version="0.1.0",
                schema_version="",
                sha256=_VALID_SHA256,
                registered_at=0,
            )

    def test_rejects_negative_registered_at(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="default",
                version="0.1.0",
                sha256=_VALID_SHA256,
                registered_at=-1,
            )

    def test_rejects_short_sha256(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="default",
                version="0.1.0",
                sha256="abc",
                registered_at=0,
            )

    def test_rejects_uppercase_sha256(self) -> None:
        with pytest.raises(ValueError):
            RulePack(
                name="default",
                version="0.1.0",
                sha256="A" * 64,
                registered_at=0,
            )


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


class TestRule:
    def test_minimal_construction(self) -> None:
        r = Rule(
            pack_id=1,
            rule_id="p0-em-dash",
            name="Em dash",
            pattern="—",
            severity_default="P0",
            description="Mechanical em-dash detector",
        )
        assert r.rule_id == "p0-em-dash"
        assert r.language is None
        assert r.enabled is True
        assert r.id is None

    def test_kw_only(self) -> None:
        with pytest.raises(TypeError):
            Rule(
                1, "r", "n", "p", "P0", None, "d"
            )  # type: ignore[misc]

    def test_rejects_zero_pack_id(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                pack_id=0,
                rule_id="r",
                name="n",
                pattern="p",
                severity_default="P0",
                description="d",
            )

    def test_rejects_empty_rule_id(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                pack_id=1,
                rule_id="",
                name="n",
                pattern="p",
                severity_default="P0",
                description="d",
            )

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                pack_id=1,
                rule_id="r",
                name="",
                pattern="p",
                severity_default="P0",
                description="d",
            )

    def test_rejects_empty_pattern(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                pack_id=1,
                rule_id="r",
                name="n",
                pattern="",
                severity_default="P0",
                description="d",
            )

    def test_rejects_invalid_severity(self) -> None:
        with pytest.raises(ValueError):
            Rule(
                pack_id=1,
                rule_id="r",
                name="n",
                pattern="p",
                severity_default="P9",
                description="d",
            )

    def test_rejects_empty_language_string(self) -> None:
        # "" is not valid; None or non-empty string is.
        with pytest.raises(ValueError):
            Rule(
                pack_id=1,
                rule_id="r",
                name="n",
                pattern="p",
                severity_default="P0",
                language="",
                description="d",
            )

    def test_accepts_none_language(self) -> None:
        # Mechanical rules have language=None and always run.
        r = Rule(
            pack_id=1,
            rule_id="r",
            name="n",
            pattern="p",
            severity_default="P0",
            language=None,
            description="d",
        )
        assert r.language is None

    def test_accepts_en_language(self) -> None:
        r = Rule(
            pack_id=1,
            rule_id="r",
            name="n",
            pattern="p",
            severity_default="P1",
            language="en",
            description="d",
        )
        assert r.language == "en"


# ---------------------------------------------------------------------------
# ScrubRun
# ---------------------------------------------------------------------------


class TestScrubRun:
    def test_minimal_construction(self) -> None:
        r = ScrubRun(
            started_at=1700_000_000,
            status=ScrubStatus.RUNNING,
            target_path="draft.md",
            mode=ScrubMode.SCAN,
        )
        assert r.status == ScrubStatus.RUNNING
        assert r.mode == ScrubMode.SCAN
        assert r.total_findings == 0
        assert r.total_redacted == 0
        assert r.ended_at is None

    def test_rejects_non_enum_status(self) -> None:
        with pytest.raises(TypeError):
            ScrubRun(
                started_at=0,
                status="running",  # type: ignore[arg-type]
                target_path="f.md",
                mode=ScrubMode.SCAN,
            )

    def test_rejects_non_enum_mode(self) -> None:
        with pytest.raises(TypeError):
            ScrubRun(
                started_at=0,
                status=ScrubStatus.PENDING,
                target_path="f.md",
                mode="scan",  # type: ignore[arg-type]
            )

    def test_rejects_ended_before_started(self) -> None:
        with pytest.raises(ValueError):
            ScrubRun(
                started_at=100,
                ended_at=50,
                status=ScrubStatus.SUCCESS,
                target_path="f.md",
                mode=ScrubMode.SCAN,
            )

    def test_rejects_empty_target_path(self) -> None:
        with pytest.raises(ValueError):
            ScrubRun(
                started_at=0,
                status=ScrubStatus.PENDING,
                target_path="",
                mode=ScrubMode.SCAN,
            )

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError):
            ScrubRun(
                started_at=0,
                status=ScrubStatus.SUCCESS,
                target_path="f.md",
                mode=ScrubMode.SCAN,
                total_findings=-1,
            )
        with pytest.raises(ValueError):
            ScrubRun(
                started_at=0,
                status=ScrubStatus.SUCCESS,
                target_path="f.md",
                mode=ScrubMode.SCAN,
                total_redacted=-1,
            )


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    def test_minimal_construction(self) -> None:
        f = Finding(
            run_id=1,
            rule_id=2,
            file_offset=100,
            length=12,
            file_path="draft.md",
            line_number=3,
            severity="P0",
            snippet="leverage",
        )
        assert f.run_id == 1
        assert f.rule_id == 2
        assert f.schema_version == HASH_INPUT_VERSION
        assert f.id is None

    def test_rejects_zero_run_id(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=0,
                rule_id=2,
                file_offset=0,
                length=1,
                file_path="f",
                line_number=1,
                severity="P0",
                snippet="x",
            )

    def test_rejects_zero_rule_id(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=1,
                rule_id=0,
                file_offset=0,
                length=1,
                file_path="f",
                line_number=1,
                severity="P0",
                snippet="x",
            )

    def test_rejects_negative_offset(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=1,
                rule_id=1,
                file_offset=-1,
                length=1,
                file_path="f",
                line_number=1,
                severity="P0",
                snippet="x",
            )

    def test_rejects_zero_length(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=1,
                rule_id=1,
                file_offset=0,
                length=0,
                file_path="f",
                line_number=1,
                severity="P0",
                snippet="x",
            )

    def test_rejects_zero_line_number(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=1,
                rule_id=1,
                file_offset=0,
                length=1,
                file_path="f",
                line_number=0,
                severity="P0",
                snippet="x",
            )

    def test_rejects_invalid_severity(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                run_id=1,
                rule_id=1,
                file_offset=0,
                length=1,
                file_path="f",
                line_number=1,
                severity="P9",
                snippet="x",
            )

    def test_rejects_non_string_snippet(self) -> None:
        with pytest.raises(TypeError):
            Finding(
                run_id=1,
                rule_id=1,
                file_offset=0,
                length=1,
                file_path="f",
                line_number=1,
                severity="P0",
                snippet=b"bytes",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# RuleMetrics
# ---------------------------------------------------------------------------


class TestRuleMetrics:
    def test_minimal_construction(self) -> None:
        m = RuleMetrics(rule_id=1, run_id=1)
        assert m.hit_count == 0
        assert m.false_positive_count == 0
        assert m.latency_ms_total == 0

    def test_rejects_zero_rule_id(self) -> None:
        with pytest.raises(ValueError):
            RuleMetrics(rule_id=0, run_id=1)

    def test_rejects_zero_run_id(self) -> None:
        with pytest.raises(ValueError):
            RuleMetrics(rule_id=1, run_id=0)

    def test_rejects_fp_exceeds_hits(self) -> None:
        with pytest.raises(ValueError):
            RuleMetrics(
                rule_id=1, run_id=1, hit_count=5, false_positive_count=10
            )

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError):
            RuleMetrics(rule_id=1, run_id=1, hit_count=-1)
        with pytest.raises(ValueError):
            RuleMetrics(rule_id=1, run_id=1, latency_ms_total=-1)


# ---------------------------------------------------------------------------
# DisabledRuleEntry
# ---------------------------------------------------------------------------


class TestDisabledRuleEntry:
    def test_minimal_construction(self) -> None:
        e = DisabledRuleEntry(
            pack_name="default",
            pack_version="0.1.0",
            rule_id="p0-em-dash",
            disabled_at=1700_000_000,
            reason="timeout",
        )
        assert e.pack_name == "default"
        assert e.redos_evidence is None

    def test_with_redos_evidence(self) -> None:
        e = DisabledRuleEntry(
            pack_name="custom",
            pack_version="0.0.1",
            rule_id="custom-12",
            disabled_at=0,
            reason="redos-static",
            redos_evidence="aaaaaaaaaaaaaaab",
        )
        assert e.redos_evidence == "aaaaaaaaaaaaaaab"

    def test_rejects_empty_pack_name(self) -> None:
        with pytest.raises(ValueError):
            DisabledRuleEntry(
                pack_name="",
                pack_version="0.1.0",
                rule_id="r",
                disabled_at=0,
                reason="timeout",
            )

    def test_rejects_empty_rule_id(self) -> None:
        with pytest.raises(ValueError):
            DisabledRuleEntry(
                pack_name="default",
                pack_version="0.1.0",
                rule_id="",
                disabled_at=0,
                reason="timeout",
            )

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValueError):
            DisabledRuleEntry(
                pack_name="default",
                pack_version="0.1.0",
                rule_id="r",
                disabled_at=0,
                reason="",
            )

    def test_rejects_negative_disabled_at(self) -> None:
        with pytest.raises(ValueError):
            DisabledRuleEntry(
                pack_name="default",
                pack_version="0.1.0",
                rule_id="r",
                disabled_at=-1,
                reason="timeout",
            )


# ---------------------------------------------------------------------------
# Slots discipline
# ---------------------------------------------------------------------------


class TestSlots:
    """All llm-scrub dataclasses use slots=True; rogue attrs must
    fail."""

    def test_rule_pack_slots(self) -> None:
        p = RulePack(
            name="default",
            version="0.1.0",
            sha256=_VALID_SHA256,
            registered_at=0,
        )
        assert dataclasses.is_dataclass(p)
        with pytest.raises(AttributeError):
            p.rogue_attr = "no"  # type: ignore[attr-defined]

    def test_rule_slots(self) -> None:
        r = Rule(
            pack_id=1,
            rule_id="r",
            name="n",
            pattern="p",
            severity_default="P0",
            description="d",
        )
        with pytest.raises(AttributeError):
            r.rogue_attr = "no"  # type: ignore[attr-defined]

    def test_scrub_run_slots(self) -> None:
        r = ScrubRun(
            started_at=0,
            status=ScrubStatus.PENDING,
            target_path="f.md",
            mode=ScrubMode.SCAN,
        )
        with pytest.raises(AttributeError):
            r.rogue_attr = "no"  # type: ignore[attr-defined]

    def test_finding_slots(self) -> None:
        f = Finding(
            run_id=1,
            rule_id=1,
            file_offset=0,
            length=1,
            file_path="f",
            line_number=1,
            severity="P0",
            snippet="x",
        )
        with pytest.raises(AttributeError):
            f.rogue_attr = "no"  # type: ignore[attr-defined]

    def test_rule_metrics_slots(self) -> None:
        m = RuleMetrics(rule_id=1, run_id=1)
        with pytest.raises(AttributeError):
            m.rogue_attr = "no"  # type: ignore[attr-defined]

    def test_disabled_rule_entry_slots(self) -> None:
        e = DisabledRuleEntry(
            pack_name="default",
            pack_version="0.1.0",
            rule_id="r",
            disabled_at=0,
            reason="timeout",
        )
        with pytest.raises(AttributeError):
            e.rogue_attr = "no"  # type: ignore[attr-defined]
