"""Shared FastMCP instance and client cache."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from kibana_mcp.client import KibanaClient

logger = logging.getLogger(__name__)

_client: KibanaClient | Any | None = None
_client_lock = threading.Lock()


@asynccontextmanager
async def app_lifespan(_app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Server lifespan: close HTTP sessions on shutdown."""
    logger.debug("kibana_mcp: startup")
    try:
        yield {}
    finally:
        global _client
        with _client_lock:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
                _client = None
        logger.debug("kibana_mcp: shutdown — HTTP sessions closed")


mcp = FastMCP("kibana_mcp", lifespan=app_lifespan)


def get_client() -> KibanaClient | Any:
    """Return a cached client instance (thread-safe lazy-init).

    Selects backend by OPENSEARCH_MODE env var:
    - If OPENSEARCH_MODE is true/1/yes/on → OpenSearchClient
    - Else → KibanaClient (requests-based)

    FastMCP runs synchronous tools in worker threads via
    ``anyio.to_thread.run_sync``; concurrent first-calls could otherwise
    race on the ``_client`` global. The lock ensures exactly one instance
    is constructed.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked locking
                from kibana_mcp.client import KibanaClient, _parse_bool  # noqa: PLC0415
                from kibana_mcp.os_client import OpenSearchClient  # noqa: PLC0415
                import os as _os  # noqa: PLC0415

                opensearch_mode = _parse_bool(
                    _os.environ.get("OPENSEARCH_MODE"), default=False
                )

                if opensearch_mode:
                    _client = OpenSearchClient()
                else:
                    _client = KibanaClient()

    return _client
