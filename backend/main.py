from fastapi import FastAPI
from routers.mcp_service_manager import router as mcp_service_manager_router

app = FastAPI(title="MCP Gateway")
app.include_router(mcp_service_manager_router)

@app.get("/")
async def root():
    return {"message": "MCP Gateway is running"}