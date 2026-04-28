import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastmcp.utilities.lifespan import combine_lifespans
import uvicorn

from tools.hive_query import hive_mcp, router as hive_router
from tools.agent_browser import agent_browser_mcp,router as agent_browser_router
from tools.mysql_query import mysql_mcp,router as mysql_router
from tools.fine_report_tools import fr_mcp, fr_router
from tools.memory import Memory, memory_mcp
from tools.ds_code_search import ds_search_mcp, DataFactoryCodeSearch
from tools.ds_client import ds_mcp
from tools.fine_cpt_search import fine_cpt_mcp
from utils.logger import logger

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


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# 注册路由
app.include_router(hive_router)
app.include_router(agent_browser_router)
app.include_router(mysql_router)

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
    lifespan=combine_lifespans(
        memory_mcp_app.lifespan,
        hive_mcp_app.lifespan,
        mysql_mcp_app.lifespan,
        ds_search_mcp_app.lifespan,
        agent_browser_mcp_app.lifespan,
        fr_mcp_app.lifespan,
        fine_cpt_mcp_app.lifespan,
        ds_mcp_app.lifespan,
    ),
)

async def main():
    """IntelliBridge服务启动"""
    logger.info("Starting IntelliBridge server...")
    logger.info(f"FastAPI: http://0.0.0.0:49000")
    logger.info(f"MCP: http://0.0.0.0:49000/mcp")

    config = uvicorn.Config(combined_app, host="0.0.0.0", port=49000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
