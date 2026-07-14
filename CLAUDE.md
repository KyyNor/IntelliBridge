# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IntelliBridge is a backend service providing data query and browser automation tools through one combined ASGI application:
- **FastAPI REST API** and **MCP Streamable HTTP** share port 49000
- MCP applications are mounted under `/mcp/*`
- REST endpoints are mounted under `/api/*`

## Development Commands

```bash
# Run locally
python main.py

# Docker build (online)
docker build -f docker/Dockerfile -t intellibridge:latest .

# Docker build with offline support
./build.sh

# Start with docker-compose
docker-compose up
```

## Architecture

### Server Architecture
`main.py` serves `combined_app` through Uvicorn on port 49000. The app combines the REST routes and individual FastMCP HTTP applications, and applies CORS, request deadlines, and a combined lifespan to the served app.

Health endpoints:
- `/health/live` checks process liveness only.
- `/health/ready` checks initialized MySQL pools and Hive resource state.
- `/health` is an alias for readiness status.

### Tool Layer (`tools/`)
Each tool exposes functionality through **both** FastAPI routers and MCP `@mcp.tool()` decorators:
- `hive_query.py` — Hive SQL queries with SQL normalization (sqlglot), filter validation, result caching
- `mysql_query.py` — MySQL queries with cross-database prevention, table metadata search
- `agent_browser.py` — Browser automation via `agent-browser` CLI with session management

### Utility Layer (`utils/`)
- `config.py` — YAML config manager with dot-notation access (`config.get("mysql.nodes")`)
- `cache.py` — Disk-based caching (diskcache) with TTL support
- `logger.py` — Loguru wrapper with daily rotation (30-day retention)
- `middleware.py` — ASGI request deadline and timeout responses
- `cache_snapshot.py` — atomic cache snapshot publication
- `hive_pool.py` — bounded Hive query leases with one connection per query
- `mysql_pool.py` — MySQL connection pool per node/database with bounded acquisition
- `decorators.py` — `@log_function_info` and bounded asynchronous call-log writing

### Configuration
`config/config.yaml` defines:
- `hive.*` — Hive connection (host, port, username, database)
- `mysql.nodes[]` — Per-node MySQL connections with database lists
- `agent_browser.cdp_port` — CDP port for browser automation
- `server.request_timeout` — combined application request deadline in seconds

### Key Patterns
- **SQL Normalization**: Tools use `sqlglot` to normalize and uppercase SQL before execution
- **Caching Strategy**: Hive queries (1h), MySQL table metadata (10-30min), MySQL query results (5min)
- **Connection Management**: Hive allows at most two independent query connections; MySQL uses per-node pools with bounded acquisition waits
- **Timeouts**: The combined ASGI app has a 300-second default request deadline; nested code can read the remaining deadline through `utils.timeouts.get_remaining_timeout()`
- **Cache Refresh**: DataFactory and FineCPT caches build new snapshots before publishing them
- **Security**: Hive enforces filter conditions on non-whitelisted tables; MySQL prevents cross-database queries

## Docker Architecture

Two Dockerfiles for different environments:
- `Dockerfile` — Online build with pip/npm mirrors (Tsinghua/NPMirror)
- `Dockerfile.offline` — Supports pre-downloaded Python packages in `build/python-packages/`

Both use pre-extracted Node.js binaries (not installed via apt) to work in air-gapped environments.
