# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] — 2026-06-11

### Added

- OpenSearch compatibility via `OPENSEARCH_MODE=true` env var
  (KIB-01, KIB-02). New `os_client.py` module with `OpenSearchClient`
  duck-type compatible with `KibanaClient` (`get_es` / `post_es`).
- Optional extra `kibana-mcp[opensearch]` adds `opensearch-py>=2.4`
  and `boto3>=1.26` for SigV4 auth support.
- Three auth modes: SigV4 (AWS_REGION), Bearer token (KIBANA_API_KEY),
  HTTP Basic (KIBANA_USERNAME + KIBANA_PASSWORD), or anonymous.
  Auto-detected by priority; forced via `OPENSEARCH_AUTH` env var
  (`sigv4` | `basic` | `token`).
- `OPENSEARCH_URL` required when `OPENSEARCH_MODE=true`; raises
  `ConfigError` with actionable hint if unset or if `opensearch-py`
  not installed.
- SSL verification reuses existing `KIBANA_SSL_VERIFY` env var.

### Changed

- `_mcp.get_client()` now selects backend by `OPENSEARCH_MODE`; existing
  ES/Kibana behavior unchanged when env var is absent or false.

### Notes

- Kibana dashboard tools (`kibana_list_dashboards`, `kibana_get_dashboard`)
  continue to use the Kibana REST path regardless of `OPENSEARCH_MODE`.
- Query DSL is shared between backends; no forked query module.
- Bearer token auth uses opensearch-py `headers` kwarg (not `http_auth`);
  `http_auth` silently drops a bare string, causing 401 at runtime.

## [0.1.0] — 2026-04-18

### Added

- `kibana_list_indices` — list Elasticsearch indices with health, status, doc count, and storage size
- `kibana_search_logs` — full-text log search using Elasticsearch Query String Syntax with time range filtering
- `kibana_aggregate_logs` — terms aggregation with optional avg/sum/min/max sub-metric
- `kibana_list_dashboards` — list Kibana saved dashboards with search and pagination
- `kibana_get_dashboard` — fetch a single dashboard with panel type/title breakdown
- Dual-transport client: direct Elasticsearch URL or Kibana Console proxy fallback
- Auth priority: ApiKey > Basic > anonymous
- `KIBANA_SSL_VERIFY` support for self-signed certificates
- FastMCP stdio transport, Python 3.10+, Trusted Publisher OIDC for PyPI
