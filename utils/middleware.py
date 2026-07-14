"""ASGI middleware shared by the combined FastAPI/MCP application."""

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict

from utils.timeouts import OperationTimeout, deadline_scope


ASGIApp = Callable[[Dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[Any]]


class RequestTimeoutMiddleware:
    """Apply one request deadline while preserving non-HTTP MCP scopes."""

    def __init__(self, app: ASGIApp, timeout_seconds: float = 300.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.app = app
        self.timeout_seconds = float(timeout_seconds)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        response_started = False

        async def send_wrapper(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            with deadline_scope(self.timeout_seconds):
                async with asyncio.timeout(self.timeout_seconds):
                    await self.app(scope, receive, send_wrapper)
        except OperationTimeout as exc:
            if not response_started:
                await self._send_timeout(send, str(exc))
        except asyncio.TimeoutError:
            if not response_started:
                await self._send_timeout(
                    send,
                    f"请求超时（限制 {self.timeout_seconds:g} 秒），请稍后重试",
                )

    async def _send_timeout(self, send, detail: str) -> None:
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 504,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
