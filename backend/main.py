from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import mcp_service_manager, mcp_service_scanner, mcp_service_proxy
from services.scheduler_manager import SchedulerManager
from models.mcp_service_model import McpServiceModel
from utils.db import DatabaseManager

app = FastAPI(
    title="MCP Gateway",
    docs_url=None,  # 禁用文档，减少内存占用
    redoc_url=None,
    openapi_url=None,
    lifespan=None  # 禁用生命周期事件，减少开销
)

# 添加CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(mcp_service_manager.router)
app.include_router(mcp_service_scanner.router)
app.include_router(mcp_service_proxy.router)

# 创建数据库管理器和MCP服务模型
db_manager = DatabaseManager()
mcp_service_model = McpServiceModel(db_manager)

# 创建并启动定时任务管理器
scheduler_manager = SchedulerManager(mcp_service_model)

@app.on_event("startup")
async def startup_event():
    await scheduler_manager.start()

@app.on_event("shutdown")
async def shutdown_event():
    await scheduler_manager.stop()

@app.get("/")
async def root():
    return {"message": "MCP Gateway is running"}