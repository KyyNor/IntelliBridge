from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.mcp_service_manager import router as mcp_service_manager_router

app = FastAPI(title="MCP Gateway")

# 添加CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mcp_service_manager_router)

@app.get("/")
async def root():
    return {"message": "MCP Gateway is running"}