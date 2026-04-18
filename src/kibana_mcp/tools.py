"""MCP tools for Kibana / Elasticsearch.

5 read-only tools covering log search and Kibana dashboard surface:

- ``kibana_list_indices``    — discover available ES indices
- ``kibana_search_logs``     — full-text log search with time range
- ``kibana_aggregate_logs``  — group-by / metric aggregation
- ``kibana_list_dashboards`` — list Kibana saved dashboards
- ``kibana_get_dashboard``   — fetch a single dashboard with panel detail

**Threading model.** All tools are synchronous ``def``. FastMCP runs them
in a worker thread via ``anyio.to_thread.run_sync``, so blocking HTTP
calls don't block the asyncio event loop.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field

from kibana_mcp import output
from kibana_mcp._mcp import get_client, mcp
from kibana_mcp.models import (
    AggregateBucket,
    AggregateOutput,
    DashboardDetail,
    DashboardPanel,
    DashboardsListOutput,
    DashboardSummary,
    IndexSummary,
    IndicesListOutput,
    LogHit,
    SearchOutput,
)

# ── Pure helper functions (unit-testable without HTTP) ────────────────────────

_SYSTEM_INDEX_PREFIXES = (".", "kibana", "ilm-history", "shrink-")


def _format_bytes(size_bytes: int | None) -> str | None:
    """Format a byte count as a human-readable string (GB / MB / KB / B).

    Returns ``None`` if ``size_bytes`` is ``None``.
    """
    if size_bytes is None:
        return None
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.2f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _parse_epoch(ts: str | None) -> str | None:
    """Return ``ts`` unchanged if set, else ``None``.

    Accepts both ISO-8601 strings (``"2026-01-01T00:00:00Z"``) and
    epoch-ms integers encoded as strings (``"1700000000000"``). Both are
    valid Elasticsearch range filter values — we pass them through as-is.
    """
    if not ts:
        return None
    return str(ts).strip() or None


def _size_human(size_str: str | None) -> str | None:
    """Parse Elasticsearch ``_cat/indices`` ``store.size`` string (e.g. ``'1.2gb'``)
    and return a normalised human-readable representation, or ``None`` on failure.
    """
    if not size_str:
        return None
    cleaned = size_str.strip().lower()
    # ES may already return 'N/A' for empty shards
    if cleaned in ("", "n/a", "0b", "0"):
        return "0 B"
    return size_str.strip()


def _shape_hit(raw: dict[str, Any], time_field: str) -> LogHit:
    """Convert a raw Elasticsearch hit dict into a :class:`LogHit`."""
    source: dict = raw.get("_source") or {}
    return {
        "_id": raw.get("_id", ""),
        "_index": raw.get("_index", ""),
        "_score": raw.get("_score"),
        "_source": source,
        "timestamp": source.get(time_field) if source else None,
    }


def _build_search_body(
    query: str,
    time_field: str,
    time_from: str | None,
    time_to: str | None,
    size: int,
    sort_order: str,
) -> dict[str, Any]:
    """Build an Elasticsearch search request body.

    Uses ``query_string`` wrapped in a ``bool/must`` with an optional
    ``range`` filter when time bounds are provided.

    Args:
        query: Elasticsearch Query String Syntax expression.
        time_field: Name of the timestamp field (e.g. ``@timestamp``).
        time_from: Lower bound in ISO-8601 or epoch-ms (``None`` = unbounded).
        time_to: Upper bound in ISO-8601 or epoch-ms (``None`` = unbounded).
        size: Number of hits to return (1-500).
        sort_order: ``"asc"`` or ``"desc"``.

    Returns:
        A dict ready to be serialised and POSTed to ``/{index}/_search``.
    """
    must: list[dict[str, Any]] = [{"query_string": {"query": query}}]
    body: dict[str, Any] = {
        "size": size,
        "query": {"bool": {"must": must}},
        "sort": [{time_field: sort_order}],
    }

    if time_from or time_to:
        range_filter: dict[str, Any] = {}
        if time_from:
            range_filter["gte"] = time_from
        if time_to:
            range_filter["lte"] = time_to
        body["query"]["bool"]["filter"] = [{"range": {time_field: range_filter}}]

    return body


def _build_aggregation_body(
    query: str,
    group_by: str,
    metric: str,
    metric_field: str | None,
    size: int,
    time_field: str,
    time_from: str | None,
    time_to: str | None,
) -> dict[str, Any]:
    """Build an Elasticsearch aggregation request body.

    Sets ``size:0`` to avoid returning hits. The aggregation uses a
    ``terms`` bucket on ``group_by``, with an optional sub-aggregation for
    non-count metrics.

    Args:
        query: Elasticsearch Query String Syntax filter (use ``"*"`` for all).
        group_by: Field name for ``terms`` aggregation.
        metric: ``"count"`` | ``"avg"`` | ``"sum"`` | ``"min"`` | ``"max"``.
        metric_field: Field to apply ``metric`` on (required for non-count metrics).
        size: Number of terms buckets to return (1-100).
        time_field: Name of the timestamp field.
        time_from: Lower bound (``None`` = unbounded).
        time_to: Upper bound (``None`` = unbounded).

    Returns:
        A dict ready to be serialised and POSTed to ``/{index}/_search``.
    """
    must: list[dict[str, Any]] = [{"query_string": {"query": query}}]
    body: dict[str, Any] = {
        "size": 0,
        "query": {"bool": {"must": must}},
    }

    if time_from or time_to:
        range_filter: dict[str, Any] = {}
        if time_from:
            range_filter["gte"] = time_from
        if time_to:
            range_filter["lte"] = time_to
        body["query"]["bool"]["filter"] = [{"range": {time_field: range_filter}}]

    terms_agg: dict[str, Any] = {"terms": {"field": group_by, "size": size}}

    if metric != "count" and metric_field:
        terms_agg["aggs"] = {"metric_value": {metric: {"field": metric_field}}}

    body["aggs"] = {"group_by": terms_agg}
    return body


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool(
    name="kibana_list_indices",
    annotations={
        "title": "List Elasticsearch Indices",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def kibana_list_indices(
    pattern: Annotated[
        str,
        Field(
            default="*",
            max_length=500,
            description=(
                "Index name or pattern to filter results (e.g. 'logs-*', 'filebeat-*'). "
                "Supports Elasticsearch wildcard syntax. Default '*' lists all non-system indices."
            ),
        ),
    ] = "*",
    include_system: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether to include system/internal indices (those starting with '.'). "
                "Default False — system indices are hidden to avoid noise."
            ),
        ),
    ] = False,
) -> IndicesListOutput:
    """List available Elasticsearch indices.

    Calls ``GET {ES_URL}/_cat/indices?format=json`` and returns a structured
    list of indices with health, status, document count, and storage size.
    Use this first to discover which index names / patterns exist before
    calling ``kibana_search_logs`` or ``kibana_aggregate_logs``.

    Examples:
        - Use when: "What log indices are available in Elasticsearch?"
          → default params, ``pattern='*'``.
        - Use when: The user mentions a service name but not the index.
          Try ``pattern='logs-myservice-*'`` to narrow down.
        - Use when: "How many documents in the access-log index?"
          → ``pattern='access-log*'``, check ``docs_count``.
        - Don't use when: You already know the index name — pass it directly
          to ``kibana_search_logs`` (saves one round trip).
        - Don't use when: You need to search log content — that's
          ``kibana_search_logs``.

    Returns:
        dict with keys ``indices_count`` / ``pattern`` / ``include_system`` /
        ``indices`` (list of {index, health, status, docs_count, store_size_bytes, size_human}).
    """
    try:
        client = get_client()
        params: dict[str, Any] = {"format": "json", "bytes": "b"}
        path = f"/_cat/indices/{pattern}" if pattern and pattern != "*" else "/_cat/indices"
        data: list[dict[str, Any]] = client.get_es(path, params=params) or []

        indices: list[IndexSummary] = []
        for entry in data:
            idx_name: str = entry.get("index", "")
            if not include_system and (idx_name.startswith(".") or any(idx_name.startswith(p) for p in ("kibana",))):
                continue
            raw_docs = entry.get("docs.count")
            raw_size = entry.get("store.size") or entry.get("pri.store.size")
            try:
                docs_count: int | None = int(raw_docs) if raw_docs is not None else None
            except (TypeError, ValueError):
                docs_count = None
            try:
                size_bytes: int | None = int(raw_size) if raw_size is not None else None
            except (TypeError, ValueError):
                size_bytes = None

            indices.append(
                {
                    "index": idx_name,
                    "health": entry.get("health"),
                    "status": entry.get("status"),
                    "docs_count": docs_count,
                    "store_size_bytes": size_bytes,
                    "size_human": _size_human(entry.get("store.size") or entry.get("pri.store.size")),
                }
            )

        result: IndicesListOutput = {
            "indices_count": len(indices),
            "pattern": pattern,
            "include_system": include_system,
            "indices": indices,
        }
        md = (
            f"## Indices ({len(indices)} matching `{pattern}`"
            + (", system excluded" if not include_system else "")
            + ")\n\n"
            + "\n".join(
                [
                    f"- **{idx['index']}** — {idx['health'] or '?'}/{idx['status'] or '?'}"
                    f", docs={idx['docs_count']}, size={idx['size_human'] or '?'}"
                    for idx in indices[:50]
                ]
            )
        )
        if len(indices) > 50:
            md += f"\n\n_Showing first 50 of {len(indices)} indices._"
        return output.ok(result, md)  # type: ignore[return-value]
    except Exception as exc:
        output.fail(exc, "listing Elasticsearch indices")


@mcp.tool(
    name="kibana_search_logs",
    annotations={
        "title": "Search Logs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def kibana_search_logs(
    index: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Elasticsearch index name or pattern (e.g. 'logs-*', 'filebeat-2026.04.18'). "
                "Use `kibana_list_indices` to discover available indices."
            ),
        ),
    ],
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description=(
                "Elasticsearch Query String Syntax. Examples: "
                "'level:ERROR', 'level:ERROR AND service:api', "
                "'message:\"connection refused\" AND host:db*', "
                "'status:[500 TO 599]'. "
                "Use '*' to match all documents."
            ),
        ),
    ],
    time_field: Annotated[
        str,
        Field(
            default="@timestamp",
            max_length=200,
            description="Name of the timestamp field. Default '@timestamp' (Logstash/Filebeat convention).",
        ),
    ] = "@timestamp",
    time_from: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Start of the time range. ISO-8601 (e.g. '2026-04-18T00:00:00Z') "
                "or epoch-ms (e.g. '1713398400000'). Omit for unbounded start."
            ),
        ),
    ] = None,
    time_to: Annotated[
        str | None,
        Field(
            default=None,
            description=("End of the time range. ISO-8601 or epoch-ms. Omit for unbounded end (searches up to now)."),
        ),
    ] = None,
    size: Annotated[
        int,
        Field(default=20, ge=1, le=500, description="Maximum number of log hits to return (1-500, default 20)."),
    ] = 20,
    sort_order: Annotated[
        str,
        Field(
            default="desc",
            description="Sort order for results: 'desc' (newest first, default) or 'asc' (oldest first).",
        ),
    ] = "desc",
) -> SearchOutput:
    """Search logs using Elasticsearch Query String Syntax.

    Wraps ``POST {ES_URL}/{index}/_search`` with a ``bool/must`` query.
    Returns the top matching log entries with their ``_source`` fields.

    When more than 20 hits are rendered in the text output, a truncation
    hint is appended — use the structured ``hits`` field for the full list.

    Examples:
        - Use when: "Show me the last 20 ERROR logs from the API service."
          → ``index='logs-*'``, ``query='level:ERROR AND service:api'``.
        - Use when: "Find 'connection refused' errors in the last hour."
          → ``query='message:\"connection refused\"'``,
          ``time_from='2026-04-18T09:00:00Z'``, ``time_to='2026-04-18T10:00:00Z'``.
        - Use when: "Show me 500 errors sorted oldest first for replay."
          → ``query='status:500'``, ``sort_order='asc'``.
        - Don't use when: You want counts / statistics per field value —
          use ``kibana_aggregate_logs`` instead (``size:0`` aggregation is
          much cheaper than retrieving full log documents).
        - Don't use when: You need more than 500 docs — ES caps ``size`` at
          500 via this tool; use scroll API directly for bulk export.

    Returns:
        dict with ``total`` / ``returned`` / ``took_ms`` / ``hits`` (list).
    """
    try:
        if sort_order not in ("asc", "desc"):
            raise ValueError(f"sort_order must be 'asc' or 'desc', got {sort_order!r}")

        client = get_client()
        body = _build_search_body(
            query=query,
            time_field=time_field,
            time_from=_parse_epoch(time_from),
            time_to=_parse_epoch(time_to),
            size=size,
            sort_order=sort_order,
        )

        data = client.post_es(f"/{index}/_search", body) or {}
        hits_raw: list[dict[str, Any]] = (data.get("hits") or {}).get("hits") or []
        total_val = (data.get("hits") or {}).get("total") or {}
        total: int = total_val.get("value", 0) if isinstance(total_val, dict) else int(total_val or 0)
        took_ms: int = int(data.get("took") or 0)

        hits: list[LogHit] = [_shape_hit(h, time_field) for h in hits_raw]

        result: SearchOutput = {
            "total": total,
            "returned": len(hits),
            "took_ms": took_ms,
            "index": index,
            "query": query,
            "time_from": time_from,
            "time_to": time_to,
            "sort_order": sort_order,
            "hits": hits,
        }

        md_limit = 20
        header = (
            f"## Log search: `{query}` in `{index}`\n\n"
            f"Total matches: {total} | Returned: {len(hits)} | Took: {took_ms}ms\n\n"
        )
        lines = []
        for h in hits[:md_limit]:
            ts = h["timestamp"] or "no-ts"
            src_preview = json.dumps(h["_source"])[:200] if h["_source"] else "{}"
            lines.append(f"- `{ts}` [{h['_index']}] `{h['_id']}` — {src_preview}")
        md = header + "\n".join(lines)
        if len(hits) > md_limit:
            md += (
                f"\n\n_Showing first {md_limit} of {len(hits)} hits in text rendering — "
                "see the `hits` field in the structured content for the full list._"
            )
        return output.ok(result, md)  # type: ignore[return-value]
    except Exception as exc:
        output.fail(exc, f"searching logs in {index!r}")


@mcp.tool(
    name="kibana_aggregate_logs",
    annotations={
        "title": "Aggregate Logs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def kibana_aggregate_logs(
    index: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description="Elasticsearch index name or pattern (e.g. 'logs-*').",
        ),
    ],
    group_by: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Field name for terms aggregation (e.g. 'level', 'service.keyword', "
                "'http.response.status_code'). For text fields use the '.keyword' sub-field."
            ),
        ),
    ],
    query: Annotated[
        str,
        Field(
            default="*",
            max_length=2000,
            description=(
                "Elasticsearch Query String Syntax filter applied before aggregation. "
                "Use '*' (default) to aggregate all documents, or narrow with e.g. 'service:api'."
            ),
        ),
    ] = "*",
    metric: Annotated[
        str,
        Field(
            default="count",
            description=(
                "Aggregation metric: 'count' (default, doc_count per bucket), "
                "'avg', 'sum', 'min', 'max' (require metric_field)."
            ),
        ),
    ] = "count",
    metric_field: Annotated[
        str | None,
        Field(
            default=None,
            max_length=500,
            description=(
                "Field to apply the metric on. Required when metric is 'avg', 'sum', 'min', or 'max'. "
                "Example: 'response_time_ms' for avg latency per service."
            ),
        ),
    ] = None,
    time_field: Annotated[
        str,
        Field(default="@timestamp", max_length=200, description="Name of the timestamp field."),
    ] = "@timestamp",
    time_from: Annotated[
        str | None,
        Field(default=None, description="Start of time range. ISO-8601 or epoch-ms."),
    ] = None,
    time_to: Annotated[
        str | None,
        Field(default=None, description="End of time range. ISO-8601 or epoch-ms."),
    ] = None,
    size: Annotated[
        int,
        Field(default=10, ge=1, le=100, description="Number of terms buckets to return (1-100, default 10)."),
    ] = 10,
) -> AggregateOutput:
    """Aggregate logs using a terms grouping and optional metric.

    Wraps ``POST {ES_URL}/{index}/_search`` with ``size:0`` (no hits returned)
    and a ``terms`` aggregation on ``group_by``. This is the efficient way to
    get counts, averages, or sums grouped by a field value.

    When more than 20 buckets are rendered in the text output, a truncation
    hint is appended — use the structured ``buckets`` field for the full list.

    Examples:
        - Use when: "How many logs per log level in the last hour?"
          → ``index='logs-*'``, ``group_by='level'``,
          ``time_from='2026-04-18T09:00:00Z'``.
        - Use when: "What is the average response time per service?"
          → ``group_by='service.keyword'``, ``metric='avg'``,
          ``metric_field='response_time_ms'``.
        - Use when: "Top 10 HTTP status codes today."
          → ``group_by='http.response.status_code'``, ``size=10``.
        - Don't use when: You need raw log content/messages — use
          ``kibana_search_logs`` which returns full ``_source`` objects.
        - Don't use when: You need time-series (histogram per interval) —
          that requires a ``date_histogram`` aggregation not supported here.

    Returns:
        dict with ``total_documents`` / ``took_ms`` / ``buckets`` (list).
    """
    try:
        valid_metrics = ("count", "avg", "sum", "min", "max")
        if metric not in valid_metrics:
            raise ValueError(f"metric must be one of {valid_metrics}, got {metric!r}")
        if metric != "count" and not metric_field:
            raise ValueError(f"metric_field is required when metric is {metric!r}")

        client = get_client()
        body = _build_aggregation_body(
            query=query,
            group_by=group_by,
            metric=metric,
            metric_field=metric_field,
            size=size,
            time_field=time_field,
            time_from=_parse_epoch(time_from),
            time_to=_parse_epoch(time_to),
        )

        data = client.post_es(f"/{index}/_search", body) or {}
        took_ms: int = int(data.get("took") or 0)
        total_val = (data.get("hits") or {}).get("total") or {}
        total_documents: int = total_val.get("value", 0) if isinstance(total_val, dict) else int(total_val or 0)

        raw_buckets: list[dict[str, Any]] = ((data.get("aggregations") or {}).get("group_by") or {}).get(
            "buckets"
        ) or []

        buckets: list[AggregateBucket] = []
        for b in raw_buckets:
            mv_raw = (b.get("metric_value") or {}).get("value")
            mv: float | None = float(mv_raw) if mv_raw is not None else None
            buckets.append(
                {
                    "key": str(b.get("key", "")),
                    "doc_count": int(b.get("doc_count", 0)),
                    "metric_value": mv,
                }
            )

        result: AggregateOutput = {
            "total_documents": total_documents,
            "took_ms": took_ms,
            "index": index,
            "group_by": group_by,
            "metric": metric,
            "metric_field": metric_field,
            "buckets_count": len(buckets),
            "buckets": buckets,
        }

        metric_col = "doc_count" if metric == "count" else f"{metric}({metric_field})"
        md_limit = 20
        header = (
            f"## Aggregation: `{group_by}` in `{index}`\n\n"
            f"Total docs: {total_documents} | Took: {took_ms}ms | "
            f"Metric: {metric_col}\n\n"
            f"| {group_by} | {metric_col} |\n|---|---|\n"
        )
        rows = [
            f"| {b['key']} | {b['metric_value'] if b['metric_value'] is not None else b['doc_count']} |"
            for b in buckets[:md_limit]
        ]
        md = header + "\n".join(rows)
        if len(buckets) > md_limit:
            md += (
                f"\n\n_Showing first {md_limit} of {len(buckets)} buckets in text rendering — "
                "see the `buckets` field in the structured content for the full list._"
            )
        return output.ok(result, md)  # type: ignore[return-value]
    except Exception as exc:
        output.fail(exc, f"aggregating logs in {index!r} by {group_by!r}")


@mcp.tool(
    name="kibana_list_dashboards",
    annotations={
        "title": "List Kibana Dashboards",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def kibana_list_dashboards(
    search: Annotated[
        str | None,
        Field(
            default=None,
            max_length=500,
            description="Optional text search in dashboard titles (case-insensitive substring match).",
        ),
    ] = None,
    page: Annotated[
        int,
        Field(default=1, ge=1, le=1000, description="Page number (1-based)."),
    ] = 1,
    page_size: Annotated[
        int,
        Field(default=20, ge=1, le=100, description="Items per page (1-100, default 20)."),
    ] = 20,
) -> DashboardsListOutput:
    """List Kibana saved dashboards.

    Calls ``GET {KIBANA_URL}/api/saved_objects/_find?type=dashboard``.
    The ``kbn-xsrf: true`` header is always sent to satisfy Kibana's CSRF
    guard. Use this to discover dashboard IDs before calling
    ``kibana_get_dashboard``.

    Pagination: if ``has_more`` is ``True``, call again with ``page + 1``.

    Examples:
        - Use when: "What Kibana dashboards are available?"
          → default params.
        - Use when: "Find the infrastructure dashboard."
          → ``search='infrastructure'``.
        - Use when: "List all dashboards — page 2."
          → ``page=2``.
        - Don't use when: You already have a dashboard ID — use
          ``kibana_get_dashboard`` directly (one fewer round trip).
        - Don't use when: You need log content — dashboards contain
          visualisation config, not raw log data. Use ``kibana_search_logs``.

    Returns:
        dict with ``total`` / ``page`` / ``page_size`` / ``has_more`` /
        ``dashboards`` (list of {id, title, description, updated_at}).
    """
    try:
        client = get_client()
        params: dict[str, Any] = {
            "type": "dashboard",
            "per_page": page_size,
            "page": page,
        }
        if search:
            params["search"] = search
            params["search_fields"] = "title"

        data = client.get_kibana("/api/saved_objects/_find", params=params) or {}
        raw_objects: list[dict[str, Any]] = data.get("saved_objects") or []
        total: int = int(data.get("total") or 0)

        dashboards: list[DashboardSummary] = []
        for obj in raw_objects:
            attrs = obj.get("attributes") or {}
            dashboards.append(
                {
                    "id": obj.get("id", ""),
                    "title": attrs.get("title", ""),
                    "description": attrs.get("description") or None,
                    "updated_at": obj.get("updated_at"),
                }
            )

        has_more = bool(total and page * page_size < total)
        result: DashboardsListOutput = {
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            "search": search,
            "dashboards": dashboards,
        }

        md = (
            f"## Kibana Dashboards — page {page} ({len(dashboards)} of {total}"
            + (f", search={search!r}" if search else "")
            + f", has_more={has_more})\n\n"
            + "\n".join([f"- **{d['title']}** — `{d['id']}`" for d in dashboards])
        )
        return output.ok(result, md)  # type: ignore[return-value]
    except Exception as exc:
        output.fail(exc, "listing Kibana dashboards")


@mcp.tool(
    name="kibana_get_dashboard",
    annotations={
        "title": "Get Kibana Dashboard",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    structured_output=True,
)
def kibana_get_dashboard(
    dashboard_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description=(
                "Kibana dashboard UUID (e.g. 'abcd1234-5678-efgh-ijkl-mnopqrstuvwx'). "
                "Use `kibana_list_dashboards` to discover valid IDs."
            ),
        ),
    ],
) -> DashboardDetail:
    """Fetch a single Kibana dashboard with panel details.

    Calls ``GET {KIBANA_URL}/api/saved_objects/dashboard/{id}``.
    Returns the dashboard metadata and a summary of contained panels
    (visualisations, controls, maps, etc.).

    Examples:
        - Use when: "What panels does the 'Infrastructure Overview' dashboard have?"
          → obtain the ID from ``kibana_list_dashboards``, then call with
          ``dashboard_id=<id>``.
        - Use when: "Give me the description and panel count of dashboard X."
          → single call, no search needed if you have the ID.
        - Use when: Verifying that a dashboard ID from a URL or bookmark is valid.
        - Don't use when: You don't have the dashboard ID — call
          ``kibana_list_dashboards`` first with a ``search`` term.
        - Don't use when: You need log data shown in the dashboard —
          dashboards contain visualisation config only. Use
          ``kibana_search_logs`` / ``kibana_aggregate_logs`` for actual data.

    Returns:
        dict with ``id`` / ``title`` / ``description`` / ``panels_count`` /
        ``panels`` (list) / ``updated_at``.
    """
    try:
        client = get_client()
        data = client.get_kibana(f"/api/saved_objects/dashboard/{dashboard_id}") or {}
        attrs = data.get("attributes") or {}

        # Parse panels from kibanaSavedObjectMeta or panelsJSON
        panels: list[DashboardPanel] = []
        panels_json_str: str = attrs.get("panelsJSON", "") or ""
        if panels_json_str:
            try:
                raw_panels: list[dict[str, Any]] = json.loads(panels_json_str)
                for p in raw_panels:
                    panel_type = p.get("type") or (p.get("embeddableConfig") or {}).get("type")
                    panel_title = (p.get("embeddableConfig") or {}).get("title") or p.get("title")
                    panels.append({"panel_type": panel_type, "title": panel_title})
            except (json.JSONDecodeError, TypeError):
                pass

        result: DashboardDetail = {
            "id": data.get("id", dashboard_id),
            "title": attrs.get("title", ""),
            "description": attrs.get("description") or None,
            "panels_count": len(panels),
            "panels": panels,
            "updated_at": data.get("updated_at"),
        }

        md = (
            f"## Dashboard: {result['title']}\n\n"
            f"ID: `{result['id']}`\n"
            f"Updated: {result['updated_at'] or 'unknown'}\n"
            + (f"Description: {result['description']}\n" if result["description"] else "")
            + f"\n**Panels ({result['panels_count']}):**\n"
            + "\n".join([f"- [{p['panel_type'] or 'unknown'}] {p['title'] or '(untitled)'}" for p in panels[:30]])
        )
        if len(panels) > 30:
            md += f"\n\n_Showing first 30 of {len(panels)} panels._"
        return output.ok(result, md)  # type: ignore[return-value]
    except Exception as exc:
        output.fail(exc, f"fetching dashboard {dashboard_id!r}")
