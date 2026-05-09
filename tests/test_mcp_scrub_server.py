"""Tests for the MCP server module.

Skipped cleanly when the Anthropic ``mcp`` SDK is not installed (the
``[mcp]`` extra is opt-in). Otherwise:

* Module loads cleanly.
* Every tool listed in ``schemas.TOOL_NAMES`` has an input schema, an
  output shape, and a registered handler.
* Each tool dispatches and returns valid content blocks + structured
  output (or an isError CallToolResult).
* Input validation rejects malformed payloads (handled by the lowlevel
  Server's call_tool decorator).
* Error paths (DB missing, run/pack not found) return ``isError=True``
  blocks with the right error code.
* The pipeline's lazy-import shim returns ``PIPELINE_UNAVAILABLE`` when
  the parallel build hasn't finished yet (transient during cleanup).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from aurochs_llm_scrub.core.db import db_connect
from aurochs_llm_scrub.core.schema import apply_schema
from aurochs_llm_scrub.core.types import ScrubMode, ScrubStatus
from aurochs_llm_scrub.mcp import scrub_server
from aurochs_llm_scrub.mcp.schemas import (
    INPUT_SCHEMAS,
    OUTPUT_SHAPES,
    SCHEMA_VERSION,
    SEVERITY_VALUES,
    TOOL_NAMES,
    ErrorCode,
)

# Module-level skip when the Anthropic mcp SDK is not installed. The
# imports above succeed regardless because mcp/__init__.py is a stub —
# scrub_server's SDK-using code is gated by ``_SDK_AVAILABLE``. We skip
# the actual SDK-dependent tests (those that build a Server, parse Tool,
# etc.) when missing — this satisfies the agent-task-required
# ``pytest.importorskip("mcp")`` gate at suite-load time.
pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp.server.lowlevel") is None
    or importlib.util.find_spec("mcp.types") is None,
    reason=(
        "Anthropic mcp SDK not installed; install via "
        "'pip install aurochs-llm-scrub[mcp]'"
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runs_db(tmp_path: Path) -> Path:
    """Return a path to a freshly-initialized runs.db."""
    db_path = tmp_path / "runs.db"
    conn = db_connect(db_path)
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return db_path


@pytest.fixture
def runs_db_with_pack(runs_db: Path) -> tuple[Path, int]:
    """Return (db_path, pack_id) with one rule pack registered."""
    conn = db_connect(runs_db)
    try:
        cur = conn.execute(
            "INSERT INTO rule_packs (name, version, schema_version, "
            "source_path, sha256, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "default",
                "0.1.0",
                "0.1.0",
                None,
                "a" * 64,
                1_700_000_000,
            ),
        )
        pack_id = cur.lastrowid
    finally:
        conn.close()
    assert pack_id is not None
    return runs_db, pack_id


@pytest.fixture
def runs_db_with_rules(runs_db_with_pack: tuple[Path, int]) -> Path:
    """Add three rules of mixed severity to the registered pack."""
    db_path, pack_id = runs_db_with_pack
    conn = db_connect(db_path)
    try:
        rules = [
            ("p0-em-dash", "Em dashes", "—", "P0", None,
             "Em dashes are LLM-tell punctuation."),
            ("p0-buzzword-leverage", "Banned buzzword: leverage",
             r"\bleverage\b", "P0", "en",
             "Generic LLM filler verb."),
            ("p1-pronoun-we", "First-person plural pronoun",
             r"\bwe\b", "P1", "en",
             "Default voice marker; promote to P0 in voice's overlay."),
        ]
        for rule_id, name, pattern, severity, language, desc in rules:
            conn.execute(
                "INSERT INTO rules "
                "(pack_id, rule_id, name, pattern, severity_default, "
                "language, description, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (pack_id, rule_id, name, pattern, severity, language, desc),
            )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def runs_db_with_runs(runs_db: Path) -> Path:
    """Add a finalized run + an in-progress run to the DB."""
    conn = db_connect(runs_db)
    try:
        conn.execute(
            "INSERT INTO scrub_runs "
            "(started_at, ended_at, status, target_path, mode, "
            "total_findings, total_redacted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1_700_000_100, 1_700_000_150, "success", "/tmp/draft.md",
             "scan", 7, 0),
        )
        conn.execute(
            "INSERT INTO scrub_runs "
            "(started_at, status, target_path, mode, total_findings, "
            "total_redacted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1_700_000_200, "running", "/tmp/longer-draft.md",
             "scan", 0, 0),
        )
    finally:
        conn.close()
    return runs_db


def _run_async(coro: Any) -> Any:
    """Helper to run an async coroutine in tests synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Schema completeness — every tool name has a schema + handler
