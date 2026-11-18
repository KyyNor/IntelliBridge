import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
import uvicorn

from tools.marimo_mcp import MARIMO_PATH, MarimoSettings, _configure
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
    async def test_native_tools_progress_errors_and_upstream_credentials(self):
        upstream = FastMCP("upstream")
        config_path = Path(self.enterContext(TemporaryDirectory())) / "config.yaml"

        @upstream.tool
        async def native_echo(text: str, ctx: Context) -> dict:
            await ctx.report_progress(1, 2, "正在读取")
            await asyncio.sleep(0.04)
            await ctx.report_progress(2, 2, "读取完成")
            return {"text": text}

        @upstream.tool
        def native_fail() -> str:
            raise ValueError("中文异常：'quote' / \\ 路径")

        for token in ("upstream-token", ""):
            with self.subTest(authenticated=bool(token)):
                seen_headers = []

                async def record_headers(scope, receive, send):
                    if scope["type"] == "http":
                        authorization = dict(scope["headers"]).get(b"authorization", b"")
                        seen_headers.append(authorization)
                        if token and authorization != f"Bearer {token}".encode():
                            return await JSONResponse({"detail": "Unauthorized"}, 401)(scope, receive, send)
                    await upstream_app(scope, receive, send)

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
                            self.assertEqual({t.name for t in tools}, {"native_echo", "native_fail"})
                            echo = next(t for t in tools if t.name == "native_echo")
                            self.assertEqual(echo.inputSchema["properties"]["text"]["type"], "string")
                            progress = []

                            async def on_progress(value, total, message):
                                progress.append((value, total, message))

                            result = await client.call_tool("native_echo", {"text": "中文 # / %"},
                                                           progress_handler=on_progress)
                            self.assertEqual(result.structured_content, {"text": "中文 # / %"})
                            self.assertTrue(any(item[2] == "读取完成" for item in progress))
                            failed = await client.call_tool("native_fail", raise_on_error=False)
                            self.assertTrue(failed.is_error)
                            self.assertIn("中文异常", failed.content[0].text)
                            again = await client.call_tool("native_echo", {"text": "still usable"})
                            self.assertEqual(again.structured_content, {"text": "still usable"})
                self.assertTrue(seen_headers)
                expected = f"Bearer {token}".encode() if token else b""
                self.assertEqual(set(seen_headers), {expected})


if __name__ == "__main__":
    unittest.main()
