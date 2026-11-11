"""内置 Datalayer Jupyter MCP Server。

把 jupyter-mcp-server 作为 IntelliBridge 的一个内置 MCP 端点（/mcp/jupyter），沿用仓库
既有约定：产出 Starlette 路由合并进 combined_app，lifespan 参与 combine_lifespans，
不使用 Starlette Mount（Mount 会保留内层 /mcp，形成 /mcp/jupyter/mcp 双层路径）。

配置只读 config.yaml 的 jupyter 段 5 项（enabled/url/token/kernel_id/execution_timeout），
其余全部硬编码。任何失败（未启用、url 非法、不可达、挂载异常）都只记 WARN，退回空路由 +
noop lifespan，绝不让 IntelliBridge 起不来。
"""

from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import requests

from utils.config import config
from utils.logger import logger

# --- 硬编码项（刻意不进配置） ------------------------------------------------
JUPYTER_PATH = "/mcp/jupyter"      # MCP 端点路径，单层
CONNECT_TIMEOUT = 5.0              # 启动期连通性探测超时（秒）
MAX_EXECUTION_TIMEOUT = 3600       # 单次执行硬上限（秒）
TRANSPORT = "streamable-http"
DOCUMENT_PROVIDER = "jupyter"
# 钉死为 jupyter-server：绝不落到 "local"（那会在 IntelliBridge 容器内起 kernel，
# 违背「分析代码只在 Jupyter Kernel 中运行」的数据面约定）。
SANDBOX_VARIANT = "jupyter-server"


@asynccontextmanager
async def _noop_lifespan(app):
    """未挂载时的占位 lifespan，使 main.py 可以无条件参与 combine_lifespans。"""
    yield


jupyter_routes: list = []
jupyter_lifespan = _noop_lifespan


def _cfg(key: str, default=None):
    """按仓库点分键约定读取 jupyter 段配置。"""
    return config.get(f"jupyter.{key}", default)


def _as_bool(value, default: bool) -> bool:
    """宽松解析布尔配置：YAML 裸布尔直返，字符串/数字按常见写法判定。

    `enabled: "false"`（带引号）若直接 bool() 会变成 True，功能被意外开启。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value, default: int, name: str) -> int:
    """宽松解析整数配置；非法值回退默认值并 WARN，绝不让 import 期抛异常。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("jupyter.{} 非法（{!r}），回退 {}s", name, value, default)
        return default


def _as_optional_str(value) -> Optional[str]:
    """可选字符串配置：空串 / "None" / "null"（含引号写法）一律视为未配置。"""
    text = str(value).strip() if value is not None else ""
    return None if text.lower() in ("", "none", "null") else text


def _probe(url: str, token: Optional[str]) -> None:
    """启动期连通性探测；失败抛异常，由调用方降级。"""
    headers = {"Authorization": f"token {token}"} if token else {}
    resp = requests.get(f"{url}/api/status", headers=headers, timeout=CONNECT_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"GET /api/status 返回 HTTP {resp.status_code}")


