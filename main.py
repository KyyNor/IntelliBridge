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

mcp_app = mcp.http_app(path='/mcp')

app = FastAPI(
    title="IntelliBridge API",
    description="IntelliBridge Backend Service",
    version="1.0.0",
    lifespan=mcp_app.lifespan
)

app.mount("/mcp", mcp_app)

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


async def main():
    """IntelliBridge服务启动"""
    logger.info("Starting IntelliBridge server...")
    logger.info(f"FastAPI: http://0.0.0.0:49000")
    logger.info(f"MCP: http://0.0.0.0:49000/mcp")

    config = uvicorn.Config(app, host="0.0.0.0", port=49000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
