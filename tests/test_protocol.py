"""Wire-protocol smoke-test (substitute for MCP Inspector).

FastMCP exposes ``mcp.list_tools()`` as the in-process equivalent of the
``tools/list`` MCP request. Running it confirms that:

- The shared ``FastMCP`` instance has all 5 tools registered.
- Each tool carries the expected ``annotations`` (readOnlyHint, etc.).
- The ``outputSchema`` is generated from the TypedDict return annotation.
- The ``inputSchema`` contains the right param names with correct required/optional split.

This is the closest we can get to ``npx @modelcontextprotocol/inspector``
without a UI.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Importing tools attaches @mcp.tool decorators.
import kibana_mcp.tools  # noqa: F401
from kibana_mcp._mcp import mcp

EXPECTED_TOOLS: dict[str, dict[str, Any]] = {
    "kibana_list_indices": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "required_params": set(),
        "optional_params": {"pattern", "include_system"},
    },
    "kibana_search_logs": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "required_params": {"index", "query"},
        "optional_params": {"time_field", "time_from", "time_to", "size", "sort_order"},
    },
    "kibana_aggregate_logs": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "required_params": {"index", "group_by"},
        "optional_params": {"query", "metric", "metric_field", "time_field", "time_from", "time_to", "size"},
    },
    "kibana_list_dashboards": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "required_params": set(),
        "optional_params": {"search", "page", "page_size"},
    },
    "kibana_get_dashboard": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "required_params": {"dashboard_id"},
        "optional_params": set(),
    },
}


@pytest.fixture(scope="module")
def listed_tools() -> list[Any]:
    """One-shot handshake equivalent: fetch the tool catalogue FastMCP exposes."""
    return asyncio.run(mcp.list_tools())


def test_all_five_tools_registered(listed_tools: list[Any]) -> None:
    names = {t.name for t in listed_tools}
    assert names == set(EXPECTED_TOOLS), (
        f"tool list mismatch.\n  registered: {sorted(names)}\n  expected:   {sorted(EXPECTED_TOOLS)}"
    )


@pytest.mark.parametrize("tool_name", list(EXPECTED_TOOLS))
def test_tool_annotations(listed_tools: list[Any], tool_name: str) -> None:
    """Every tool must carry readOnly/destructive/idempotent hints."""
    tool = next(t for t in listed_tools if t.name == tool_name)
    ann = tool.annotations
    expected = EXPECTED_TOOLS[tool_name]
    assert ann.readOnlyHint is expected["readOnlyHint"], f"{tool_name}.readOnlyHint"
    assert ann.destructiveHint is expected["destructiveHint"], f"{tool_name}.destructiveHint"
    assert ann.idempotentHint is expected["idempotentHint"], f"{tool_name}.idempotentHint"


@pytest.mark.parametrize("tool_name", list(EXPECTED_TOOLS))
def test_input_schema_shape(listed_tools: list[Any], tool_name: str) -> None:
    """Required + optional parameter sets must match the tool signatures."""
    tool = next(t for t in listed_tools if t.name == tool_name)
    schema = tool.inputSchema
    assert schema["type"] == "object"
    properties = set(schema.get("properties", {}).keys())
    required = set(schema.get("required", []))

    expected = EXPECTED_TOOLS[tool_name]
    assert required == expected["required_params"], (
        f"{tool_name}.required: got {required}, expected {expected['required_params']}"
    )
    expected_all = expected["required_params"] | expected["optional_params"]
    assert expected_all.issubset(properties), f"{tool_name}: missing properties {expected_all - properties}"


@pytest.mark.parametrize("tool_name", list(EXPECTED_TOOLS))
def test_output_schema_is_generated(listed_tools: list[Any], tool_name: str) -> None:
    """structured_output=True must produce an outputSchema for every tool."""
    tool = next(t for t in listed_tools if t.name == tool_name)
    assert tool.outputSchema is not None, f"{tool_name} has no outputSchema"
    assert tool.outputSchema.get("type") == "object", f"{tool_name} outputSchema not an object"
    assert tool.outputSchema.get("properties"), f"{tool_name} outputSchema has no properties"


def test_search_logs_required_params(listed_tools: list[Any]) -> None:
    """kibana_search_logs must require both index and query."""
    tool = next(t for t in listed_tools if t.name == "kibana_search_logs")
    required = set(tool.inputSchema.get("required", []))
    assert "index" in required
    assert "query" in required


def test_aggregate_logs_required_params(listed_tools: list[Any]) -> None:
    """kibana_aggregate_logs must require both index and group_by."""
    tool = next(t for t in listed_tools if t.name == "kibana_aggregate_logs")
    required = set(tool.inputSchema.get("required", []))
    assert "index" in required
    assert "group_by" in required


def test_get_dashboard_required_param(listed_tools: list[Any]) -> None:
    """kibana_get_dashboard must require dashboard_id."""
    tool = next(t for t in listed_tools if t.name == "kibana_get_dashboard")
    required = set(tool.inputSchema.get("required", []))
    assert "dashboard_id" in required


def test_metric_field_documented_in_aggregate(listed_tools: list[Any]) -> None:
    """metric_field description must mention 'avg', 'sum', 'min', 'max'."""
    tool = next(t for t in listed_tools if t.name == "kibana_aggregate_logs")
    props = tool.inputSchema["properties"]
    metric_field_desc = props.get("metric_field", {}).get("description", "")
    for v in ("avg", "sum", "min", "max"):
        assert v in metric_field_desc, f"'{v}' not mentioned in metric_field description"


def test_sort_order_documented(listed_tools: list[Any]) -> None:
    """sort_order description must mention both 'asc' and 'desc'."""
    tool = next(t for t in listed_tools if t.name == "kibana_search_logs")
    props = tool.inputSchema["properties"]
    sort_desc = props.get("sort_order", {}).get("description", "")
    assert "asc" in sort_desc
    assert "desc" in sort_desc
