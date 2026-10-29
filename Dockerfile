# DBU Waste Finder MCP (stdio). Used by Glama deploy/release and for local `docker run -i`.
# Credentials (DATABRICKS_HOST / TOKEN / WAREHOUSE_ID) are only needed when calling tools.
# Build needs PyPI access (use `docker build --network=host` if the default bridge has no DNS).
FROM python:3.12-slim

WORKDIR /app

# Install package from ./mcp (pyproject + src)
COPY mcp/pyproject.toml mcp/README.md /app/mcp/
COPY mcp/src /app/mcp/src
COPY LICENSE /app/LICENSE
COPY README.md /app/README.md

RUN pip install --no-cache-dir -e /app/mcp

# Default: stdio MCP. Glama wraps this with mcp-proxy for introspection.
ENTRYPOINT ["dbu-waste-finder-mcp"]
