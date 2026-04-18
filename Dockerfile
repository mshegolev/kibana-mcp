FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir kibana-mcp

ENTRYPOINT ["kibana-mcp"]
