"""Importable LogClient facade + LogHit dataclass for the Investigator Contract.

Works against both Elasticsearch (KibanaClient) and OpenSearch (OpenSearchClient)
backends. Safe to import without starting the MCP server — no FastMCP dependency
at module level.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kibana_mcp.client import _parse_bool
from kibana_mcp.errors import ConfigError  # noqa: F401
from kibana_mcp.search_helpers import (
    _build_search_body,
    _parse_epoch,
    _validate_index_pattern,
)

# ── Module-level candidate list defaults ─────────────────────────────────────

_DEFAULT_TRACE_ID_CANDIDATES: list[str] = ["trace.id", "traceId", "trace_id"]
_DEFAULT_SERVICE_NAME_CANDIDATES: list[str] = [
    "service.name",
    "serviceName",
    "service_name",
]


# ── Private helpers ───────────────────────────────────────────────────────────


def _resolve_dotted(source: dict[str, Any], dotted_key: str) -> Any | None:
    """Resolve a dotted key from a source dict.

    Tries flat dotted-key lookup first (e.g. ``source["trace.id"]``), then
    nested-dict traversal (``source["trace"]["id"]``). Returns the first
    non-None value found, or None if absent.
    """
    # 1. Flat dotted-key form: {"trace.id": "abc"}
    flat = source.get(dotted_key)
    if flat is not None:
        return flat

    # 2. Nested dict traversal: {"trace": {"id": "abc"}}
    parts = dotted_key.split(".")
    current: Any = source
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _extract_json_field(
    source: dict[str, Any], *keys: str
) -> dict[str, Any] | None:
    """Extract a JSON field from source, trying each key in order.

    For the first non-None value found:
    - dict → returned as-is
    - str  → parsed with json.loads; None on JSONDecodeError

    Returns None if all keys are absent. NEVER raises any exception.
    """
    try:
        for key in keys:
            value = _resolve_dotted(source, key)
            if value is None:
                continue
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        return parsed
                    return None
                except json.JSONDecodeError:
                    return None
        return None
    except Exception:  # noqa: BLE001
        return None


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime.

    Replaces trailing 'Z' with '+00:00' before parsing. Returns None on
    ValueError (malformed input).
    """
    try:
        adjusted = ts.rstrip("Z") + "+00:00" if ts.endswith("Z") else ts
        return datetime.fromisoformat(adjusted)
    except ValueError:
        return None


# ── LogHit dataclass ──────────────────────────────────────────────────────────


@dataclass
class LogHit:
    """Facade dataclass representing a single log hit from Elasticsearch/OpenSearch.

    All 9 contract fields from the Investigator Contract are present.
    Extraction never raises; absent or unparseable fields default to None.
    """

    timestamp_utc: datetime | None = None
    trace_id: str | None = None
    service_name: str | None = None
    level: str | None = None
    message: str | None = None
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_source(
        cls,
        source: dict[str, Any],
        *,
        time_field: str = "@timestamp",
        trace_id_candidates: list[str] | None = None,
        service_name_candidates: list[str] | None = None,
        raw_hit: dict[str, Any] | None = None,
    ) -> LogHit:
        """Construct a LogHit from an Elasticsearch ``_source`` dict.

        Args:
            source: The ``_source`` value from an ES/OS hit.
            time_field: Source field name for the timestamp (default
                ``"@timestamp"``).
            trace_id_candidates: Candidate field names for trace_id, checked in
                order. Defaults to
                ``["trace.id", "traceId", "trace_id"]``.
            service_name_candidates: Candidate field names for service_name.
                Defaults to ``["service.name", "serviceName", "service_name"]``.
            raw_hit: The full ES hit dict (including ``_id``, ``_index``, etc.);
                stored in ``LogHit.raw``.

        Returns:
            A fully-populated :class:`LogHit` instance.
        """
        trace_candidates = (
            trace_id_candidates or _DEFAULT_TRACE_ID_CANDIDATES
        )
        service_candidates = (
            service_name_candidates or _DEFAULT_SERVICE_NAME_CANDIDATES
        )

        # timestamp_utc
        ts_str = source.get(time_field)
        timestamp_utc = _parse_timestamp(ts_str) if ts_str else None

        # trace_id — first non-None candidate wins
        trace_id: str | None = None
        for cand in trace_candidates:
            val = _resolve_dotted(source, cand)
            if val is not None:
                trace_id = str(val)
                break

        # service_name — first non-None candidate wins
        service_name: str | None = None
        for cand in service_candidates:
            val = _resolve_dotted(source, cand)
            if val is not None:
                service_name = str(val)
                break

        # level
        level = (
            source.get("level")
            or _resolve_dotted(source, "log.level")
            or None
        )

        # message
        message = source.get("message") or None

        # request_json / response_json — tolerant extraction
        request_json = _extract_json_field(
            source, "request", "http.request.body.content"
        )
        response_json = _extract_json_field(
            source, "response", "http.response.body.content"
        )

        return cls(
            timestamp_utc=timestamp_utc,
            trace_id=trace_id,
            service_name=service_name,
            level=level,
            message=message,
            request_json=request_json,
            response_json=response_json,
            fields=source,
            raw=raw_hit or {},
        )


