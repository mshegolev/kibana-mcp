"""TypedDict output schemas for every MCP tool.

These schemas are read by FastMCP (``structured_output=True``) to generate
a JSON-Schema ``outputSchema`` for each tool. Clients that support structured
data use that schema to validate the ``structuredContent`` payload; clients
that don't use the markdown ``content`` block instead.

**Python / Pydantic compat note.** We deliberately avoid
``Required`` / ``NotRequired`` qualifiers: Pydantic 2.13+ mishandles them
during runtime schema generation on Py < 3.12. Optional fields use
``| None`` convention; the code always sets the key (``None`` when absent).
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict


# ── Indices ──────────────────────────────────────────────────────────────────


class IndexSummary(TypedDict):
    index: str
    health: str | None
    status: str | None
    docs_count: int | None
    store_size_bytes: int | None
    size_human: str | None


class IndicesListOutput(TypedDict):
    indices_count: int
    pattern: str
    include_system: bool
    indices: list[IndexSummary]


# ── Log search ───────────────────────────────────────────────────────────────


class LogHit(TypedDict):
    _id: str
    _index: str
    _score: float | None
    _source: dict
    timestamp: str | None


class SearchOutput(TypedDict):
    total: int
    returned: int
    took_ms: int
    index: str
    query: str
    time_from: str | None
    time_to: str | None
    sort_order: str
    hits: list[LogHit]


# ── Aggregations ─────────────────────────────────────────────────────────────


class AggregateBucket(TypedDict):
    key: str
    doc_count: int
    metric_value: float | None


class AggregateOutput(TypedDict):
    total_documents: int
    took_ms: int
    index: str
    group_by: str
    metric: str
    metric_field: str | None
    buckets_count: int
    buckets: list[AggregateBucket]


# ── Dashboards ───────────────────────────────────────────────────────────────


class DashboardSummary(TypedDict):
    id: str
    title: str
    description: str | None
    updated_at: str | None


class DashboardsListOutput(TypedDict):
    total: int
    page: int
    page_size: int
    has_more: bool
    search: str | None
    dashboards: list[DashboardSummary]


class DashboardPanel(TypedDict):
    panel_type: str | None
    title: str | None


class DashboardDetail(TypedDict):
    id: str
    title: str
    description: str | None
    panels_count: int
    panels: list[DashboardPanel]
    updated_at: str | None
