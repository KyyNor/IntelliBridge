"""Expose marimo code mode with a local MCP tool catalogue."""

import asyncio
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from pydantic import (
    AnyHttpUrl, BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, ValidationError, ValidationInfo,
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


class MarimoNotebookInfo(BaseModel):
    name: str
    path: str
    session_id: str


class ListSessionsResult(BaseModel):
    sessions: list[MarimoNotebookInfo]


class CodeExecutionResult(BaseModel):
    success: bool
    output: str | None = None
    stdout: list[str]
    stderr: list[str]
    errors: list[str]
    error: str | None = None
    output_mimetype: Literal[
        "application/json",
        "application/vnd.marimo+error",
        "application/vnd.marimo+traceback",
        "application/vnd.marimo+mimebundle",
        "application/vnd.vega.v5+json",
        "application/vnd.vegalite.v5+json",
        "application/vnd.vega.v6+json",
        "application/vnd.vegalite.v6+json",
        "application/vnd.jupyter.widget-view+json",
        "image/png",
        "image/svg+xml",
        "image/tiff",
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "video/mp4",
        "video/mpeg",
        "text/html",
        "text/plain",
        "text/password",
        "text/markdown",
        "text/latex",
        "text/csv",
    ] | None = None


class MarimoToolResult(ToolResult):
    """Preserve the upstream MCP result, including its isError flag."""

    _result: types.CallToolResult = PrivateAttr()

    def __init__(self, result: types.CallToolResult):
        super().__init__(content=result.content, structured_content=result.structuredContent, meta=result.meta)
        self._result = result

    def to_mcp_result(self) -> types.CallToolResult:
        return self._result


def create_marimo_app(settings: MarimoSettings):
    if settings.url is None:
        raise ValueError("Set marimo.url to the code-mode MCP endpoint")

    marimo_mcp = FastMCP("IntelliBridge marimo")
    url = str(settings.url)
    token = settings.token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    timeout = httpx.Timeout(settings.timeout, connect=30.0)

    async def call_upstream(name: str, arguments: dict[str, str], ctx: Context) -> MarimoToolResult:
        context = ctx.request_context
        meta = context.meta if context else None
        request = types.ClientRequest(types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=arguments, _meta=meta),
        ))
        upstream_status = None

        async def record_http_error(response: httpx.Response):
            nonlocal upstream_status
            if response.is_error:
                upstream_status = response.status_code

        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout,
                                         event_hooks={"response": [record_http_error]}) as http:
                async with streamable_http_client(url, http_client=http) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        async with asyncio.timeout(10):
                            await client.initialize()
                        # ClientSession.call_tool may refresh the output schema with tools/list.
                        result = await client.send_request(
                            request, types.CallToolResult,
                            progress_callback=ctx.report_progress if meta and meta.progressToken is not None else None,
                        )
        except Exception as exc:
            if upstream_status is not None:
                raise ToolError(f"marimo upstream returned HTTP {upstream_status}") from None
            logger.warning("Marimo MCP 上游调用 {} 失败（{}）", name, type(exc).__name__)
            raise ToolError("marimo upstream MCP request failed") from None

        return MarimoToolResult(result)

    @marimo_mcp.tool(name="list_sessions", output_schema=ListSessionsResult.model_json_schema())
    async def list_sessions(ctx: Context) -> ToolResult:
        """List active marimo notebook sessions and their session IDs."""
        return await call_upstream("list_sessions", {}, ctx)

    @marimo_mcp.tool(name="execute_code", output_schema=CodeExecutionResult.model_json_schema())
    async def execute_code(session_id: str, code: str, ctx: Context) -> ToolResult:
        """Execute Python code in the specified marimo notebook's kernel scratchpad."""
        return await call_upstream("execute_code", {"session_id": session_id, "code": code}, ctx)

    return marimo_mcp.http_app(path=MARIMO_PATH, stateless_http=True)


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