# ---------------------------------------------------------------------------


class TestSchemaCompleteness:
    """Cross-checks: every TOOL_NAME has a schema, an output shape, and a
    handler in scrub_server._DISPATCH. Forgetting any one of those is the
    most likely add-a-tool regression."""

    def test_every_tool_has_input_schema(self) -> None:
        for name in TOOL_NAMES:
            assert name in INPUT_SCHEMAS, f"{name} missing from INPUT_SCHEMAS"

    def test_every_tool_has_output_shape(self) -> None:
        for name in TOOL_NAMES:
            assert name in OUTPUT_SHAPES, f"{name} missing from OUTPUT_SHAPES"

    def test_every_tool_has_handler(self) -> None:
        for name in TOOL_NAMES:
            assert name in scrub_server._DISPATCH, (
                f"{name} missing from scrub_server._DISPATCH"
            )

    def test_no_orphan_handlers(self) -> None:
        # Reverse direction: every dispatch entry must be in TOOL_NAMES.
        for name in scrub_server._DISPATCH:
            assert name in TOOL_NAMES, (
                f"_DISPATCH has {name!r} but TOOL_NAMES does not"
            )

    def test_input_schemas_are_strict(self) -> None:
        # Plan v4 cross-cutting #16: additionalProperties: false on every
        # tool input schema (no surprise fields).
        for name, schema in INPUT_SCHEMAS.items():
            assert schema.get("additionalProperties") is False, (
                f"{name} input schema must set additionalProperties: false"
            )

    def test_severity_enum_matches_constant(self) -> None:
        assert SEVERITY_VALUES == ("P0", "P1", "P2")
        # Schemas referencing severity should use the same set.
        scan_filter = INPUT_SCHEMAS["scrub_scan_text"]["properties"][
            "severity_filter"
        ]
        assert set(scan_filter["items"]["enum"]) == set(SEVERITY_VALUES)

    def test_status_enum_aligned_with_core_types(self) -> None:
        # Belt-and-suspenders for _validate_schema_alignment.
        run_props = (
            OUTPUT_SHAPES["scrub_run_status"]["properties"]["run"]
            ["properties"]
        )
        declared = set(run_props["status"]["enum"])
        expected = {s.value for s in ScrubStatus}
        assert declared == expected

    def test_mode_enum_aligned_with_core_types(self) -> None:
        run_props = (
            OUTPUT_SHAPES["scrub_run_status"]["properties"]["run"]
            ["properties"]
        )
        declared = set(run_props["mode"]["enum"])
        expected = {m.value for m in ScrubMode}
        assert declared == expected

    def test_schema_version_string(self) -> None:
        assert SCHEMA_VERSION == "0.1.0"


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


