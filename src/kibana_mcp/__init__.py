"""kibana-mcp — MCP server for Kibana / Elasticsearch log search."""

from __future__ import annotations

__version__ = "0.2.1"

from kibana_mcp.log_client import LogClient, LogHit

__all__ = ["__version__", "LogClient", "LogHit"]
