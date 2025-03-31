from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.mcp_service_manager import router as mcp_service_manager_router
from routers.mcp_service_proxy import router as mcp_service_proxy_router

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
app.include_router(mcp_service_manager_router)
app.include_router(mcp_service_proxy_router)

@app.get("/")
async def root():
    return {"message": "MCP Gateway is running"}