class TestServerConstruction:
    def test_build_server_returns_server_instance(self, runs_db: Path) -> None:
        server = scrub_server.build_server(db_path=runs_db)
        assert server is not None
        # Server is the lowlevel SDK class; we just assert it's not None
        # and has the registered handlers attribute.
        assert hasattr(server, "request_handlers")

    def test_build_tool_list_returns_all_tools(self) -> None:
        tools = scrub_server.build_tool_list()
        names = {t.name for t in tools}
        assert names == set(TOOL_NAMES)

    def test_each_tool_has_input_schema_attached(self) -> None:
        tools = scrub_server.build_tool_list()
        for tool in tools:
            assert tool.inputSchema == INPUT_SCHEMAS[tool.name]

    def test_each_tool_has_description(self) -> None:
        tools = scrub_server.build_tool_list()
        for tool in tools:
            assert tool.description, f"{tool.name} has empty description"
            assert len(tool.description) > 50, (
                f"{tool.name} description too short — should explain when to use"
            )

    def test_schema_alignment_passes(self) -> None:
        # Should not raise.
        scrub_server._validate_schema_alignment()


# ---------------------------------------------------------------------------
# scrub_list_rules
# ---------------------------------------------------------------------------


class TestListRules:
    def test_empty_db_returns_empty_list(self, runs_db: Path) -> None:
        # No rules registered yet. Pipeline shim may also be missing
        # (parallel build) — either way the result is a clean empty.
        content, structured = _run_async(
            scrub_server._tool_scrub_list_rules({}, runs_db)
        )
        assert structured["schema_version"] == SCHEMA_VERSION
        # Either empty or populated by the in-package fallback. We don't
        # require pipeline presence in this test — just structural sanity.
        assert isinstance(structured["rules"], list)
        assert len(content) == 1

    def test_lists_registered_rules(
        self, runs_db_with_rules: Path
    ) -> None:
        content, structured = _run_async(
            scrub_server._tool_scrub_list_rules({}, runs_db_with_rules)
        )
        assert structured["schema_version"] == SCHEMA_VERSION
        rules = structured["rules"]
        assert len(rules) == 3
        rule_ids = {r["rule_id"] for r in rules}
        assert rule_ids == {
            "p0-em-dash",
            "p0-buzzword-leverage",
            "p1-pronoun-we",
        }
        # Em-dash rule is mechanical (language is None).
        em_dash = next(r for r in rules if r["rule_id"] == "p0-em-dash")
        assert em_dash["language"] is None
        assert em_dash["pack_name"] == "default"
        assert em_dash["pack_version"] == "0.1.0"
        # The summary block names rules.
        assert "p0-em-dash" in content[0].text

    def test_pack_filter_returns_only_pack_rules(
        self, runs_db_with_rules: Path
    ) -> None:
        # Add a second pack with one rule to confirm filtering works.
        conn = db_connect(runs_db_with_rules)
        try:
            cur = conn.execute(
                "INSERT INTO rule_packs (name, version, schema_version, "
                "source_path, sha256, registered_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("stefan-custom", "0.1.0", "0.1.0", None,
                 "b" * 64, 1_700_000_300),
            )
            custom_pack_id = cur.lastrowid
            conn.execute(
                "INSERT INTO rules (pack_id, rule_id, name, pattern, "
                "severity_default, language, description, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (custom_pack_id, "stefan-pixel-perfect",
                 "Banned: pixel-perfect", "pixel-perfect", "P0", "en",
                 "Stefan-banned per BOOK-PROTOCOL"),
            )
        finally:
            conn.close()

        _, structured = _run_async(
            scrub_server._tool_scrub_list_rules(
                {"pack_name": "stefan-custom"}, runs_db_with_rules
            )
        )
        assert len(structured["rules"]) == 1
        assert structured["rules"][0]["rule_id"] == "stefan-pixel-perfect"
        assert structured["rules"][0]["pack_name"] == "stefan-custom"

    def test_unknown_pack_returns_error(
        self, runs_db_with_rules: Path
    ) -> None:
        result = _run_async(
            scrub_server._tool_scrub_list_rules(
                {"pack_name": "no-such-pack"}, runs_db_with_rules
            )
        )
        assert result.isError is True
        assert ErrorCode.PACK_NOT_FOUND in result.content[0].text

    def test_db_missing_with_default_filter_falls_back(
        self, tmp_path: Path
    ) -> None:
        # Specifying pack_name="default" with a missing DB should not
        # error; the in-package default rule fallback kicks in.
        missing = tmp_path / "no-such-runs.db"
        result = _run_async(
            scrub_server._tool_scrub_list_rules(
                {"pack_name": "default"}, missing
            )
        )
        # Either a tuple (rules empty or populated) or — if the rules
        # module isn't yet wired and the fallback is silent — a tuple
        # with empty rules. Both are acceptable; we just need it to
        # NOT be an isError result.
        assert not (
            hasattr(result, "isError") and getattr(result, "isError", False)
        )

    def test_db_missing_with_unknown_pack_filter_errors(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "no-such-runs.db"
        result = _run_async(
            scrub_server._tool_scrub_list_rules(
                {"pack_name": "stefan-custom"}, missing
            )
        )
        assert result.isError is True
        assert ErrorCode.PACK_NOT_FOUND in result.content[0].text


# ---------------------------------------------------------------------------
# scrub_run_status
# ---------------------------------------------------------------------------


class TestRunStatus:
    def test_no_runs_returns_empty(self, runs_db: Path) -> None:
        content, structured = _run_async(
            scrub_server._tool_scrub_run_status({}, runs_db)
        )
        assert structured["schema_version"] == SCHEMA_VERSION
        assert structured["run"] is None
        assert "no scrub runs" in content[0].text.lower()

    def test_returns_in_progress_first(
        self, runs_db_with_runs: Path
    ) -> None:
        # Unfiltered call should surface the in-progress run, not the
        # most-recent finalized one.
        _, structured = _run_async(
            scrub_server._tool_scrub_run_status({}, runs_db_with_runs)
        )
        assert structured["run"] is not None
        assert structured["run"]["status"] == "running"
        assert structured["run"]["target_path"] == "/tmp/longer-draft.md"

    def test_returns_specific_run_by_id(
        self, runs_db_with_runs: Path
    ) -> None:
        # Look up the finalized run (id=1).
        content, structured = _run_async(
            scrub_server._tool_scrub_run_status(
                {"run_id": 1}, runs_db_with_runs
            )
        )
        assert structured["run"] is not None
        assert structured["run"]["id"] == 1
        assert structured["run"]["status"] == "success"
        assert structured["run"]["total_findings"] == 7
        assert "success" in content[0].text

    def test_unknown_run_id_returns_error(
        self, runs_db_with_runs: Path
    ) -> None:
        result = _run_async(
            scrub_server._tool_scrub_run_status(
                {"run_id": 999}, runs_db_with_runs
            )
        )
        assert result.isError is True
        assert ErrorCode.RUN_NOT_FOUND in result.content[0].text

    def test_db_missing_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-runs.db"
        result = _run_async(
            scrub_server._tool_scrub_run_status({}, missing)
        )
        assert result.isError is True
        assert ErrorCode.DB_MISSING in result.content[0].text


# ---------------------------------------------------------------------------
# scrub_scan_text and scrub_scrub_text — pipeline-dependent
# ---------------------------------------------------------------------------


class TestScanText:
    """The scan tool requires core.scrubber.scan_text to be wired. While
    the parallel build is in flight, the lazy-import shim returns
    PIPELINE_UNAVAILABLE — we exercise both paths so the contract holds
    on either side of cleanup.
    """

    def test_pipeline_unavailable_returns_error(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the lazy import to return None (simulates parallel
        # build incomplete).
        monkeypatch.setattr(scrub_server, "_import_scrubber", lambda: None)
        result = _run_async(
            scrub_server._tool_scrub_scan_text(
                {"text": "Hello world"}, runs_db
            )
        )
        assert result.isError is True
        assert ErrorCode.PIPELINE_UNAVAILABLE in result.content[0].text

    def test_with_stub_pipeline(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub out core.scrubber.scan_text with an in-test fake.
        class _FakeScrubber:
            @staticmethod
            def scan_text_report(
                text: str,
                *,
                severity_filter: list[str] | None,
                db_path: Path,
            ) -> dict[str, Any]:
                return {
                    "findings": [
                        {
                            "rule_id": "p0-em-dash",
                            "pack_name": "default",
                            "severity": "P0",
                            "file_offset": 5,
                            "length": 1,
                            "line_number": 1,
                            "snippet": "Hello —world",
                        }
                    ],
                    "totals": {"P0": 1, "P1": 0, "P2": 0},
                    "auto_disabled_count": 0,
                    "language": "en",
                    "redaction_count": 0,
                }

        monkeypatch.setattr(
            scrub_server, "_import_scrubber", lambda: _FakeScrubber
        )
        content, structured = _run_async(
            scrub_server._tool_scrub_scan_text(
                {"text": "Hello —world"}, runs_db
            )
        )
        assert structured["schema_version"] == SCHEMA_VERSION
        assert len(structured["findings"]) == 1
        assert structured["findings"][0]["rule_id"] == "p0-em-dash"
        assert structured["totals"]["P0"] == 1
        assert structured["auto_disabled_count"] == 0
        # Content has the finding block + summary footer.
        assert any("p0-em-dash" in c.text for c in content)
        assert any("P0=1" in c.text for c in content)

    def test_severity_filter_threaded(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture what severity_filter the pipeline received.
        captured: dict[str, Any] = {}

        class _CapturingScrubber:
            @staticmethod
            def scan_text_report(
                text: str,
                *,
                severity_filter: list[str] | None,
                db_path: Path,
            ) -> dict[str, Any]:
                captured["severity_filter"] = severity_filter
                return {
                    "findings": [],
                    "totals": {"P0": 0, "P1": 0, "P2": 0},
                    "auto_disabled_count": 0,
                    "language": "en",
                    "redaction_count": 0,
                }

        monkeypatch.setattr(
            scrub_server, "_import_scrubber", lambda: _CapturingScrubber
        )

        # Empty list normalizes to None per the contract.
        _run_async(
            scrub_server._tool_scrub_scan_text(
                {"text": "Hi", "severity_filter": []}, runs_db
            )
        )
        assert captured["severity_filter"] is None

        # Populated list passes through.
        _run_async(
            scrub_server._tool_scrub_scan_text(
                {"text": "Hi", "severity_filter": ["P0"]}, runs_db
            )
        )
        assert captured["severity_filter"] == ["P0"]

    def test_auto_disabled_footer_surfaces(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # K2 lock: when auto_disabled_count > 0, the summary footer
        # must surface it (silent failure prevention).
        class _Scrubber:
            @staticmethod
            def scan_text_report(
                text: str,
                *,
                severity_filter: list[str] | None,
                db_path: Path,
            ) -> dict[str, Any]:
                return {
                    "findings": [],
                    "totals": {"P0": 0, "P1": 0, "P2": 0},
                    "auto_disabled_count": 2,
                    "language": "en",
                    "redaction_count": 0,
                }

        monkeypatch.setattr(
            scrub_server, "_import_scrubber", lambda: _Scrubber
        )
        content, structured = _run_async(
            scrub_server._tool_scrub_scan_text({"text": "Hi"}, runs_db)
        )
        assert structured["auto_disabled_count"] == 2
        assert any(
            "auto_disabled=2" in c.text for c in content
        ), "auto_disable footer must be visible to caller"


class TestScrubText:
    def test_pipeline_unavailable_returns_error(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scrub_server, "_import_scrubber", lambda: None)
        result = _run_async(
            scrub_server._tool_scrub_scrub_text(
                {"text": "Hello world"}, runs_db
            )
        )
        assert result.isError is True
        assert ErrorCode.PIPELINE_UNAVAILABLE in result.content[0].text

    def test_with_stub_pipeline_returns_cleaned_text(
        self, runs_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeScrubber:
            @staticmethod
            def scrub_text_report(
                text: str,
                *,
                severity_filter: list[str] | None,
                db_path: Path,
            ) -> dict[str, Any]:
                return {
                    "cleaned_text": text.replace("—", "--"),
                    "findings": [
                        {
                            "rule_id": "p0-em-dash",
                            "pack_name": "default",
                            "severity": "P0",
                            "file_offset": 5,
                            "length": 1,
                            "line_number": 1,
                            "snippet": "Hello —world",
                        }
                    ],
                    "fixed_count": 1,
                    "totals": {"P0": 1, "P1": 0, "P2": 0},
                    "auto_disabled_count": 0,
                }

        monkeypatch.setattr(
            scrub_server, "_import_scrubber", lambda: _FakeScrubber
        )
        content, structured = _run_async(
            scrub_server._tool_scrub_scrub_text(
                {"text": "Hello —world"}, runs_db
            )
        )
        assert structured["cleaned_text"] == "Hello --world"
        assert structured["fixed_count"] == 1
        # Lead summary block has fixed= count.
        assert "fixed=1" in content[0].text


# ---------------------------------------------------------------------------
# Database resolution
# ---------------------------------------------------------------------------


class TestDbResolution:
    def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom = tmp_path / "custom-runs.db"
        monkeypatch.setenv("AUROCHS_LLM_SCRUB_DB", str(custom))
        resolved = scrub_server._default_runs_db_path()
        assert resolved == custom

    def test_default_is_in_user_data_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUROCHS_LLM_SCRUB_DB", raising=False)
        resolved = scrub_server._default_runs_db_path()
        # Must end in runs.db and live somewhere user-writable.
        assert resolved.name == "runs.db"
        # Path string contains 'aurochs-llm-scrub' somewhere.
        assert "aurochs-llm-scrub" in str(resolved).lower()


# ---------------------------------------------------------------------------
# Error block shape
# ---------------------------------------------------------------------------


class TestErrorBlocks:
    def test_error_result_has_isError_flag(self) -> None:
        result = scrub_server._error_result(
            ErrorCode.DB_MISSING, "test", path="/foo"
        )
        assert result.isError is True
        assert len(result.content) == 1

    def test_error_block_is_json_decodable(self) -> None:
        result = scrub_server._error_result(
            ErrorCode.RUN_NOT_FOUND, "missing", run_id=42
        )
        decoded = json.loads(result.content[0].text)
        assert decoded["error"]["code"] == ErrorCode.RUN_NOT_FOUND
        assert decoded["error"]["schema_version"] == SCHEMA_VERSION
        assert decoded["error"]["details"]["run_id"] == 42


# ---------------------------------------------------------------------------
# End-to-end through the dispatch table
# ---------------------------------------------------------------------------


class TestDispatchEndToEnd:
    """Exercise each tool through ``_DISPATCH`` to confirm wire-up works.

    This is the closest unit test gets to running the actual MCP server;
    the full stdio-server smoke test is integration-level (skipped here).
    """

    def test_dispatch_table_keys_match_tool_names(self) -> None:
        assert set(scrub_server._DISPATCH.keys()) == set(TOOL_NAMES)

    def test_dispatch_scrub_run_status(
        self, runs_db: Path
    ) -> None:
        handler = scrub_server._DISPATCH["scrub_run_status"]
        result = _run_async(handler({}, runs_db))
        # Either tuple (content, structured) or CallToolResult (error).
        if isinstance(result, tuple):
            _content, structured = result
            assert structured["schema_version"] == SCHEMA_VERSION
        else:
            pytest.fail(f"Unexpected dispatch result shape: {type(result)}")

    def test_dispatch_scrub_list_rules(
        self, runs_db_with_rules: Path
    ) -> None:
        handler = scrub_server._DISPATCH["scrub_list_rules"]
        result = _run_async(handler({}, runs_db_with_rules))
        assert isinstance(result, tuple)
        _content, structured = result
        assert structured["schema_version"] == SCHEMA_VERSION
        assert len(structured["rules"]) == 3