def _init() -> None:
    global jupyter_routes, jupyter_lifespan

    if not _as_bool(_cfg("enabled", False), False):
        logger.info("jupyter.enabled=false，跳过 Jupyter MCP 挂载")
        return

    raw_url = _cfg("url") or ""
    url = str(raw_url).strip().rstrip("/")
    parsed = urlparse(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.hostname:
        logger.warning("Jupyter MCP 未挂载：jupyter.url 非法（{!r}）", raw_url)
        return

    token = _as_optional_str(_cfg("token"))
    kernel_id = _as_optional_str(_cfg("kernel_id"))
    # 配置写错（"30s"/""/"None"）时回退默认值，不能抛到 import 期
    execution_timeout = _as_int(_cfg("execution_timeout", 30), 30, "execution_timeout")

    try:
        _probe(url, token)
    except Exception as exc:
        logger.warning("Jupyter MCP 未挂载：{} 不可达（{}）", url, exc)
        return

    try:
        from jupyter_mcp_server.config import set_config
        from jupyter_mcp_server.server import mcp as jupyter_mcp
        from mcp.server.transport_security import TransportSecuritySettings

        cfg = set_config(
            transport=TRANSPORT,
            code_sandbox_url=url,
            code_sandbox_token=token,
            code_sandbox_id=kernel_id,
            document_url=url,
            document_provider=DOCUMENT_PROVIDER,
            sandbox_variant=SANDBOX_VARIANT,
            start_new_code_sandbox=False,
            execution_timeout=execution_timeout,
            max_execution_timeout=MAX_EXECUTION_TIMEOUT,
            jupyterlab=False,
            open_notebook_in_ui=False,
        )
        # set_config 对未知键静默忽略（仅 hasattr 才 setattr），因此回读校验。
        # 这里用显式 if 而非 assert：assert 在 python -O 下会被剥掉。
        if cfg.code_sandbox_url != url or cfg.transport != TRANSPORT:
            logger.warning(
                "Jupyter MCP 未挂载：set_config 未生效（url={!r}, transport={!r}）",
                cfg.code_sandbox_url,
                cfg.transport,
            )
            return

        # 关闭 DNS rebinding 保护，理由：
        # 1) 现有 8 个 MCP 端点（fastmcp 3.x）本就没有这层校验，关掉才与既有行为一致；
        # 2) mcp SDK 的 allowed_hosts 只支持精确值或 "host:*" 前缀，没有 "*" 通配，
        #    内网按 IP 访问（如 10.x.x.x:49000）会被 421 Invalid Host header 挡住，
        #    逐条列举真实访问 host 又与「配置只有 5 项」冲突；
        # 3) 本期不做鉴权，隔离依赖网络层（仅本机 / 隔离测试网开放）。
        # 将来要开保护：需同时补 allowed_hosts 配置项，并逐个列出真实访问 host。
        jupyter_mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        # 路径只能改 settings：streamable_http_app() 不接受 path 参数，内部路径默认 "/mcp"。
        jupyter_mcp.settings.streamable_http_path = JUPYTER_PATH
        # 只能调用一次：每次调用都会新建 StreamableHTTPSessionManager，其 run() 只允许一次。
        app = jupyter_mcp.streamable_http_app()

        # 只合并 MCP 路由。该 app 还带有 /api/connect、/api/stop、/api/healthz 管理路由，
        # 而路由合并是复制 Route 对象、jmcp 的中间件（管理路由保护、Identity、CORS）不会随行，
        # 带进宿主会变成 /api 命名空间下未鉴权的状态变更入口。
        wanted = {JUPYTER_PATH, JUPYTER_PATH + "/"}
        jupyter_routes = [r for r in app.routes if getattr(r, "path", None) in wanted]
        if not jupyter_routes:
            # 路径没生效（例如 SDK 改了内部路由形状）时不能挂个空壳还声称成功。
            logger.warning(
                "Jupyter MCP 未挂载：{} 路径过滤结果为空（实际路由：{}）",
                JUPYTER_PATH,
                [getattr(r, "path", None) for r in app.routes],
            )
            jupyter_lifespan = _noop_lifespan
            return
        # mcp SDK 返回纯 Starlette，没有 .lifespan（fastmcp 的 http_app 才有），故取 router 上的。
        jupyter_lifespan = app.router.lifespan_context
    except Exception:
        logger.opt(exception=True).warning("Jupyter MCP 挂载失败，IntelliBridge 继续启动")
        jupyter_routes, jupyter_lifespan = [], _noop_lifespan
        return

    logger.info(
        "Jupyter MCP 已挂载于 {}（{} 条路由，kernel_id={}，execution_timeout={}s）",
        JUPYTER_PATH,
        len(jupyter_routes),
        kernel_id or "由调用方传入",
        execution_timeout,
    )


_init()
