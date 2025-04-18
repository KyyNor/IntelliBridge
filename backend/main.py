from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import mcp_service_manager, mcp_service_scanner, mcp_service_proxy, mcp_service_tester
from services.scheduler_manager import SchedulerManager
from models.mcp_service_model import McpServiceModel
from utils.db import DatabaseManager
from utils.logger import log


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时的代码
    log.info("应用程序启动中...")

    # 创建数据库管理器和MCP服务模型
    db_manager = DatabaseManager()
    mcp_service_model = McpServiceModel(db_manager)
    log.info("已初始化数据库管理器和MCP服务模型")

    # 创建并启动定时任务管理器
    scheduler_manager = SchedulerManager(mcp_service_model)
    log.info("已创建定时任务管理器")

    await scheduler_manager.start()
    log.info("应用程序启动完成")
    yield
    # 关闭时的代码
    log.info("应用程序关闭中...")
    await scheduler_manager.stop()
    log.info("应用程序关闭完成")

app = FastAPI(
    title="MCP Gateway",
    docs_url=None,  # 禁用文档，减少内存占用
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)

# 添加CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
log.info("已配置CORS中间件")

# 注册路由
app.include_router(mcp_service_manager.router)
app.include_router(mcp_service_scanner.router)
app.include_router(mcp_service_tester.router)
app.include_router(mcp_service_proxy.router)
log.info("已注册所有路由")

# @app.on_event("startup")
# async def startup_event():
#
#     await scheduler_manager.start()
#     log.info("应用程序启动完成")
#
# @lifespan
# @app.on_event("shutdown")
# async def shutdown_event():
#

@app.get("/")
async def root():
    log.debug("收到根路径请求")
    return {"message": "MCP Gateway is running"}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
