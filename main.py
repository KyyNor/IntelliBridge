import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
import uvicorn

from tools.hive_query import hive_mcp, router as hive_router
from tools.agent_browser import agent_browser_mcp,router as agent_browser_router
from tools.mysql_query import mysql_mcp,router as mysql_router
from tools.load_export import load_job_manager, router as load_router
from tools.fine_report_tools import fr_mcp, fr_router
from tools.memory import memory_mcp
from tools.ds_code_search import ds_search_mcp, DataFactoryCodeSearch
from tools.ds_client import ds_mcp
from tools.fine_cpt_search import fine_cpt_mcp
from tools.spark_sql_analyzer import spark_analyzer
from utils.config import config
from utils.decorators import shutdown_call_log_writer
from utils.hive_pool import hive_pool
from utils.logger import logger
from utils.middleware import RequestTimeoutMiddleware
from utils.mysql_pool import mysql_pool

memory_mcp_app = memory_mcp.http_app(path='/mcp/memory')
hive_mcp_app = hive_mcp.http_app(path='/mcp/hive')
mysql_mcp_app = mysql_mcp.http_app(path='/mcp/mysql')
ds_search_mcp_app = ds_search_mcp.http_app(path='/mcp/ds_search')
agent_browser_mcp_app = agent_browser_mcp.http_app(path='/mcp/agent_browser')
ds_mcp_app = ds_mcp.http_app(path='/mcp/ds_runner')
fr_mcp_app = fr_mcp.http_app(path='/mcp/fine_report_tools')
fine_cpt_mcp_app = fine_cpt_mcp.http_app(path='/mcp/fine_search')

app = FastAPI(
    title="IntelliBridge API",
    description="IntelliBridge Backend Service",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"message": "Welcome to IntelliBridge API"}


def _readiness_payload() -> tuple[dict, int]:
    mysql_status = mysql_pool.get_readiness()
    hive_status = hive_pool.get_status()
    checks = {"mysql": mysql_status, "hive": hive_status}
    ready = all(check.get("healthy", False) for check in checks.values())
    return {
        "status": "healthy" if ready else "not_ready",
        "checks": checks,
    }, 200 if ready else 503


@app.get("/health")
async def health_check():
    payload, status_code = _readiness_payload()
    return JSONResponse(payload, status_code=status_code)


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    payload, status_code = _readiness_payload()
    return JSONResponse(payload, status_code=status_code)


# 注册路由
app.include_router(hive_router)
app.include_router(agent_browser_router)
app.include_router(mysql_router)
app.include_router(fr_router)
app.include_router(load_router)

_mcp_lifespan = combine_lifespans(
    memory_mcp_app.lifespan,
    hive_mcp_app.lifespan,
    mysql_mcp_app.lifespan,
    ds_search_mcp_app.lifespan,
    agent_browser_mcp_app.lifespan,
    fr_mcp_app.lifespan,
    fine_cpt_mcp_app.lifespan,
    ds_mcp_app.lifespan,
)


def shutdown_resources() -> None:
    """Flush background work and close process-owned resources."""

    shutdown_call_log_writer()
    spark_analyzer.stop()
    load_job_manager.stop()
    hive_pool.close()
    mysql_pool.close_all_pools()


@asynccontextmanager
async def combined_lifespan(application):
    # 启动后台 Spark SQL 查询性能分析器（幂等，配置 enabled=false 时是空操作）
    spark_analyzer.ensure_started()
    # 启动 Load spool TTL 清理器（幂等）
    load_job_manager.ensure_started()
    try:
        async with _mcp_lifespan(application):
            yield
    finally:
        shutdown_resources()


combined_app = FastAPI(
    title="IntelliBridge",
    description="IntelliBridge Service",
    routes=[
        *app.routes,
        *memory_mcp_app.routes,
        *hive_mcp_app.routes,
        *mysql_mcp_app.routes,
        *ds_search_mcp_app.routes,
        *agent_browser_mcp_app.routes,
        *fr_mcp_app.routes,
        *fine_cpt_mcp_app.routes,
        *ds_mcp_app.routes,
    ],
    lifespan=combined_lifespan,
)

combined_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
combined_app.add_middleware(
    RequestTimeoutMiddleware,
    timeout_seconds=float(config.get("server.request_timeout", 300.0)),
)

async def main():
    """IntelliBridge服务启动"""
    logger.info("Starting IntelliBridge server...")
    logger.info(f"FastAPI: http://0.0.0.0:49000")
    logger.info(f"MCP: http://0.0.0.0:49000/mcp")

    uvicorn_config = uvicorn.Config(combined_app, host="0.0.0.0", port=49000, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
