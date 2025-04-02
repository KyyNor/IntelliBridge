from fastapi import APIRouter, HTTPException, Depends, Request
# from fastapi.openapi.models import Response
from fastapi.responses import StreamingResponse, Response
import httpx
from typing import Dict
from models.mcp_service_model import McpServiceModel, ServiceStatus
from starlette.background import BackgroundTask
from utils.db import DatabaseManager
from utils.logger import log
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


async def stream_sse_data(url: str, endpoint: str):
    """
    流式传输 SSE 数据。

    Args:
        url: 目标 SSE 服务器 URL。
        endpoint: 当前服务器向外提供的endpoint

    Yields:
        来自 SSE 服务器的数据。
    """
    is_return_endpoint = True
    try:
        async with httpx.AsyncClient(timeout=None) as client:  # timeout=None 保持长连接
            async with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as response:
                async for chunk in response.aiter_bytes():
                    log.debug(f"sse流请求：{chunk}")
                    if is_return_endpoint:
                        chunk_str = chunk.decode('utf-8')
                        if '/message' in chunk_str:
                            chunk_str = chunk_str.replace('/message', f'/{endpoint}/message')
                            chunk = chunk_str.encode('utf-8')
                        is_return_endpoint = False
                    yield chunk
    except httpx.RequestError as e:
        log.error(f"Error connecting to SSE server: {e}")
        yield f"data: Error connecting to SSE server: {e}\n\n".encode('utf-8')  # 发送错误信息给客户端
    except httpx.ReadTimeout as e:
        log.error(f"Timeout reading from SSE server: {e}")
        yield f"data: Timeout reading from SSE server: {e}\n\n".encode('utf-8')  # 发送错误信息给客户端
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        yield f"data: Unexpected error: {e}\n\n".encode('utf-8')  # 发送错误信息给客户端


async def forward_sse_request(request: Request, target_url: str, endpoint: str):
    return StreamingResponse(
        stream_sse_data(target_url, endpoint),
        media_type="text/event-stream",
    )


async def forward_request(request: Request, target_url: str):
    log.debug(f"开始转发请求到: {target_url}")
    async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:  # 禁用自动重定向
        try:
            # 获取原始请求的方法、头部
            method = request.method
            headers = dict(request.headers)

            # 移除可能导致问题的头部
            headers.pop("host", None)
            headers.pop("content-length", None)
            # 获取原始请求的query参数
            params = dict(request.query_params)

            # 获取请求体(如果存在)
            body = None
            if method in ["POST", "PUT", "PATCH"]:
                body = await request.body()
                log.debug(f"message 请求体: {body.decode('utf-8')}")  # 记录请求体 (debug 级别)

            # 转发请求
            response = await client.request(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
                params=params,
            )
            log.debug(f"请求转发成功: {target_url}, 状态码: {response.status_code}")

            if 300 <= response.status_code < 400:
                # 手动处理重定向
                redirect_url = response.headers.get("location")
                if redirect_url:
                    log.info(f"手动处理重定向到: {redirect_url}")
                    # 需要递归调用 forward_request 或者构建一个新的请求到 redirect_url
                    # 为了避免无限循环，可能需要限制重定向的次数
                    return Response(status_code=response.status_code, headers=dict(response.headers)) # 直接返回重定向的 response, 不需要streaming
                else:
                    log.warning("重定向响应缺少 Location 头部")
                    raise HTTPException(status_code=500, detail="重定向响应缺少 Location 头部")
            else:
                # 直接返回 httpx 的 response 对象
                return Response(content=response.content, status_code=response.status_code, headers=response.headers)

        except httpx.TimeoutException as e:
            log.error(f"请求超时: {str(e)}")
            raise HTTPException(status_code=504, detail="请求超时") # 504 Gateway Timeout
        except httpx.RequestError as e:
            log.error(f"请求转发失败: {str(e)}")
            raise HTTPException(status_code=502, detail="上游服务器错误") # 502 Bad Gateway
        except Exception as e:
            log.exception(f"未知错误: {str(e)}") # 打印完整的堆栈信息
            raise HTTPException(status_code=500, detail="内部服务器错误")


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
    log.info(f"收到代理请求: {endpoint}/{path}")
    # 查找对应的MCP服务
    service = mcp_service_model.get_service_by_endpoint(endpoint)
    if not service:
        log.error(f"代理请求失败：未找到端点 {endpoint} 对应的服务")
        raise HTTPException(status_code=404, detail=f"Service with endpoint {endpoint} not found")

    if service.status != ServiceStatus.ONLINE:
        log.warning(f"代理请求失败：服务 {service.name} 未激活")
        raise HTTPException(status_code=503, detail=f"Service {service.name} is not online")

    # 构建目标URL
    target_url = f"http://{service.ip}:{service.port}/{path}"
    if request.url.query is not None and len(request.url.query) > 0:
        real_target_url = f"{target_url}?{request.url.query}"
    else:
        real_target_url = f"{target_url}"
    log.debug(f"目标URL: {target_url},带query参数的目标URL：{real_target_url}")

    # 检查是否是 SSE 请求
    # accept_header = request.headers.get("accept", "")
    if "/sse" in real_target_url:
        log.debug(f"检测到SSE请求: {real_target_url}")
        return await forward_sse_request(request, real_target_url, endpoint)

    # 普通请求转发
    return await forward_request(request, real_target_url)
