# Changelog

All notable changes to this project will be documented in this file.

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
