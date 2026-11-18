"""Verify the proxy against marimo code mode without starting database services.

Run from the repository root with IntelliBridge's Python environment:
    python scripts/verify_marimo_proxy.py --url http://marimo:2718/mcp/server
An upstream token can be supplied through MARIMO_TOKEN.
"""

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn

from tools.marimo_mcp import MARIMO_PATH, MarimoSettings, create_marimo_app
from utils.middleware import RequestTimeoutMiddleware


async def verify(args):
    settings = MarimoSettings(enabled=True, url=args.url,
                              token=os.environ.get("MARIMO_TOKEN", ""), timeout=60)
    proxy = create_marimo_app(settings)
    app = Starlette(routes=[Route(MARIMO_PATH, proxy)], lifespan=proxy.lifespan)
    app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=0.01,
                       path_timeouts={MARIMO_PATH: settings.timeout})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}{MARIMO_PATH}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Proxy stopped before startup")
                await asyncio.sleep(0.01)
        async with Client(address) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {"list_sessions", "execute_code"}

            token = settings.token.get_secret_value()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with Client(StreamableHttpTransport(str(settings.url), headers=headers)) as upstream:
                upstream_tools = await upstream.list_tools()
            assert {tool.name: tool.model_dump() for tool in tools} == {
                tool.name: tool.model_dump() for tool in upstream_tools
            }, "The upstream tool definitions differ from the local marimo code-mode catalogue"

            result = await client.call_tool_mcp("list_sessions", {})
            assert not result.isError, result
            assert isinstance(result.structuredContent["sessions"], list)
        evidence = {
            "status": "PASS", "endpoint": MARIMO_PATH, "tools": [tool.name for tool in tools],
            "upstream_auth_configured": bool(token),
            "fastmcp": importlib.metadata.version("fastmcp"),
            "mcp": importlib.metadata.version("mcp"),
            "tool_definitions_match_upstream": True,
            "session_count": len(result.structuredContent["sessions"]),
            "scope": "Real marimo code-mode tool definitions and list_sessions through the proxy",
        }
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Upstream marimo MCP endpoint")
    parser.add_argument("--output", type=Path, default=Path("/tmp/marimo-proxy-verification.json"))
    asyncio.run(verify(parser.parse_args()))
