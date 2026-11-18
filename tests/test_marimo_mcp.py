import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from tools.marimo_mcp import MARIMO_PATH, MarimoSettings, _configure, create_marimo_app
from utils.config import ConfigManager
from utils.middleware import RequestTimeoutMiddleware


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Test server stopped before startup")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            sock.close()


class MarimoConfigurationTests(unittest.TestCase):
    def test_disabled_and_invalid_settings_leave_other_services_available(self):
        for raw in ({}, {"enabled": "false"}, {"enabled": True}, {"enabled": True, "url": None},
                    {"enabled": True, "url": "bad"},
                    {"enabled": True, "url": "http://localhost/mcp", "timeout": 0}):
            with self.subTest(raw=raw), patch("tools.marimo_mcp.config.get", return_value=raw):
                routes, _, timeouts = _configure()
                self.assertEqual(routes, [])
                self.assertEqual(timeouts, {})

    def test_credentials_and_nonfinite_budgets_are_rejected_without_logging_secrets(self):
        for url in ("http://user:secret@localhost/mcp/server",
                    "http://localhost/mcp/server?access_token=secret"):
            with self.assertRaises(ValidationError):
                MarimoSettings(url=url)
        for timeout in (float("inf"), float("nan"), -1):
            with self.assertRaises(ValidationError):
                MarimoSettings(timeout=timeout)
        with patch("tools.marimo_mcp.config.get", return_value={
            "enabled": True, "url": "http://localhost/mcp?access_token=secret",
            "token": "private-token",
        }), patch("tools.marimo_mcp.logger.warning") as warning:
            _configure()
            self.assertNotIn("secret", str(warning.call_args))
            self.assertNotIn("private-token", str(warning.call_args))


class MarimoForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_catalogue_and_code_mode_calls(self):
        upstream = FastMCP("upstream")
        config_path = Path(self.enterContext(TemporaryDirectory())) / "config.yaml"

        @upstream.tool
        def list_sessions() -> dict:
            return {"sessions": [{"name": "notebook", "path": "notebook.py", "session_id": "test"}]}

        @upstream.tool
        async def execute_code(session_id: str, code: str, ctx: Context) -> dict:
            if code == "fail":
                raise ValueError("中文异常：'quote' / \\ 路径")
            await ctx.report_progress(1, 2, "正在读取")
            await asyncio.sleep(0.04)
            await ctx.report_progress(2, 2, "读取完成")
            return {"success": True, "stdout": [session_id, code], "stderr": [], "errors": []}

        for token in ("upstream-token", ""):
            with self.subTest(authenticated=bool(token)):
                seen_headers = []
                upstream_messages = []

                async def record_headers(scope, receive, send):
                    if scope["type"] == "http":
                        authorization = dict(scope["headers"]).get(b"authorization", b"")
                        seen_headers.append(authorization)
                        if token and authorization != f"Bearer {token}".encode():
                            return await JSONResponse({"detail": "Unauthorized"}, 401)(scope, receive, send)
                    if scope.get("method") != "POST":
                        return await upstream_app(scope, receive, send)

                    body = await Request(scope, receive).body()
                    upstream_messages.append(json.loads(body))

                    async def replay_body():
                        nonlocal body
                        if body is None:
                            return await receive()
                        message = {"type": "http.request", "body": body, "more_body": False}
                        body = None
                        return message

                    await upstream_app(scope, replay_body, send)

                # Recreate each MCP app because its session manager has one lifespan.
                upstream_path = "/test-upstream/mcp"
                upstream_app = upstream.http_app(path=upstream_path, stateless_http=True)
                async with serve(record_headers) as upstream_url:
                    ConfigManager(config_path).save_config({"marimo": {
                        "enabled": True,
                        "url": upstream_url + upstream_path,
                        "token": token,
                        "timeout": 2,
                    }})
                    with patch("tools.marimo_mcp.config", ConfigManager(config_path)):
                        routes, lifespan, timeouts = _configure()
                    combined = Starlette(routes=routes, lifespan=lifespan)
                    combined.add_middleware(RequestTimeoutMiddleware, timeout_seconds=0.01,
                                            path_timeouts=timeouts)
                    async with serve(combined) as proxy_url:
                        transport = StreamableHttpTransport(proxy_url + MARIMO_PATH,
                            headers={"Authorization": "Bearer client-only-token"})
                        async with Client(transport) as client:
                            tools = await client.list_tools()
                            self.assertEqual({t.name for t in tools}, {"list_sessions", "execute_code"})
                            execute = next(t for t in tools if t.name == "execute_code")
                            self.assertEqual(execute.inputSchema["required"], ["session_id", "code"])
                            self.assertEqual(upstream_messages, [])
                            missing = await client.call_tool_mcp("get_active_notebooks", {})
                            self.assertTrue(missing.isError)
                            invalid = await client.call_tool_mcp("execute_code", {"code": "pass"})
                            self.assertTrue(invalid.isError)
                            self.assertEqual(upstream_messages, [])

                            sessions = await client.call_tool_mcp("list_sessions", {})
                            self.assertEqual(sessions.structuredContent["sessions"][0]["session_id"], "test")
                            progress = []

                            async def on_progress(value, total, message):
                                progress.append((value, total, message))

                            result = await client.call_tool_mcp(
                                "execute_code", {"session_id": "test", "code": "中文 # / %"},
                                progress_handler=on_progress,
                            )
                            self.assertEqual(result.structuredContent["stdout"], ["test", "中文 # / %"])
                            self.assertTrue(any(item[2] == "读取完成" for item in progress))
                            failed = await client.call_tool_mcp("execute_code", {"session_id": "test", "code": "fail"})
                            self.assertTrue(failed.isError)
                            self.assertIn("中文异常", failed.content[0].text)
                            again = await client.call_tool_mcp("execute_code", {"session_id": "test", "code": "still usable"})
                            self.assertEqual(again.structuredContent["stdout"], ["test", "still usable"])
                methods = [message["method"] for message in upstream_messages]
                self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"] * 4)
                self.assertTrue(seen_headers)
                expected = f"Bearer {token}".encode() if token else b""
                self.assertEqual(set(seen_headers), {expected})

    async def test_failed_upstream_does_not_hide_tools(self):
        async def reject(request):
            return Response("Invalid Host header", status_code=421)

        async with serve(Starlette(routes=[Route("/mcp/server", reject, methods=["POST"])])) as upstream_url:
            proxy = create_marimo_app(MarimoSettings(enabled=True, url=upstream_url + "/mcp/server"))
            async with serve(Starlette(routes=[Route(MARIMO_PATH, proxy)], lifespan=proxy.lifespan)) as proxy_url:
                async with Client(proxy_url + MARIMO_PATH, timeout=12) as client:
                    self.assertEqual(len(await client.list_tools()), 2)
                    result = await client.call_tool_mcp("list_sessions", {})
                    self.assertTrue(result.isError)
                    self.assertIn("HTTP 421", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
