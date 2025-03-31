from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
import httpx
from typing import Dict
from backend.models.mcp_service_model import McpServiceModel, ServiceStatus
from utils.db import DatabaseManager
import asyncio
from contextlib import asynccontextmanager

router = APIRouter(tags=["MCP Service Proxy"])

# 创建全局连接池
@asynccontextmanager
async def get_client():
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=5.0,
            read=None,  # SSE 连接需要保持
            write=5.0,
            pool=5.0
        ),
        limits=httpx.Limits(
            max_keepalive_connections=1000,  # 最大保持连接数
            max_connections=2000,  # 最大连接数
            keepalive_expiry=30.0  # 连接保持时间
        )
    ) as client:
        yield client

def get_db_manager():
    return DatabaseManager()

def get_mcp_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceModel(db_manager)

async def forward_sse_request(request: Request, target_url: str):
    """
    转发 SSE 请求到目标URL
    """
    async with get_client() as client:
        try:
            headers = dict(request.headers)
            headers.pop("host", None)
            
            # SSE 请求需要设置特殊的 headers
            headers["Accept"] = "text/event-stream"
            headers["Cache-Control"] = "no-cache"
            headers["Connection"] = "keep-alive"
            
            async with client.stream("GET", target_url, headers=headers) as response:
                # 设置 SSE 响应头
                return StreamingResponse(
                    response.aiter_raw(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "Content-Type": "text/event-stream"
                    }
                )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

async def forward_request(request: Request, target_url: str):
    """
    转发普通请求到目标URL
    """
    async with get_client() as client:
        try:
            # 获取原始请求的方法、头部和内容
            method = request.method
            headers = dict(request.headers)
            # 移除可能导致问题的头部
            headers.pop("host", None)
            
            # 获取请求体
            body = await request.body() if method in ["POST", "PUT", "PATCH"] else None
            
            # 转发请求
            response = await client.request(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
                follow_redirects=True
            )
            
            # 创建响应
            return StreamingResponse(
                response.aiter_raw(),
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@router.api_route("/{endpoint}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_request(
    endpoint: str,
    path: str,
    request: Request,
    mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)
):
    """
    处理MCP服务的接口转发
    """
    # 查找对应的MCP服务
    service = mcp_service_model.get_service_by_endpoint(endpoint)
    if not service:
        raise HTTPException(status_code=404, detail=f"Service with endpoint {endpoint} not found")
    
    if service.status != ServiceStatus.ACTIVE:
        raise HTTPException(status_code=503, detail=f"Service {service.name} is not active")
    
    # 构建目标URL
    target_url = f"http://{service.ip}:{service.port}/{path}"
    
    # 检查是否是 SSE 请求
    accept_header = request.headers.get("accept", "")
    if "text/event-stream" in accept_header:
        return await forward_sse_request(request, target_url)
    
    # 普通请求转发
    return await forward_request(request, target_url) 