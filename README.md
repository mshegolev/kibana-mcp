# kibana-mcp

<!-- mcp-name: io.github.mshegolev/kibana-mcp -->

[![PyPI version](https://badge.fury.io/py/kibana-mcp.svg)](https://pypi.org/project/kibana-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/mshegolev/kibana-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/mshegolev/kibana-mcp/actions)

MCP server for **Kibana / Elasticsearch** — log search, aggregations, index discovery, and dashboard browsing via Claude and any MCP-compatible agent.

## Why another Kibana MCP?

Existing integrations require a running Kibana instance with browser-level credentials and often wrap the Kibana UI rather than the stable REST APIs. This server:

- Hits **Elasticsearch REST API directly** for log queries (faster, stable across Kibana UI changes)
- Falls back to the **Kibana Console proxy** when no direct ES URL is configured (zero extra firewall rules)
- Supports **ApiKey auth** (best for agents) as well as Basic auth and anonymous access
- Returns both **structured JSON** (`outputSchema`) and **markdown text** so it works with any MCP client
- Is **read-only** — all tools carry `readOnlyHint: true`, no data is modified

## Tools

| Tool | API | Description |
|------|-----|-------------|
| `kibana_list_indices` | `GET ES/_cat/indices` | Discover available indices with health, docs, size |
| `kibana_search_logs` | `POST ES/{index}/_search` | Full-text log search with time range, sort, size |
| `kibana_aggregate_logs` | `POST ES/{index}/_search` | Terms grouping with count/avg/sum/min/max metric |
| `kibana_list_dashboards` | `GET Kibana/api/saved_objects/_find` | List saved dashboards with search + pagination |
| `kibana_get_dashboard` | `GET Kibana/api/saved_objects/dashboard/{id}` | Fetch one dashboard with panel breakdown |

## Installation

```bash
pip install kibana-mcp
```

Or run directly with `uvx`:

```bash
uvx kibana-mcp
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `KIBANA_URL` | Yes | Kibana base URL (e.g. `https://kibana.example.com`) |
| `ELASTICSEARCH_URL` | No | Direct ES endpoint. If unset, ES requests go through Kibana Console proxy |
| `KIBANA_API_KEY` | No | ES API key (`ApiKey base64(id:api_key)` format). Recommended for agents |
| `KIBANA_USERNAME` | No | HTTP Basic auth username (used if API key not set) |
| `KIBANA_PASSWORD` | No | HTTP Basic auth password |
| `KIBANA_SSL_VERIFY` | No | `true` (default) or `false` for self-signed certificates |

Auth priority: **ApiKey** > **Basic** > **anonymous**.

Copy `.env.example` to `.env` and fill in your values.

### MCP Client Configuration (Claude Desktop / claude.app)

```json
{
  "mcpServers": {
    "kibana": {
      "command": "uvx",
      "args": ["kibana-mcp"],
      "env": {
        "KIBANA_URL": "https://kibana.example.com",
        "KIBANA_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

Or with direct ES access for better performance:

```json
{
  "mcpServers": {
    "kibana": {
      "command": "uvx",
      "args": ["kibana-mcp"],
      "env": {
        "KIBANA_URL": "https://kibana.example.com",
        "ELASTICSEARCH_URL": "https://es.example.com:9200",
        "KIBANA_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Docker

```bash
docker run --rm -i \
  -e KIBANA_URL=https://kibana.example.com \
  -e KIBANA_API_KEY=your-key \
  ghcr.io/mshegolev/kibana-mcp
```

## Usage Examples

### Log Search

```
Find the last 50 ERROR logs from the API service in the last hour
```
→ `kibana_search_logs(index="logs-*", query="level:ERROR AND service:api", size=50, time_from="2026-04-18T09:00:00Z")`

```
Show 500 HTTP errors sorted oldest first for incident replay
```
→ `kibana_search_logs(index="nginx-*", query="status:500", sort_order="asc", size=100)`

### Aggregations

```
How many logs per log level in the last hour?
```
→ `kibana_aggregate_logs(index="logs-*", group_by="level", time_from="2026-04-18T09:00:00Z")`

```
What is the average response time per service?
```
→ `kibana_aggregate_logs(index="logs-*", group_by="service.keyword", metric="avg", metric_field="response_time_ms")`

### Index Discovery

```
What log indices are available?
```
→ `kibana_list_indices()`

```
Show me all filebeat indices
```
→ `kibana_list_indices(pattern="filebeat-*")`

### Dashboards

```
Find the infrastructure dashboard
```
→ `kibana_list_dashboards(search="infrastructure")`

```
What panels does dashboard X have?
```
→ `kibana_get_dashboard(dashboard_id="<id from list_dashboards>")`

## Performance Characteristics

- **Log search** (`kibana_search_logs`): typically 50-500ms with direct ES URL; add 100-200ms when routing through Kibana Console proxy
- **Aggregations** (`kibana_aggregate_logs`): `size:0` queries — no hits transferred, usually 10-100ms
- **Index listing**: single `_cat/indices` call, O(index_count) response, typically <100ms
- **Dashboard APIs**: Kibana Saved Objects API, typically 50-200ms; latency is Kibana-side, not network
- Set `ELASTICSEARCH_URL` directly if your agent does frequent log searches — eliminates the proxy overhead

## Development

```bash
git clone https://github.com/mshegolev/kibana-mcp
cd kibana-mcp
pip install -e '.[dev]'
pytest tests/ -v
ruff check src tests
ruff format src tests
```

## License

MIT — see [LICENSE](LICENSE).
