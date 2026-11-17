"""Verify the proxy against a running marimo without starting database services.

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
            names = [tool.name for tool in tools]
            for name in ("get_active_notebooks", "get_cell_outputs", "get_notebook_errors"):
                assert name in names, names
            result = await client.call_tool("get_active_notebooks", {"args": {}})
            assert not result.is_error, result
            assert result.structured_content["status"] == "success", result
        evidence = {
            "status": "PASS", "endpoint": MARIMO_PATH, "tools": names,
            "upstream_auth_configured": bool(settings.token.get_secret_value()),
            "fastmcp": importlib.metadata.version("fastmcp"),
            "mcp": importlib.metadata.version("mcp"),
            "get_active_notebooks": result.structured_content,
            "scope": "Real marimo through proxy routes/lifespan/deadline middleware; database services not started",
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
