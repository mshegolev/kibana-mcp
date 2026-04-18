# Evaluation suite

This repository ships a 10-question evaluation (`evaluation.xml`) built per the
mcp-builder Phase 4 specification. The suite measures whether an LLM can
productively use kibana-mcp to answer realistic, read-only questions about log
indices, log searches, aggregations, and dashboards.

## Design principles

Every question is **read-only, independent, stable, verifiable, complex, and
instance-agnostic**. Since kibana-mcp wraps a customer-owned Elasticsearch /
Kibana stack, no pre-solved shared fixture exists. The suite ships with
`__VERIFY_ON_INSTANCE__` placeholders.

## Filling in answers

1. Pick a target stack (your team's self-hosted ES+Kibana, or the demo at
   https://www.elastic.co/demo-gallery).
2. Export env vars:
   ```bash
   export KIBANA_URL=https://kibana.example.com
   # optional: export ELASTICSEARCH_URL=https://es.example.com:9200
   # optional: export KIBANA_API_KEY=... (or KIBANA_USERNAME / KIBANA_PASSWORD)
   ```
3. Solve each question — fastest path is Claude Code + this MCP configured.
4. Replace placeholders with verified values.
5. For instance stability, you may want to pin specific index names / dashboard
   IDs in each question instead of "first returned" references.

## Running the harness

```bash
python scripts/evaluation.py \
  -t stdio \
  -c uvx \
  -a kibana-mcp \
  -e KIBANA_URL=$KIBANA_URL \
  -e KIBANA_API_KEY=$KIBANA_API_KEY \
  -o evaluation_report.md \
  evaluation.xml
```

Low accuracy signals the same class of fix as sonarqube/jaeger: tighten tool
description, adjust output schema, or rephrase ambiguous questions.

## Design deviations

Same honest template compromise as sonarqube/jaeger: question structure is
fixed (validates the MCP design); values come from the instance you verify
against. A shared fixture would require a reproducible ES+Kibana deployment
with pinned documents and dashboards — out of scope for v0.1.0.
