"""Pure helper functions for Elasticsearch search DSL construction,
index pattern validation, hit shaping, and byte/epoch formatting. No I/O, no
framework dependencies. Imported by both tools.py and log_client.py.
"""

from __future__ import annotations

from typing import Any

from kibana_mcp.models import LogHit

# ── Pure helper functions (unit-testable without HTTP) ────────────────────────

# Index name prefixes treated as "system" and hidden by default. Covers the
# dot-prefixed internal indices (.kibana, .security, .monitoring…) plus
# non-dot system indices that still appear in _cat/indices output.
_SYSTEM_INDEX_PREFIXES: tuple[str, ...] = (".", "kibana", "ilm-history", "shrink-")


def _is_system_index(name: str) -> bool:
    """Return True when ``name`` matches any prefix in ``_SYSTEM_INDEX_PREFIXES``."""
    return any(name.startswith(p) for p in _SYSTEM_INDEX_PREFIXES)


# Characters that legal ES/OpenSearch index names and patterns never contain
# but that would alter the request path or query string if interpolated.
_INDEX_FORBIDDEN_CHARS: tuple[str, ...] = ("/", "?", "#", "%", "\\")


def _validate_index_pattern(value: str, param_name: str = "index") -> str:
    """Validate a user-supplied index name / pattern before path interpolation.

    The value is interpolated into the request path (``/{index}/_search``,
    ``/_cat/indices/{pattern}``) by both backends, so path or query delimiters
    could redirect a read-only call to a different — potentially destructive —
    endpoint (e.g. ``index='logs/_delete_by_query?'`` turns
    ``POST /{index}/_search`` into ``POST /logs/_delete_by_query``).

    Rejects ``/``, ``?``, ``#``, ``%``, ``\\``, internal whitespace, and
    leading ``_`` (per comma-separated segment). Commas (multi-index) and
    ``*`` wildcards stay allowed. Returns the value with outer whitespace
    stripped.

    Raises:
        ValueError: If the value is empty or contains forbidden characters.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{param_name} must not be empty")
    if any(ch in cleaned for ch in _INDEX_FORBIDDEN_CHARS) or any(
        ch.isspace() for ch in cleaned
    ):
        raise ValueError(
            f"{param_name} contains characters not allowed in index names/patterns "
            f"('/', '?', '#', '%', '\\', whitespace): {cleaned!r}"
        )
    for segment in cleaned.split(","):
        if segment.strip().startswith("_"):
            raise ValueError(
                f"{param_name} segments must not start with '_' "
                f"(reserved for ES/OpenSearch APIs): {cleaned!r}"
            )
    return cleaned


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
