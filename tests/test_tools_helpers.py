"""Unit tests for pure helper functions in :mod:`kibana_mcp.tools`.

These functions have no I/O — they shape raw ES/Kibana dicts into TypedDict
outputs and build Elasticsearch request bodies. They are unit-testable
directly without mocking any HTTP client.
"""

from __future__ import annotations

from kibana_mcp.tools import (
    _build_aggregation_body,
    _build_search_body,
    _format_bytes,
    _parse_epoch,
    _shape_hit,
    _size_human,
)


class TestFormatBytes:
    def test_zero_bytes(self) -> None:
        assert _format_bytes(0) == "0 B"

    def test_bytes(self) -> None:
        assert _format_bytes(512) == "512 B"
        assert _format_bytes(1023) == "1023 B"

    def test_kilobytes(self) -> None:
        assert _format_bytes(1024) == "1.0 KB"
        assert _format_bytes(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert _format_bytes(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self) -> None:
        assert _format_bytes(1024**3) == "1.00 GB"
        assert _format_bytes(int(2.5 * 1024**3)) == "2.50 GB"

    def test_none_returns_none(self) -> None:
        assert _format_bytes(None) is None  # type: ignore[arg-type]


class TestParseEpoch:
    def test_iso_string_passthrough(self) -> None:
        ts = "2026-04-18T09:00:00Z"
        assert _parse_epoch(ts) == ts

    def test_epoch_ms_string_passthrough(self) -> None:
        ts = "1713398400000"
        assert _parse_epoch(ts) == ts

    def test_none_returns_none(self) -> None:
        assert _parse_epoch(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_epoch("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _parse_epoch("   ") is None

    def test_strips_whitespace(self) -> None:
        assert _parse_epoch("  2026-04-18T00:00:00Z  ") == "2026-04-18T00:00:00Z"


class TestSizeHuman:
    def test_none_returns_none(self) -> None:
        assert _size_human(None) is None

    def test_empty_returns_none(self) -> None:
        assert _size_human("") is None

    def test_zero_bytes(self) -> None:
        assert _size_human("0b") == "0 B"

    def test_passthrough_es_size_string(self) -> None:
        # ES returns strings like '1.2gb', '300mb', '12kb' — pass through
        assert _size_human("1.2gb") == "1.2gb"
        assert _size_human("300mb") == "300mb"

    def test_na_returns_zero(self) -> None:
        assert _size_human("n/a") == "0 B"


class TestShapeHit:
    def test_full_hit(self) -> None:
        raw = {
            "_id": "abc123",
            "_index": "logs-2026.04.18",
            "_score": 1.5,
            "_source": {"@timestamp": "2026-04-18T12:00:00Z", "level": "ERROR", "message": "oops"},
        }
        hit = _shape_hit(raw, "@timestamp")
        assert hit["_id"] == "abc123"
        assert hit["_index"] == "logs-2026.04.18"
        assert hit["_score"] == 1.5
        assert hit["timestamp"] == "2026-04-18T12:00:00Z"
        assert hit["_source"]["level"] == "ERROR"

    def test_missing_timestamp_field(self) -> None:
        raw = {
            "_id": "x",
            "_index": "logs",
            "_score": 0.0,
            "_source": {"message": "hello"},
        }
        hit = _shape_hit(raw, "@timestamp")
        assert hit["timestamp"] is None

    def test_custom_time_field(self) -> None:
        raw = {
            "_id": "y",
            "_index": "logs",
            "_source": {"event_time": "2026-01-01T00:00:00Z"},
        }
        hit = _shape_hit(raw, "event_time")
        assert hit["timestamp"] == "2026-01-01T00:00:00Z"

    def test_empty_source(self) -> None:
        raw = {"_id": "z", "_index": "logs", "_source": {}}
        hit = _shape_hit(raw, "@timestamp")
        assert hit["_source"] == {}
        assert hit["timestamp"] is None

    def test_missing_source(self) -> None:
        raw = {"_id": "z", "_index": "logs"}
        hit = _shape_hit(raw, "@timestamp")
        assert hit["_source"] == {}
        assert hit["timestamp"] is None

    def test_null_score(self) -> None:
        raw = {"_id": "z", "_index": "logs", "_score": None, "_source": {}}
        hit = _shape_hit(raw, "@timestamp")
        assert hit["_score"] is None


class TestBuildSearchBody:
    def test_minimal_no_time_range(self) -> None:
        body = _build_search_body("level:ERROR", "@timestamp", None, None, 20, "desc")
        assert body["size"] == 20
        assert body["sort"] == [{"@timestamp": "desc"}]
        must = body["query"]["bool"]["must"]
        assert must[0]["query_string"]["query"] == "level:ERROR"
        assert "filter" not in body["query"]["bool"]

    def test_time_from_only(self) -> None:
        body = _build_search_body("*", "@timestamp", "2026-04-18T00:00:00Z", None, 10, "asc")
        filters = body["query"]["bool"]["filter"]
        assert len(filters) == 1
        range_clause = filters[0]["range"]["@timestamp"]
        assert range_clause["gte"] == "2026-04-18T00:00:00Z"
        assert "lte" not in range_clause

    def test_time_to_only(self) -> None:
        body = _build_search_body("*", "@timestamp", None, "2026-04-18T23:59:59Z", 10, "desc")
        filters = body["query"]["bool"]["filter"]
        range_clause = filters[0]["range"]["@timestamp"]
        assert "gte" not in range_clause
        assert range_clause["lte"] == "2026-04-18T23:59:59Z"

    def test_both_time_bounds(self) -> None:
        body = _build_search_body("*", "@timestamp", "2026-04-01T00:00:00Z", "2026-04-30T23:59:59Z", 50, "desc")
        range_clause = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        assert range_clause["gte"] == "2026-04-01T00:00:00Z"
        assert range_clause["lte"] == "2026-04-30T23:59:59Z"

    def test_custom_time_field(self) -> None:
        body = _build_search_body("*", "event_time", "2026-01-01T00:00:00Z", None, 5, "asc")
        assert body["sort"] == [{"event_time": "asc"}]
        filters = body["query"]["bool"]["filter"]
        assert "event_time" in filters[0]["range"]

    def test_sort_order_asc(self) -> None:
        body = _build_search_body("*", "@timestamp", None, None, 1, "asc")
        assert body["sort"] == [{"@timestamp": "asc"}]

    def test_epoch_ms_time_bounds(self) -> None:
        body = _build_search_body("*", "@timestamp", "1700000000000", "1700003600000", 10, "desc")
        range_clause = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        assert range_clause["gte"] == "1700000000000"
        assert range_clause["lte"] == "1700003600000"


class TestBuildAggregationBody:
    def test_count_metric_no_sub_agg(self) -> None:
        body = _build_aggregation_body("*", "level", "count", None, 10, "@timestamp", None, None)
        assert body["size"] == 0
        group_by_agg = body["aggs"]["group_by"]
        assert group_by_agg["terms"]["field"] == "level"
        assert group_by_agg["terms"]["size"] == 10
        assert "aggs" not in group_by_agg

    def test_avg_metric_adds_sub_agg(self) -> None:
        body = _build_aggregation_body("*", "service.keyword", "avg", "response_ms", 5, "@timestamp", None, None)
        group_by_agg = body["aggs"]["group_by"]
        assert "aggs" in group_by_agg
        assert "metric_value" in group_by_agg["aggs"]
        assert "avg" in group_by_agg["aggs"]["metric_value"]
        assert group_by_agg["aggs"]["metric_value"]["avg"]["field"] == "response_ms"

    def test_sum_metric_sub_agg(self) -> None:
        body = _build_aggregation_body("service:api", "host", "sum", "bytes_sent", 20, "@timestamp", None, None)
        assert body["aggs"]["group_by"]["aggs"]["metric_value"]["sum"]["field"] == "bytes_sent"

    def test_min_metric_sub_agg(self) -> None:
        body = _build_aggregation_body("*", "host", "min", "response_ms", 10, "@timestamp", None, None)
        assert "min" in body["aggs"]["group_by"]["aggs"]["metric_value"]

    def test_max_metric_sub_agg(self) -> None:
        body = _build_aggregation_body("*", "host", "max", "cpu_pct", 10, "@timestamp", None, None)
        assert "max" in body["aggs"]["group_by"]["aggs"]["metric_value"]

    def test_with_time_range(self) -> None:
        body = _build_aggregation_body(
            "*", "level", "count", None, 10, "@timestamp", "2026-04-18T00:00:00Z", "2026-04-18T23:59:59Z"
        )
        filters = body["query"]["bool"]["filter"]
        range_clause = filters[0]["range"]["@timestamp"]
        assert range_clause["gte"] == "2026-04-18T00:00:00Z"
        assert range_clause["lte"] == "2026-04-18T23:59:59Z"

    def test_no_time_range_no_filter_key(self) -> None:
        body = _build_aggregation_body("*", "level", "count", None, 10, "@timestamp", None, None)
        assert "filter" not in body["query"]["bool"]

    def test_query_filter_forwarded(self) -> None:
        body = _build_aggregation_body("level:ERROR", "service", "count", None, 5, "@timestamp", None, None)
        must = body["query"]["bool"]["must"]
        assert must[0]["query_string"]["query"] == "level:ERROR"

    def test_size_forwarded_to_terms(self) -> None:
        body = _build_aggregation_body("*", "level", "count", None, 42, "@timestamp", None, None)
        assert body["aggs"]["group_by"]["terms"]["size"] == 42
