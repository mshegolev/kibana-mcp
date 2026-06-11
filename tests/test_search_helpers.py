"""Unit tests for :mod:`kibana_mcp.search_helpers`.

Verifies that search_helpers is importable without FastMCP/mcp imports and
that all 10 extracted items (2 constants + 8 functions) are accessible and
behave identically to their original definitions in tools.py.
"""

from __future__ import annotations

import importlib
import sys

import pytest


class TestNoFrameworkImports:
    """search_helpers must not pull in FastMCP or mcp server machinery."""

    def test_import_search_helpers_no_fastmcp(self) -> None:
        """Importing search_helpers must not cause FastMCP or _mcp to load.

        We measure which modules are *newly* loaded by the import, by capturing
        sys.modules before and after. Modules loaded by other tests before this
        one are not attributed to search_helpers.
        """
        # Remove cached search_helpers so we can re-import it cleanly
        for mod in list(sys.modules.keys()):
            if "search_helpers" in mod:
                del sys.modules[mod]

        before = set(sys.modules.keys())
        importlib.import_module("kibana_mcp.search_helpers")
        newly_loaded = set(sys.modules.keys()) - before

        # FastMCP / mcp server must NOT have been imported as a new side effect
        for mod in newly_loaded:
            assert "fastmcp" not in mod.lower(), (
                f"FastMCP was newly imported as side effect: {mod}"
            )
            assert mod != "kibana_mcp._mcp", (
                "kibana_mcp._mcp was newly imported as side effect"
            )


class TestAllItemsImportable:
    """All 10 items must be importable from kibana_mcp.search_helpers."""

    def test_constants_importable(self) -> None:
        from kibana_mcp.search_helpers import (
            _INDEX_FORBIDDEN_CHARS,
            _SYSTEM_INDEX_PREFIXES,
        )
        assert isinstance(_SYSTEM_INDEX_PREFIXES, tuple)
        assert isinstance(_INDEX_FORBIDDEN_CHARS, tuple)

    def test_functions_importable(self) -> None:
        from kibana_mcp.search_helpers import (
            _build_aggregation_body,
            _build_search_body,
            _format_bytes,
            _is_system_index,
            _parse_epoch,
            _shape_hit,
            _size_human,
            _validate_index_pattern,
        )
        assert callable(_validate_index_pattern)
        assert callable(_parse_epoch)
        assert callable(_build_search_body)
        assert callable(_build_aggregation_body)
        assert callable(_shape_hit)
        assert callable(_is_system_index)
        assert callable(_format_bytes)
        assert callable(_size_human)


class TestValidateIndexPatternBehavior:
    """_validate_index_pattern must preserve security invariants."""

    def test_valid_pattern_returned_unchanged(self) -> None:
        from kibana_mcp.search_helpers import _validate_index_pattern
        assert _validate_index_pattern("logs-*") == "logs-*"

    def test_forbidden_slash_raises(self) -> None:
        from kibana_mcp.search_helpers import _validate_index_pattern
        with pytest.raises(ValueError):
            _validate_index_pattern("logs/_delete?")

    def test_leading_underscore_segment_raises(self) -> None:
        from kibana_mcp.search_helpers import _validate_index_pattern
        with pytest.raises(ValueError):
            _validate_index_pattern("_internal")


class TestParseEpochBehavior:
    """_parse_epoch tolerant pass-through."""

    def test_none_returns_none(self) -> None:
        from kibana_mcp.search_helpers import _parse_epoch
        assert _parse_epoch(None) is None

    def test_iso_string_returned_unchanged(self) -> None:
        from kibana_mcp.search_helpers import _parse_epoch
        result = _parse_epoch("2026-01-01T00:00:00Z")
        assert result == "2026-01-01T00:00:00Z"


class TestBuildSearchBodyBehavior:
    """_build_search_body constructs correct ES DSL."""

    def test_no_time_range_no_filter_key(self) -> None:
        from kibana_mcp.search_helpers import _build_search_body
        body = _build_search_body("level:ERROR", "@timestamp", None, None, 20, "desc")
        must = body["query"]["bool"]["must"]
        assert must[0]["query_string"]["query"] == "level:ERROR"
        assert "filter" not in body["query"]["bool"]

    def test_time_from_adds_filter(self) -> None:
        from kibana_mcp.search_helpers import _build_search_body
        body = _build_search_body(
            "*", "@timestamp", "2026-01-01T00:00:00Z", None, 10, "asc"
        )
        rng = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        assert rng["gte"] == "2026-01-01T00:00:00Z"


class TestBuildAggregationBodyBehavior:
    """_build_aggregation_body constructs size=0 ES DSL with terms agg."""

    def test_size_zero_and_terms_field(self) -> None:
        from kibana_mcp.search_helpers import _build_aggregation_body
        body = _build_aggregation_body(
            "*", "level", "count", None, 10, "@timestamp", None, None
        )
        assert body["size"] == 0
        assert body["aggs"]["group_by"]["terms"]["field"] == "level"


class TestShapeHitBehavior:
    """_shape_hit extracts timestamp from _source."""

    def test_timestamp_extracted(self) -> None:
        from kibana_mcp.search_helpers import _shape_hit
        raw = {
            "_id": "1",
            "_index": "logs",
            "_score": 1.0,
            "_source": {
                "@timestamp": "2026-06-12T10:00:00Z",
                "msg": "test",
            },
        }
        hit = _shape_hit(raw, "@timestamp")
        assert hit["timestamp"] == "2026-06-12T10:00:00Z"
        assert hit["_id"] == "1"
