"""FastMCP server entry point for Kibana MCP."""

from __future__ import annotations

# Importing the tools module attaches @mcp.tool decorators to the shared
# FastMCP instance.
from kibana_mcp import tools as _tools  # noqa: F401
from kibana_mcp._mcp import app_lifespan, mcp


def main() -> None:
    """Entry point for the ``kibana-mcp`` console script (stdio)."""
    mcp.run()


__all__ = ["mcp", "app_lifespan", "main"]


if __name__ == "__main__":
    main()