# ── LogClient class ───────────────────────────────────────────────────────────


class LogClient:
    """Importable facade for searching Elasticsearch/OpenSearch logs.

    Wraps a backend (KibanaClient or OpenSearchClient) via their duck-typed
    ``post_es`` interface. No new HTTP session — auth and connection config
    come from the backend.

    Obtain via :meth:`from_env` for production use, or pass a mock backend
    in tests.
    """

    def __init__(
        self,
        backend: Any,
        *,
        trace_id_candidates: list[str] | None = None,
        service_name_candidates: list[str] | None = None,
    ) -> None:
        self._backend = backend
        self.trace_id_candidates = (
            trace_id_candidates or _DEFAULT_TRACE_ID_CANDIDATES
        )
        self.service_name_candidates = (
            service_name_candidates or _DEFAULT_SERVICE_NAME_CANDIDATES
        )

    @classmethod
    def from_env(
        cls,
        *,
        trace_id_candidates: list[str] | None = None,
        service_name_candidates: list[str] | None = None,
    ) -> LogClient:
        """Construct a LogClient from environment variables.

        Selects backend via OPENSEARCH_MODE:
        - ``true``/``1``/``yes``/``on`` → :class:`OpenSearchClient`
        - anything else (default) → :class:`KibanaClient`

        KibanaClient and OpenSearchClient are imported lazily here to prevent
        transitive FastMCP imports at module level.
        """
        from kibana_mcp.client import KibanaClient  # noqa: PLC0415

        opensearch_mode = _parse_bool(
            os.environ.get("OPENSEARCH_MODE"), default=False
        )

        if opensearch_mode:
            from kibana_mcp.os_client import OpenSearchClient  # noqa: PLC0415

            backend: Any = OpenSearchClient()
        else:
            backend = KibanaClient()

        return cls(
            backend=backend,
            trace_id_candidates=trace_id_candidates,
            service_name_candidates=service_name_candidates,
        )

    def search_logs(
        self,
        query: str,
        *,
        index: str = "*",
        time_from: datetime | str | None = None,
        time_to: datetime | str | None = None,
        size: int = 100,
        sort: str = "asc",
    ) -> list[LogHit]:
        """Search logs and return a list of :class:`LogHit` objects.

        Args:
            query: Elasticsearch Query String Syntax expression.
            index: Index name or pattern (default ``"*"``). Validated against
                injection characters via :func:`_validate_index_pattern`.
            time_from: Lower time bound — ``datetime``, ISO-8601 string, or
                ``None`` (unbounded).
            time_to: Upper time bound — same semantics as ``time_from``.
            size: Number of hits to return (default 100).
            sort: Sort order ``"asc"`` or ``"desc"`` (default ``"asc"``).

        Returns:
            List of :class:`LogHit` instances; empty list when no hits.

        Raises:
            ValueError: If ``index`` contains injection characters.
            Exception: Backend exceptions are re-raised as-is.
        """
        # Security gate: validate index pattern before path interpolation
        _validate_index_pattern(index, "index")

        # Normalise time bounds to str | None
        def _to_str(val: datetime | str | None) -> str | None:
            if val is None:
                return None
            if isinstance(val, datetime):
                return _parse_epoch(val.isoformat())
            return _parse_epoch(str(val))

        time_from_str = _to_str(time_from)
        time_to_str = _to_str(time_to)

        body = _build_search_body(
            query, "@timestamp", time_from_str, time_to_str, size, sort
        )

        response = self._backend.post_es(f"/{index}/_search", body)

        raw_hits: list[dict[str, Any]] = (
            (response.get("hits") or {}).get("hits") or []
        )

        result: list[LogHit] = []
        for raw_hit in raw_hits:
            source = raw_hit.get("_source") or {}
            hit_obj = LogHit.from_source(
                source,
                trace_id_candidates=self.trace_id_candidates,
                service_name_candidates=self.service_name_candidates,
                raw_hit=raw_hit,
            )
            result.append(hit_obj)

        return result

    def get_request_response_json(
        self, hit: LogHit
    ) -> dict[str, dict[str, Any] | None]:
        """Return request and response dicts already extracted in a LogHit.

        Pure re-read — no re-query to the backend. NEVER raises.

        Args:
            hit: A :class:`LogHit` instance (from :meth:`search_logs`).

        Returns:
            ``{"request": dict|None, "response": dict|None}``
        """
        return {"request": hit.request_json, "response": hit.response_json}
