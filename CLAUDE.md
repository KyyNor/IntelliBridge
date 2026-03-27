# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IntelliBridge is a backend service providing data query and browser automation tools via dual interfaces:
- **FastAPI** on port 49000 — REST API endpoints
- **MCP (Model Context Protocol)** on port 49001 — AI agent tool interface

## Development Commands

```bash
# Run locally
python main.py

# Docker build (online)
docker build -t intellibridge:latest .

# Docker build with offline support
./build.sh

# Start with docker-compose
docker-compose up
```

## Architecture

### Server Architecture
`main.py` starts two async servers simultaneously via `asyncio.gather()`:
- FastAPI (`run_fastapi()`) — HTTP REST API
- MCP (`run_mcp()`) — Streamable HTTP transport for AI agents

### Tool Layer (`tools/`)
Each tool exposes functionality through **both** FastAPI routers and MCP `@mcp.tool()` decorators:
- `hive_query.py` — Hive SQL queries with SQL normalization (sqlglot), filter validation, result caching
- `mysql_query.py` — MySQL queries with cross-database prevention, table metadata search
- `agent_browser.py` — Browser automation via `agent-browser` CLI with session management

### Utility Layer (`utils/`)
- `config.py` — YAML config manager with dot-notation access (`config.get("mysql.nodes")`)
- `cache.py` — Disk-based caching (diskcache) with TTL support
- `logger.py` — Loguru wrapper with daily rotation (30-day retention)
- `mcp.py` — FastMCP server instance (imported by tools for `@mcp.tool()` decorator)
- `hive_pool.py` — Hive connection singleton
- `mysql_pool.py` — MySQL connection pool per node/database
- `decorators.py` — `@log_function_info` for request tracing (request_id, elapsed time)

### Configuration
`config/config.yaml` defines:
- `hive.*` — Hive connection (host, port, username, database)
- `mysql.nodes[]` — Per-node MySQL connections with database lists
- `agent_browser.cdp_port` — CDP port for browser automation

### Key Patterns
- **SQL Normalization**: Tools use `sqlglot` to normalize and uppercase SQL before execution
- **Caching Strategy**: Hive queries (1h), MySQL table metadata (10-30min), MySQL query results (5min)
- **Connection Management**: Hive uses singleton pattern; MySQL uses per-node-per-database singletons
- **Security**: Hive enforces filter conditions on non-whitelisted tables; MySQL prevents cross-database queries

## Docker Architecture

Two Dockerfiles for different environments:
- `Dockerfile` — Online build with pip/npm mirrors (Tsinghua/NPMirror)
- `Dockerfile.offline` — Supports pre-downloaded Python packages in `build/python-packages/`

Both use pre-extracted Node.js binaries (not installed via apt) to work in air-gapped environments.
