import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from tools.hive_query import router as hive_router
from tools.agent_browser import router as agent_browser_router
from tools.mysql_query import router as mysql_router
from tools.memory import Mem0Memory
from tools.ds_code_search import DataFactoryCodeSearch
from utils.mcp import mcp
from utils.logger import logger

app = FastAPI(
    title="IntelliBridge API",
    description="IntelliBridge Backend Service",
    version="1.0.0"
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


async def run_fastapi():
    """运行 FastAPI 服务器"""
    config = uvicorn.Config(app, host="0.0.0.0", port=49001, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_mcp():
    """运行 MCP 服务器"""
    await mcp.run_http_async(transport="streamable-http", host="0.0.0.0", port=49000, log_level="info")


async def main():
    """同时运行 FastAPI 和 MCP 服务器"""
    logger.info("Starting IntelliBridge servers...")
    logger.info(f"FastAPI server: http://0.0.0.0:49001")
    logger.info(f"MCP server: http://0.0.0.0:49000")

    try:
        await asyncio.gather(
            run_fastapi(),
            run_mcp()
        )
    except KeyboardInterrupt:
        logger.info("Shutting down servers...")


if __name__ == "__main__":
    asyncio.run(main())
