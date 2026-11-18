"""Expose marimo code mode with a local MCP tool catalogue."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import (
    AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError, ValidationInfo,
    field_validator,
)
from starlette.routing import Route

from utils.config import config
from utils.logger import logger

MARIMO_PATH = "/mcp/marimo"


class MarimoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: AnyHttpUrl | None = Field(default=None, validate_default=True)
    token: SecretStr = SecretStr("")
    timeout: float = Field(default=600, gt=0, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: AnyHttpUrl | None, info: ValidationInfo) -> AnyHttpUrl | None:
        if value is None:
            if info.data.get("enabled"):
                raise ValueError("Set marimo.url when marimo is enabled")
            return None
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use marimo.token for credentials; URL must not contain a query or fragment")
        return value


class MarimoApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        await self.session_manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self, app):
        async with self.session_manager.run():
            yield


def create_marimo_app(settings: MarimoSettings) -> MarimoApp:
    """Serve definitions from marimo 0.25.2 code mode and forward tool calls."""
    if settings.url is None:
        raise ValueError("Set marimo.url to the code-mode MCP endpoint")

    catalogue_path = Path(__file__).with_name("marimo_code_tools.json")
    catalogue = types.ListToolsResult.model_validate_json(catalogue_path.read_bytes())
    tool_names = {tool.name for tool in catalogue.tools}
    server = Server("marimo")

    token = settings.token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx.Timeout(settings.timeout, connect=30.0)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return catalogue.tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        if name not in tool_names:
            raise ValueError(f"Unknown marimo code-mode tool: {name}")

        context = server.request_context
        progress_token = context.meta.progressToken if context.meta else None

        async def forward_progress(progress: float, total: float | None, message: str | None):
            await context.session.send_progress_notification(
                progress_token, progress, total, message, related_request_id=context.request_id,
            )

        request = types.ClientRequest(types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=arguments, _meta=context.meta),
        ))
        upstream_status = None

        async def record_http_error(response: httpx.Response):
            nonlocal upstream_status
            if response.is_error:
                upstream_status = response.status_code

        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout,
                                         event_hooks={"response": [record_http_error]}) as http:
                async with streamable_http_client(str(settings.url), http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        async with asyncio.timeout(10):
                            await client.initialize()
                        # ClientSession.call_tool may refresh the output schema with tools/list.
                        return await client.send_request(
                            request, types.CallToolResult,
                            progress_callback=forward_progress if progress_token is not None else None,
                        )
        except Exception as exc:
            if upstream_status is not None:
                raise RuntimeError(f"marimo upstream returned HTTP {upstream_status}") from None
            logger.warning("Marimo MCP 上游调用 {} 失败（{}）", name, type(exc).__name__)
            raise RuntimeError("marimo upstream MCP request failed") from None

    return MarimoApp(StreamableHTTPSessionManager(server, stateless=True))


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def _configure():
    try:
        settings = MarimoSettings.model_validate(config.get("marimo", {}))
        if not settings.enabled:
            return [], _noop_lifespan, {}
        app = create_marimo_app(settings)
    except ValidationError as exc:
        # Validation errors include their inputs by default, potentially tokens.
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
        logger.warning("Marimo MCP 未挂载：配置字段无效 {}", fields)
        return [], _noop_lifespan, {}
    except Exception as exc:
        logger.warning("Marimo MCP 未挂载：初始化失败（{}）", type(exc).__name__)
        return [], _noop_lifespan, {}
    logger.info("Marimo MCP 已挂载于 {}，请求期限 {}s", MARIMO_PATH, settings.timeout)
    return [Route(MARIMO_PATH, endpoint=app)], app.lifespan, {
        MARIMO_PATH: settings.timeout,
        MARIMO_PATH + "/": settings.timeout,
    }


marimo_routes, marimo_lifespan, marimo_timeout_overrides = _configure()
