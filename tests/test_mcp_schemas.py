"""Tests for MCP schema definitions (no SDK required).

These tests validate the contract surface in
``aurochs_llm_scrub.mcp.schemas`` without importing the Anthropic mcp
SDK — they exercise pure-Python dicts. They run on installs without the
``[mcp]`` extra.
"""

from __future__ import annotations

from aurochs_llm_scrub.mcp.schemas import (
    INPUT_SCHEMAS,
    OUTPUT_SHAPES,
    SCHEMA_VERSION,
    SEVERITY_VALUES,
    TOOL_NAMES,
    ErrorCode,
)


class TestSchemaCompleteness:
    def test_every_tool_has_input_schema(self) -> None:
        for name in TOOL_NAMES:
            assert name in INPUT_SCHEMAS

    def test_every_tool_has_output_shape(self) -> None:
        for name in TOOL_NAMES:
            assert name in OUTPUT_SHAPES

    def test_no_orphan_schemas(self) -> None:
        for name in INPUT_SCHEMAS:
            assert name in TOOL_NAMES
        for name in OUTPUT_SHAPES:
            assert name in TOOL_NAMES


class TestInputSchemas:
    def test_all_strict_no_additional_properties(self) -> None:
        for name, schema in INPUT_SCHEMAS.items():
            assert schema.get("type") == "object", (
                f"{name} schema must be type=object"
            )
            assert schema.get("additionalProperties") is False, (
                f"{name} schema must set additionalProperties=false"
            )
            assert "required" in schema, (
                f"{name} schema must declare required (even if empty)"
            )

    def test_scrub_scan_text_requires_text(self) -> None:
        schema = INPUT_SCHEMAS["scrub_scan_text"]
        assert schema["required"] == ["text"]
        text = schema["properties"]["text"]
        assert text["type"] == "string"
        assert text["minLength"] == 1
        assert text["maxLength"] == 1_048_576

    def test_scrub_scan_text_severity_filter_optional(self) -> None:
        schema = INPUT_SCHEMAS["scrub_scan_text"]
        sev = schema["properties"]["severity_filter"]
        assert sev["type"] == ["array", "null"]
        assert sev["default"] is None
        assert set(sev["items"]["enum"]) == set(SEVERITY_VALUES)

    def test_scrub_list_rules_pack_name_pattern(self) -> None:
        schema = INPUT_SCHEMAS["scrub_list_rules"]
        pack = schema["properties"]["pack_name"]
        assert pack["type"] == ["string", "null"]
        assert pack["pattern"] == "^[a-z0-9][a-z0-9-]*$"

    def test_scrub_run_status_run_id_optional(self) -> None:
        schema = INPUT_SCHEMAS["scrub_run_status"]
        run_id = schema["properties"]["run_id"]
        assert run_id["type"] == ["integer", "null"]
        assert run_id["default"] is None
        assert run_id["minimum"] == 1


class TestOutputShapes:
    def test_all_have_schema_version(self) -> None:
        for name, shape in OUTPUT_SHAPES.items():
            assert shape.get("schema_version") == SCHEMA_VERSION, (
                f"{name} output shape missing schema_version"
            )

    def test_scan_text_output_has_findings_and_totals(self) -> None:
        shape = OUTPUT_SHAPES["scrub_scan_text"]
        assert "findings" in shape["properties"]
        assert "totals" in shape["properties"]
        assert "auto_disabled_count" in shape["properties"]


class TestErrorCodes:
    def test_error_code_constants_present(self) -> None:
        # All error codes the server can emit.
        assert ErrorCode.INPUT_VALIDATION == "INPUT_VALIDATION"
        assert ErrorCode.DB_MISSING == "DB_MISSING"
        assert ErrorCode.RUN_NOT_FOUND == "RUN_NOT_FOUND"
        assert ErrorCode.PACK_NOT_FOUND == "PACK_NOT_FOUND"
        assert ErrorCode.PIPELINE_UNAVAILABLE == "PIPELINE_UNAVAILABLE"
        assert ErrorCode.INTERNAL_ERROR == "INTERNAL_ERROR"


class TestSchemaVersion:
    def test_schema_version_is_locked_for_v0_1(self) -> None:
        assert SCHEMA_VERSION == "0.1.0"

    def test_severity_values_locked(self) -> None:
        assert SEVERITY_VALUES == ("P0", "P1", "P2")
