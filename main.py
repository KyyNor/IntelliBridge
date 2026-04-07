import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP
import uvicorn

from tools.hive_query import router as hive_router
from tools.agent_browser import router as agent_browser_router
from tools.mysql_query import router as mysql_router
from tools.memory import Memory, memory_mcp
from tools.ds_code_search import DataFactoryCodeSearch
from utils.mcp import mcp
from utils.logger import logger

mcp = FastMCP("IntelliBridge")
mcp.mount(
    memory_mcp,
    namespace='memory'
)

mcp_app = mcp.http_app(path='/mcp')

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
        *mcp_app.routes,
        *app.routes,
    ],
    lifespan=mcp_app.lifespan,
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
