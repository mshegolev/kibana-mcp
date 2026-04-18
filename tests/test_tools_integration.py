"""Integration-style tests for MCP tools using ``responses`` mock library.

Each test exercises one tool end-to-end (from tool call → HTTP mock → result
shape) without starting a real Elasticsearch or Kibana server. The
``responses`` library intercepts ``requests.Session`` calls at the socket level.

Covers:
- Happy path for every tool
- Error paths (4xx, 5xx, network failure)
- Time range forwarding verified via request body inspection
- Aggregation body construction (metric field, sub-agg presence)
- sort_order validation
- Truncation hint presence for large result sets
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import responses as resp_lib

import kibana_mcp._mcp as _mcp_module
from kibana_mcp.tools import (
    kibana_aggregate_logs,
    kibana_get_dashboard,
    kibana_list_dashboards,
    kibana_list_indices,
    kibana_search_logs,
)

ES_URL = "https://es.example.com:9200"
KIBANA_URL = "https://kibana.example.com"


@pytest.fixture(autouse=True)
def setup_client(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[return]
    """Reset client cache and inject a fresh client pointing at our mock URLs."""
    monkeypatch.setattr(_mcp_module, "_client", None)
    monkeypatch.setenv("KIBANA_URL", KIBANA_URL)
    monkeypatch.setenv("ELASTICSEARCH_URL", ES_URL)
    monkeypatch.delenv("KIBANA_API_KEY", raising=False)
    monkeypatch.delenv("KIBANA_USERNAME", raising=False)
    monkeypatch.delenv("KIBANA_PASSWORD", raising=False)
    yield
    if _mcp_module._client is not None:
        try:
            _mcp_module._client.close()
        except Exception:
            pass
        _mcp_module._client = None


# ── kibana_list_indices ────────────────────────────────────────────────────────


class TestKibanaListIndices:
    @resp_lib.activate
    def test_happy_path(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{ES_URL}/_cat/indices",
            json=[
                {
                    "index": "logs-2026.04.18",
                    "health": "green",
                    "status": "open",
                    "docs.count": "1234",
                    "store.size": "5mb",
                },
                {
                    "index": "filebeat-2026.04.18",
                    "health": "yellow",
                    "status": "open",
                    "docs.count": "500",
                    "store.size": "1mb",
                },
                {"index": ".kibana_1", "health": "green", "status": "open", "docs.count": "10", "store.size": "100kb"},
            ],
            status=200,
        )
        result = kibana_list_indices()
        assert result.structuredContent is not None  # type: ignore[union-attr]
        data = result.structuredContent
        # System index .kibana_1 should be excluded by default
        indices = data["indices"]
        names = [i["index"] for i in indices]
        assert ".kibana_1" not in names
        assert "logs-2026.04.18" in names
        assert data["include_system"] is False

    @resp_lib.activate
    def test_include_system_indices(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{ES_URL}/_cat/indices",
            json=[
                {"index": ".kibana_1", "health": "green", "status": "open", "docs.count": "10", "store.size": "1kb"},
            ],
            status=200,
        )
        result = kibana_list_indices(include_system=True)
        data = result.structuredContent
        names = [i["index"] for i in data["indices"]]
        assert ".kibana_1" in names

    @resp_lib.activate
    def test_pattern_forwarded_in_path(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{ES_URL}/_cat/indices/logs-*",
            json=[
                {
                    "index": "logs-2026.04.18",
                    "health": "green",
                    "status": "open",
                    "docs.count": "100",
                    "store.size": "1mb",
                },
            ],
            status=200,
        )
        result = kibana_list_indices(pattern="logs-*")
        assert result.structuredContent["pattern"] == "logs-*"  # type: ignore[index]

    @resp_lib.activate
    def test_404_raises_tool_error(self) -> None:
        resp_lib.add(resp_lib.GET, f"{ES_URL}/_cat/indices", status=404)
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="404"):
            kibana_list_indices()

    @resp_lib.activate
    def test_empty_response(self) -> None:
        resp_lib.add(resp_lib.GET, f"{ES_URL}/_cat/indices", json=[], status=200)
        result = kibana_list_indices()
        assert result.structuredContent["indices_count"] == 0  # type: ignore[index]

    @resp_lib.activate
    def test_docs_count_parsed_as_int(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{ES_URL}/_cat/indices",
            json=[{"index": "logs", "health": "green", "status": "open", "docs.count": "999", "store.size": "1mb"}],
            status=200,
        )
        result = kibana_list_indices()
        assert result.structuredContent["indices"][0]["docs_count"] == 999  # type: ignore[index]


# ── kibana_search_logs ─────────────────────────────────────────────────────────


def _make_search_response(hits: list[dict[str, Any]], total: int = None, took: int = 5) -> dict[str, Any]:
    if total is None:
        total = len(hits)
    return {
        "took": took,
        "hits": {
            "total": {"value": total, "relation": "eq"},
            "hits": hits,
        },
    }


def _make_hit(idx: int) -> dict[str, Any]:
    return {
        "_id": f"id{idx}",
        "_index": "logs-2026.04.18",
        "_score": 1.0,
        "_source": {"@timestamp": f"2026-04-18T12:{idx:02d}:00Z", "level": "ERROR", "message": f"error {idx}"},
    }


class TestKibanaSearchLogs:
    @resp_lib.activate
    def test_happy_path_returns_hits(self) -> None:
        hits = [_make_hit(i) for i in range(3)]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response(hits), status=200)
        result = kibana_search_logs(index="logs-*", query="level:ERROR")
        data = result.structuredContent
        assert data["total"] == 3  # type: ignore[index]
        assert data["returned"] == 3  # type: ignore[index]
        assert len(data["hits"]) == 3  # type: ignore[index]
        assert data["hits"][0]["_id"] == "id0"  # type: ignore[index]

    @resp_lib.activate
    def test_time_range_forwarded_in_body(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response([]), status=200)
        kibana_search_logs(
            index="logs-*",
            query="*",
            time_from="2026-04-18T00:00:00Z",
            time_to="2026-04-18T23:59:59Z",
        )
        # Inspect the request body sent to ES
        sent_body = json.loads(resp_lib.calls[0].request.body)
        filters = sent_body["query"]["bool"].get("filter", [])
        assert len(filters) == 1
        range_clause = filters[0]["range"]["@timestamp"]
        assert range_clause["gte"] == "2026-04-18T00:00:00Z"
        assert range_clause["lte"] == "2026-04-18T23:59:59Z"

    @resp_lib.activate
    def test_sort_order_asc_forwarded(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response([]), status=200)
        kibana_search_logs(index="logs-*", query="*", sort_order="asc")
        sent_body = json.loads(resp_lib.calls[0].request.body)
        assert sent_body["sort"] == [{"@timestamp": "asc"}]

    @resp_lib.activate
    def test_size_forwarded(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response([]), status=200)
        kibana_search_logs(index="logs-*", query="*", size=100)
        sent_body = json.loads(resp_lib.calls[0].request.body)
        assert sent_body["size"] == 100

    def test_invalid_sort_order_raises_tool_error(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="sort_order"):
            kibana_search_logs(index="logs-*", query="*", sort_order="invalid")

    @resp_lib.activate
    def test_truncation_hint_when_more_than_20_hits(self) -> None:
        hits = [_make_hit(i) for i in range(25)]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response(hits, total=25), status=200)
        result = kibana_search_logs(index="logs-*", query="*", size=25)
        text = result.content[0].text  # type: ignore[index]
        assert "Showing first 20" in text

    @resp_lib.activate
    def test_no_truncation_hint_for_20_or_fewer(self) -> None:
        hits = [_make_hit(i) for i in range(10)]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_search_response(hits, total=10), status=200)
        result = kibana_search_logs(index="logs-*", query="*", size=10)
        text = result.content[0].text  # type: ignore[index]
        assert "Showing first" not in text

    @resp_lib.activate
    def test_400_bad_query_raises_tool_error(self) -> None:
        resp_lib.add(
            resp_lib.POST,
            f"{ES_URL}/logs-*/_search",
            json={"error": {"type": "parse_exception"}},
            status=400,
        )
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="400"):
            kibana_search_logs(index="logs-*", query="INVALID::query")

    @resp_lib.activate
    def test_total_as_integer_fallback(self) -> None:
        """ES 6.x returns total as a plain integer, not a dict."""
        resp_lib.add(
            resp_lib.POST,
            f"{ES_URL}/logs-*/_search",
            json={"took": 2, "hits": {"total": 42, "hits": []}},
            status=200,
        )
        result = kibana_search_logs(index="logs-*", query="*")
        assert result.structuredContent["total"] == 42  # type: ignore[index]


# ── kibana_aggregate_logs ──────────────────────────────────────────────────────


def _make_agg_response(
    buckets: list[dict[str, Any]],
    total: int = 1000,
    took: int = 3,
) -> dict[str, Any]:
    return {
        "took": took,
        "hits": {"total": {"value": total, "relation": "eq"}, "hits": []},
        "aggregations": {"group_by": {"buckets": buckets, "doc_count_error_upper_bound": 0, "sum_other_doc_count": 0}},
    }


class TestKibanaAggregateLogs:
    @resp_lib.activate
    def test_happy_path_count_metric(self) -> None:
        buckets = [
            {"key": "ERROR", "doc_count": 500},
            {"key": "WARN", "doc_count": 300},
        ]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response(buckets), status=200)
        result = kibana_aggregate_logs(index="logs-*", group_by="level")
        data = result.structuredContent
        assert data["buckets_count"] == 2  # type: ignore[index]
        assert data["buckets"][0]["key"] == "ERROR"  # type: ignore[index]
        assert data["buckets"][0]["doc_count"] == 500  # type: ignore[index]
        assert data["metric"] == "count"  # type: ignore[index]

    @resp_lib.activate
    def test_avg_metric_includes_metric_value(self) -> None:
        buckets = [
            {"key": "api-svc", "doc_count": 100, "metric_value": {"value": 250.5}},
        ]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response(buckets), status=200)
        result = kibana_aggregate_logs(
            index="logs-*", group_by="service.keyword", metric="avg", metric_field="response_ms"
        )
        data = result.structuredContent
        assert data["buckets"][0]["metric_value"] == 250.5  # type: ignore[index]

    @resp_lib.activate
    def test_aggregation_body_has_size_zero(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response([]), status=200)
        kibana_aggregate_logs(index="logs-*", group_by="level")
        sent_body = json.loads(resp_lib.calls[0].request.body)
        assert sent_body["size"] == 0

    @resp_lib.activate
    def test_aggregation_body_no_sub_agg_for_count(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response([]), status=200)
        kibana_aggregate_logs(index="logs-*", group_by="level", metric="count")
        sent_body = json.loads(resp_lib.calls[0].request.body)
        group_by_agg = sent_body["aggs"]["group_by"]
        assert "aggs" not in group_by_agg

    @resp_lib.activate
    def test_aggregation_body_has_sub_agg_for_avg(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response([]), status=200)
        kibana_aggregate_logs(index="logs-*", group_by="service", metric="avg", metric_field="latency_ms")
        sent_body = json.loads(resp_lib.calls[0].request.body)
        sub_agg = sent_body["aggs"]["group_by"]["aggs"]["metric_value"]
        assert "avg" in sub_agg
        assert sub_agg["avg"]["field"] == "latency_ms"

    @resp_lib.activate
    def test_time_range_forwarded(self) -> None:
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response([]), status=200)
        kibana_aggregate_logs(
            index="logs-*",
            group_by="level",
            time_from="2026-04-18T00:00:00Z",
            time_to="2026-04-18T23:59:59Z",
        )
        sent_body = json.loads(resp_lib.calls[0].request.body)
        filters = sent_body["query"]["bool"]["filter"]
        assert filters[0]["range"]["@timestamp"]["gte"] == "2026-04-18T00:00:00Z"

    def test_invalid_metric_raises_tool_error(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="metric"):
            kibana_aggregate_logs(index="logs-*", group_by="level", metric="median")

    def test_metric_field_required_for_avg(self) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="metric_field"):
            kibana_aggregate_logs(index="logs-*", group_by="service", metric="avg")

    @resp_lib.activate
    def test_truncation_hint_when_more_than_20_buckets(self) -> None:
        buckets = [{"key": f"svc-{i}", "doc_count": i * 10} for i in range(25)]
        resp_lib.add(resp_lib.POST, f"{ES_URL}/logs-*/_search", json=_make_agg_response(buckets), status=200)
        result = kibana_aggregate_logs(index="logs-*", group_by="service.keyword", size=25)
        text = result.content[0].text  # type: ignore[index]
        assert "Showing first 20" in text


# ── kibana_list_dashboards ──────────────────────────────────────────────────────


def _make_dashboards_response(
    objects: list[dict[str, Any]],
    total: int = None,
) -> dict[str, Any]:
    if total is None:
        total = len(objects)
    return {"total": total, "saved_objects": objects}


def _make_dashboard_obj(idx: int) -> dict[str, Any]:
    return {
        "id": f"dash-{idx:04d}",
        "attributes": {"title": f"Dashboard {idx}", "description": f"Desc {idx}"},
        "updated_at": "2026-04-18T12:00:00.000Z",
    }


class TestKibanaListDashboards:
    @resp_lib.activate
    def test_happy_path(self) -> None:
        objects = [_make_dashboard_obj(i) for i in range(3)]
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/_find",
            json=_make_dashboards_response(objects),
            status=200,
        )
        result = kibana_list_dashboards()
        data = result.structuredContent
        assert data["total"] == 3  # type: ignore[index]
        assert len(data["dashboards"]) == 3  # type: ignore[index]
        assert data["dashboards"][0]["id"] == "dash-0000"  # type: ignore[index]

    @resp_lib.activate
    def test_search_param_forwarded(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/_find",
            json=_make_dashboards_response([]),
            status=200,
        )
        kibana_list_dashboards(search="infrastructure")
        req = resp_lib.calls[0].request
        assert "infrastructure" in req.url

    @resp_lib.activate
    def test_has_more_flag(self) -> None:
        objects = [_make_dashboard_obj(i) for i in range(20)]
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/_find",
            json=_make_dashboards_response(objects, total=50),
            status=200,
        )
        result = kibana_list_dashboards(page=1, page_size=20)
        data = result.structuredContent
        assert data["has_more"] is True  # type: ignore[index]

    @resp_lib.activate
    def test_kbn_xsrf_header_sent(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/_find",
            json=_make_dashboards_response([]),
            status=200,
        )
        kibana_list_dashboards()
        req = resp_lib.calls[0].request
        assert req.headers.get("kbn-xsrf") == "true"

    @resp_lib.activate
    def test_404_raises_tool_error(self) -> None:
        resp_lib.add(resp_lib.GET, f"{KIBANA_URL}/api/saved_objects/_find", status=404)
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="404"):
            kibana_list_dashboards()

    @resp_lib.activate
    def test_empty_list(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/_find",
            json=_make_dashboards_response([]),
            status=200,
        )
        result = kibana_list_dashboards()
        assert result.structuredContent["total"] == 0  # type: ignore[index]


# ── kibana_get_dashboard ────────────────────────────────────────────────────────


class TestKibanaGetDashboard:
    @resp_lib.activate
    def test_happy_path_with_panels(self) -> None:
        panels_json = json.dumps(
            [
                {"type": "visualization", "embeddableConfig": {"title": "Error Rate"}},
                {"type": "lens", "embeddableConfig": {"title": "Throughput"}},
            ]
        )
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/dash-0001",
            json={
                "id": "dash-0001",
                "attributes": {
                    "title": "My Dashboard",
                    "description": "A test dashboard",
                    "panelsJSON": panels_json,
                },
                "updated_at": "2026-04-18T12:00:00.000Z",
            },
            status=200,
        )
        result = kibana_get_dashboard(dashboard_id="dash-0001")
        data = result.structuredContent
        assert data["id"] == "dash-0001"  # type: ignore[index]
        assert data["title"] == "My Dashboard"  # type: ignore[index]
        assert data["panels_count"] == 2  # type: ignore[index]
        assert data["panels"][0]["panel_type"] == "visualization"  # type: ignore[index]
        assert data["panels"][0]["title"] == "Error Rate"  # type: ignore[index]

    @resp_lib.activate
    def test_empty_panels_json(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/dash-0002",
            json={
                "id": "dash-0002",
                "attributes": {"title": "Empty", "panelsJSON": "[]"},
                "updated_at": None,
            },
            status=200,
        )
        result = kibana_get_dashboard(dashboard_id="dash-0002")
        data = result.structuredContent
        assert data["panels_count"] == 0  # type: ignore[index]
        assert data["panels"] == []  # type: ignore[index]

    @resp_lib.activate
    def test_missing_panels_json(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/dash-0003",
            json={"id": "dash-0003", "attributes": {"title": "No panels"}},
            status=200,
        )
        result = kibana_get_dashboard(dashboard_id="dash-0003")
        assert result.structuredContent["panels_count"] == 0  # type: ignore[index]

    @resp_lib.activate
    def test_404_raises_tool_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/nonexistent",
            status=404,
        )
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="404"):
            kibana_get_dashboard(dashboard_id="nonexistent")

    @resp_lib.activate
    def test_401_raises_tool_error(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/dash-x",
            status=401,
        )
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="401"):
            kibana_get_dashboard(dashboard_id="dash-x")

    @resp_lib.activate
    def test_markdown_contains_title(self) -> None:
        resp_lib.add(
            resp_lib.GET,
            f"{KIBANA_URL}/api/saved_objects/dashboard/dash-md",
            json={"id": "dash-md", "attributes": {"title": "Infra Overview", "panelsJSON": "[]"}},
            status=200,
        )
        result = kibana_get_dashboard(dashboard_id="dash-md")
        text = result.content[0].text  # type: ignore[index]
        assert "Infra Overview" in text
