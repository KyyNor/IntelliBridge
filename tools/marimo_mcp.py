"""Expose a separately running marimo MCP server through IntelliBridge."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.routing import Route

from utils.config import config
from utils.logger import logger

MARIMO_PATH = "/mcp/marimo"


class MarimoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:2718/mcp/server")
    token: SecretStr = SecretStr("")
    timeout: float = Field(default=600, gt=0, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use marimo.token for credentials; URL must not contain a query or fragment")
        return value


def create_marimo_app(settings: MarimoSettings):
    """Build a lazy proxy; creating it does not connect to marimo."""
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy
    from fastmcp.server.providers.proxy import ProxyClient

    token = settings.token.get_secret_value()
    # FastMCP 3.1 forwards inbound Authorization by default. Always override it,
    # including for anonymous upstreams, so client credentials stay on this hop.
    transport = StreamableHttpTransport(
        str(settings.url),
        headers={"Authorization": f"Bearer {token}" if token else ""},
    )
    backend = ProxyClient(transport, timeout=settings.timeout, init_timeout=10)
    proxy = create_proxy(backend, name="marimo")
    return proxy.http_app(path=MARIMO_PATH, stateless_http=True)


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
    # An ASGI Route preserves FastMCP's HTTP context middleware without
    # stripping the path or introducing a second /mcp prefix through Mount.
    return [Route(MARIMO_PATH, endpoint=app)], app.lifespan, {
        MARIMO_PATH: settings.timeout,
        MARIMO_PATH + "/": settings.timeout,
    }


marimo_routes, marimo_lifespan, marimo_timeout_overrides = _configure()
