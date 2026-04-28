"""
FineReport 工具包
提供 FineReport 报表登录、控件筛选、Excel下载和结构化数据提取能力。
以 FastMCP 工具和 REST API 双重形式对外提供服务。
"""
import os
import sys
import time
import uuid
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Dict, List, Any

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Download

from utils.config import config
from utils.logger import logger
from utils.decorators import log_function_info
from utils.cache import cache as cache_manager

from fastmcp import FastMCP
from fastapi import APIRouter
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 全局面板浏览器管理（通过 CDP URL 连接远端 Chrome，同 fine_report_snapshot）
# ---------------------------------------------------------------------------

_global_pw = None
_global_browser: Optional[Browser] = None
_global_context: Optional[BrowserContext] = None
_logged_in: bool = False


def _get_browser_context() -> BrowserContext:
    """获取（或初始化）全局 BrowserContext，通过 CDP 连接远端浏览器"""
    global _global_pw, _global_browser, _global_context

    if _global_browser is None:
        cdp_url = config.get("fine_report.cdp_url")
        if not cdp_url:
            raise RuntimeError("fine_report.cdp_url 配置缺失，请在 config/config.yaml 中配置")

        logger.info(f"通过 CDP 连接远端浏览器: {cdp_url}")
        _global_pw = sync_playwright().start()
        _global_browser = _global_pw.chromium.connect_over_cdp(cdp_url)
        _global_context = _global_browser.new_context()
        logger.info("远端浏览器连接成功")

    return _global_context


def _is_on_login_page(page_or_url) -> bool:
    """判断当前页面或 URL 是否处于登录页（URL 必须以 /login 结尾，防止误判）"""
    url = getattr(page_or_url, "url", page_or_url)
    return url.rstrip("/").endswith("/login")


def _login_once(page: Page) -> None:
    """检测到 /login 页面时，强制跳转 SSO 登录页、完成认证、切新环境（SSO 场景专用）"""
    username = config.get("fine_report.username")
    password = config.get("fine_report.password")
    login_url = config.get("fine_report.login_url")

    if not username or not password or not login_url:
        raise RuntimeError("FineReport login_url/username/password 配置缺失")

    logger.info(f"检测到 /login 页面，主动跳转 SSO 登录页: {login_url}")
    page.goto(login_url, wait_until="networkidle")
    page.fill("#inputUsername", username)
    page.fill("#inputPassword", password)
    page.click("#submitBtn")
    page.wait_for_load_state("networkidle")
    logger.info("SSO 登录完成")

    time.sleep(2)

    # 点"点我前往新环境"
    try:
        iframe_el = page.frame_locator('iframe')
        iframe_el.locator('text="点我前往新环境"').click()
        logger.info("已点击「新环境」入口")
    except Exception:
        pass

    time.sleep(2)


def _ensure_logged_in(page: Page) -> None:
    """确保当前 Page 已登录；若已在登录页则触发 SSO 登录流程"""
    global _logged_in
    if _is_on_login_page(page.url):
        logger.info("页面仍停留在登录页，触发 SSO 登录流程")
        _login_once(page)
    _logged_in = True


# ---------------------------------------------------------------------------
# 文件等待（来自 fine_report_snapshot.py，原样移植）
# ---------------------------------------------------------------------------

def _wait_for_file_stable(
    filepath: str,
    stable_seconds: float = 3,
    timeout: float = 600,
    check_interval: float = 1,
) -> bool:
    """
    监听文件大小，等待其连续 stable_seconds 保持不变，视为下载完成。
    用于配合 expect_download 兜底，确保大文件传输完毕。
    """
    last_size = None
    stable_start = None
    start_time = time.time()

    while True:
        if time.time() - start_time > timeout:
            raise TimeoutError(f"等待文件稳定超时，当前大小: {last_size}")

        if not os.path.exists(filepath):
            logger.debug("文件尚不存在，等待创建...")
            last_size = None
            stable_start = None
            time.sleep(check_interval)
            continue

        current_size = os.path.getsize(filepath)

        if last_size is None:
            last_size = current_size
            logger.debug(f"首次检测文件大小: {current_size} bytes")
        elif current_size == last_size:
            if stable_start is None:
                stable_start = time.time()
            elapsed = time.time() - stable_start
            if elapsed >= stable_seconds:
                logger.info(f"文件下载完成，稳定 {stable_seconds}s，最终大小: {current_size}")
                return True
        else:
            if current_size > last_size:
                logger.debug(f"文件仍在写入: {current_size} bytes (+{current_size - last_size})")
            last_size = current_size
            stable_start = None

        time.sleep(check_interval)


# ---------------------------------------------------------------------------
# 日期控件自动填充（来自 fine_report_snapshot.py，原样移植）
# ---------------------------------------------------------------------------

def _auto_fill_date_control(page: Page, target_date: str) -> None:
    """
    利用 JavaScript 自动识别报表中最可能的时间控件，并将 target_date 填入。

    识别策略：
    - 枚举所有 datetime 类型控件
    - 若某个控件的当前值落在最近5天内，认定为候选，再用 date_widget_names 决定优先级
    - 若无匹配，则按遍历顺序取第一个
    """
    if not target_date:
        logger.info("未指定 target_date，跳过日期控件填充")
        return

    date_widget_names_cfg = config.get(
        "fine_report.date_widget_names",
        ["date", "sjdate", "etl_date", "import_date_tag"],
    )
    date_widget_list_js = json.dumps(date_widget_names_cfg)

    script = f"""
    (function() {{
        var date_widget_list = {date_widget_list_js};
        var widgets = _g().getParameterContainer().getAllWidgets();

        var candidates = [];
        for (var w_name in widgets) {{
            var widget = widgets[w_name];
            if (widget.options && widget.options.type === 'datetime') {{
                var idx = date_widget_list.indexOf(w_name.toLowerCase());
                candidates.push({{
                    name: w_name,
                    value: widget.getValue(),
                    listIndex: idx === -1 ? Infinity : idx
                }});
            }}
        }}

        if (candidates.length === 0) {{
            return '';   // 无 datetime 控件
        }}

        var today = new Date();
        var fiveDaysSet = {{}};
        for (var i = 0; i < 6; i++) {{
            var d = new Date(today);
            d.setDate(today.getDate() - i);
            var yyyy = d.getFullYear();
            var mm = ('0' + (d.getMonth() + 1)).slice(-2);
            var dd = ('0' + d.getDate()).slice(-2);
            fiveDaysSet[yyyy + '-' + mm + '-' + dd] = true;
        }};

        var matchedWithValue = [];
        for (var j = 0; j < candidates.length; j++) {{
            var item = candidates[j];
            if (item.value !== null && item.value !== undefined && item.value !== '') {{
                var valStr = '';
                if (typeof item.value === 'object' && item.value instanceof Date) {{
                    var y = item.value.getFullYear();
                    var m = ('0' + (item.value.getMonth() + 1)).slice(-2);
                    var da = ('0' + item.value.getDate()).slice(-2);
                    valStr = y + '-' + m + '-' + da;
                }} else {{
                    valStr = String(item.value).substring(0, 10);
                }};
                if (fiveDaysSet[valStr]) {{
                    matchedWithValue.push(item);
                }};
            }};
        }};

        if (matchedWithValue.length > 0) {{
            matchedWithValue.sort(function(a, b) {{
                if (a.listIndex !== b.listIndex) return a.listIndex - b.listIndex;
                return 0;
            }});
            return matchedWithValue[0].name;
        }};

        return candidates[0].name;
    }})()
    """

    try:
        date_widget_name = page.evaluate(script)
        if not date_widget_name:
            logger.warning("未找到 datetime 类型控件，跳过自动日期填充")
            return

        logger.info(f"自动选中日期控件: {date_widget_name}，目标日期: {target_date}")
        fill_script = f"""
        _g().getParameterContainer().getWidgetByName("{date_widget_name}").setValue("{target_date}");
        _g().parameterCommit();
        """
        page.evaluate(fill_script)
        page.wait_for_load_state("networkidle")
        logger.info("日期控件填充并提交完成")
    except Exception as e:
        logger.warning(f"日期控件自动填充失败: {e}")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def excel_column_to_index(column: str) -> int:
    """
    将 Excel 列名（A/B/…/Z/AA/AB/…）转换为从 0 开始的数字索引。

    >>> excel_column_to_index('A')
    0
    >>> excel_column_to_index('C')
    2
    >>> excel_column_to_index('AA')
    26
    """
    result = 0
    for char in column.upper():
        if not char.isalpha():
            raise ValueError(f"无效的 Excel 列名: {column}")
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1


def extract_data_from_excel(excel_path: str, locators: Dict[str, Any]) -> Dict[str, str]:
    """
    从 Excel 文件中根据定位规则提取数据。

    支持两种 locator 格式：
      - 简单单元格: {'balance': 'C5'}  →  直接返回该格子内容
      - 条件查找:   {'balance': {'find_column': 'A', 'find_value': '汉口银行',
                              'return_column': 'C'}}
                  → 在 find_column 中找含有 find_value 的行，返回对应 return_column 的值
    """
    import pandas as pd  # 局部导入，减少顶层依赖感

    df = pd.read_excel(excel_path, header=None)
    result: Dict[str, str] = {}

    for key, locator in locators.items():
        # 简单格式：单个单元格引用字符串 'C5'
        if isinstance(locator, str) and not isinstance(locator, dict):
            locator_clean = locator.strip()
            if len(locator_clean) < 2:
                result[key] = ""
                continue
            col_part = "".join(c for c in locator_clean if c.isalpha())
            row_part = "".join(c for c in locator_clean if c.isdigit())
            if not col_part or not row_part:
                result[key] = ""
                continue
            try:
                col_idx = excel_column_to_index(col_part)
                row_idx = int(row_part) - 1
                if 0 <= row_idx < len(df) and 0 <= col_idx < len(df.columns):
                    val = df.iloc[row_idx, col_idx]
                    result[key] = str(val) if not (isinstance(val, float) and val != val) else ""
                else:
                    result[key] = ""
            except Exception:
                result[key] = ""
            continue

        # 条件查找格式
        if not isinstance(locator, dict):
            result[key] = ""
            continue

        find_col = locator.get("find_column", "")
        find_val = str(locator.get("find_value", ""))
        ret_col = locator.get("return_column", "")

        if not find_col or not ret_col:
            result[key] = ""
            continue

        try:
            find_col_idx = excel_column_to_index(find_col)
            ret_col_idx = excel_column_to_index(ret_col)
        except ValueError as e:
            logger.warning(f"定位器 '{key}' 列名错误: {e}")
            result[key] = ""
            continue

        if find_col_idx >= len(df.columns) or ret_col_idx >= len(df.columns):
            logger.warning(
                f"定位器 '{key}' 列越界: find={find_col}({find_col_idx}), "
                f"ret={ret_col}({ret_col_idx}), 总列数={len(df.columns)}"
            )
            result[key] = ""
            continue

        found = False
        for row_idx in range(len(df)):
            cell_val = str(df.iloc[row_idx, find_col_idx])
            if find_val in cell_val:
                ret_val = df.iloc[row_idx, ret_col_idx]
                import math
                result[key] = (
                    str(ret_val) if not isinstance(ret_val, float) or not math.isnan(ret_val) else ""
                )
                found = True
                break

        if not found:
            result[key] = ""

    return result


# ---------------------------------------------------------------------------
# 核心工具类
# ---------------------------------------------------------------------------

class FineReportTools:
    """FineReport 工具类，对外暴露 get_report_sample / download_fine_by_filter"""

    def __init__(self):
        self.download_dir = config.get(
            "fine_report.download_path",
            "./downloads/fr_download",
        )
        self.download_timeout_ms = config.get("fine_report.download_timeout_ms", 60000)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def get_report_sample(self, report_url: str) -> str:
        """
        获取报表样例：访问报表 → 下载Excel → MarkItDown转Markdown → 返回控件信息+页面内容摘要。

        Args:
            report_url: FineReport 报表的完整 URL

        Returns:
            Markdown 格式的抽样信息字符串，出错时返回错误提示文本
        """
        logger.info(f"获取报表样例: {report_url}")

        context = _get_browser_context()
        page: Optional[Page] = None
        tmp_file = None

        try:
            page = context.new_page()
            page.goto(report_url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            # 登录兜底（session 可能已失效）
            _ensure_logged_in(page)
            page.wait_for_load_state("networkidle")
            
            page.goto(report_url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")

            # 获取控件信息
            widgets_info = page.evaluate("""
                () => {
                    const container = _g().getParameterContainer();
                    const items = container.options.items || [];
                    return JSON.stringify(items.map(it => ({
                        widgetName: it.widgetName || '',
                        type: it.type || '',
                        disabled: !!it.disabled,
                        invisible: !!it.invisible,
                        x: it.x || 0,
                        y: it.y || 0,
                    })));
                }
            """)

            # 下载 Excel
            dl_dir = Path(self.download_dir)
            dl_dir.mkdir(parents=True, exist_ok=True)

            with page.expect_download(timeout=self.download_timeout_ms) as dl_info:
                page.evaluate("_g().exportReportToExcel('simple')")

            dl: Download = dl_info.value
            fname = f"fr_sample_{uuid.uuid4().hex[:8]}.xlsx"
            tmp_file = dl_dir / fname
            dl.save_as(str(tmp_file))

            # MarkItDown 解析
            try:
                from markitdown import MarkItDown
                converter = MarkItDown()
                doc_result = converter.convert(str(tmp_file))
                page_md = doc_result.text_content or ""
            except Exception as e:
                logger.warning(f"MarkItDown 解析失败: {e}")
                page_md = "(MarkItDown 解析失败，内容不可读)"

            # 组装返回
            widgets_list = json.loads(widgets_info)
            md_lines = [
                "# FineReport 报表样例",
                "",
                f"**报表URL**: {report_url}",
                "",
                f"**控件数量**: {len(widgets_list)}",
                "",
                "| 控件名 | 类型 | 禁用 | 隐藏 |",
                "|--------|------|------|------|",
            ]
            for w in widgets_list:
                md_lines.append(
                    f"| {w['widgetName']} | {w['type']} | "
                    f"{'是' if w['disabled'] else '否'} | "
                    f"{'是' if w['invisible'] else '否'} |"
                )
            md_lines.append("")
            md_lines.append("## 页面数据摘要")
            md_lines.append("")
            md_lines.append(page_md[:2000])

            return "\n".join(md_lines)

        except Exception as e:
            logger.exception(f"获取报表样例失败: {e}")
            return f"# 错误\n获取报表样例失败: {e}"

        finally:
            if page:
                page.close()
            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    # ------------------------------------------------------------------

    def download_fine_by_filter(
        self,
        report_url: str,
        controls: List[Dict[str, Any]],
        locators: Optional[Dict[str, Any]] = None,
        target_date: str = "",
    ) -> str:
        """
        设控件值（含日期控件自动填充）→ 提交 → 下载Excel → 按 locators 提取数据。

        Args:
            report_url:   FineReport 报表完整 URL
            controls:     普通控件操作列表，格式: [{'name': '控件名', 'value': '值'}, ...]
            locators:     数据提取规则，见 extract_data_from_excel
            target_date:  可选，指定目标月份最后一天的日期（格式 yyyy-MM-dd），
                          若报表含日期控件会自动识别并填入

        Returns:
            JSON 字符串: {"success": true/false, "data": {...}, "error": ""}
        """
        logger.info(
            f"download_fine_by_filter 开始 | url={report_url} | "
            f"target_date={target_date} | controls={controls}"
        )

        context = _get_browser_context()
        page: Optional[Page] = None
        tmp_file = None

        try:
            page = context.new_page()
            page.goto(report_url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")
            time.sleep(3)

            _ensure_logged_in(page)
            page.wait_for_load_state("networkidle")

            page.goto(report_url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")

            # 自动填日期控件
            _auto_fill_date_control(page, target_date)

            # 设普通控件
            for ctrl in controls:
                name = ctrl.get("name", "")
                value = ctrl.get("value", "")
                if not name:
                    continue
                escaped_value = str(value).replace('"', '\\"')
                page.evaluate(
                    f'_g().getParameterContainer().getWidgetByName("{name}").setValue("{escaped_value}")'
                )
            page.evaluate("_g().parameterCommit()")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)
            logger.info("控件已设置并提交")

            # 下载 Excel
            dl_dir = Path(self.download_dir)
            dl_dir.mkdir(parents=True, exist_ok=True)

            with page.expect_download(timeout=self.download_timeout_ms) as dl_info:
                page.evaluate("_g().exportReportToExcel('simple')")

            dl: Download = dl_info.value
            fname = f"fr_{uuid.uuid4().hex[:8]}.xlsx"
            tmp_file = dl_dir / fname
            dl.save_as(str(tmp_file))

            # 确保大文件下载完成
            try:
                _wait_for_file_stable(str(tmp_file), stable_seconds=3, timeout=300)
            except Exception as e:
                logger.warning(f"文件稳定等待失败（将继续）: {e}")

            logger.info(f"Excel 下载完成: {tmp_file}")

            # 提取数据
            if locators:
                data = extract_data_from_excel(str(tmp_file), locators)
                return json.dumps({"success": True, "data": data}, ensure_ascii=False, indent=2)
            else:
                # fallback：无 locators 时返回整张 Excel 的内容
                import pandas as pd
                df = pd.read_excel(str(tmp_file), header=None)
                excel_json = {
                    "columns": [str(c) for c in df.columns.tolist()],
                    "rows": df.values.tolist(),
                    "shape": [df.shape[0], df.shape[1]],
                }
                return json.dumps({
                    "success": True,
                    "data": excel_json,
                    "message": "未提供 locators，已返回整表内容",
                    "download_path": str(tmp_file),
                }, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.exception(f"download_fine_by_filter 失败: {e}")
            return json.dumps({"success": False, "data": {}, "error": str(e)}, ensure_ascii=False)

        finally:
            if page:
                page.close()
            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 公开工具函数（MCP 工具底层实现，供 router 和 MCP 共同调用）
# ---------------------------------------------------------------------------

def _fr_get_report_sample(report_url: str) -> str:
    cache_key = f"fr_sample:{report_url}"
    cached = cache_manager.get(cache_key)
    if cached is not None:
        logger.info(f"[缓存命中] report_sample {report_url}")
        return cached
    result = FineReportTools().get_report_sample(report_url)
    cache_manager.set(cache_key, result, expire=3 * 86400)
    return result


def _fr_download_fine_by_filter(
    report_url: str,
    controls: List[Dict[str, Any]] = None,
    locators: Optional[Dict[str, Any]] = None,
    target_date: str = "",
) -> str:
    if controls is None:
        controls = []

    cache_key = f"fr_download:{report_url}:{json.dumps(controls, sort_keys=True)}:{target_date}"
    cached = cache_manager.get(cache_key)
    if cached is not None:
        logger.info(f"[缓存命中] download_fine_by_filter {report_url} controls={controls} target_date={target_date}")
        return cached

    result = FineReportTools().download_fine_by_filter(
        report_url=report_url,
        controls=controls,
        locators=locators,
        target_date=target_date,
    )
    cache_manager.set(cache_key, result, expire=86400)
    return result


# ---------------------------------------------------------------------------
# FastMCP 工具注册（与 hive_query.py、agent_browser.py 风格一致）
# ---------------------------------------------------------------------------

fr_mcp = FastMCP("IntelliBridge FineReport")


@fr_mcp.tool(name="get_report_sample")
@log_function_info
def fr_get_report_sample(report_url: str) -> str:
    """
    获取 FineReport 报表样例：返回报表上的控件清单和当前页面数据的 Markdown 摘要。

    Args:
        report_url: FineReport 报表的完整 URL（如 DecisionEngine 访问地址）

    Returns:
        Markdown 格式的抽样信息
    """
    return _fr_get_report_sample(report_url)


@fr_mcp.tool(name="download_fine_by_filter")
@log_function_info
def fr_download_fine_by_filter(
    report_url: str,
    controls: List[Dict[str, Any]] = None,
    target_date: str = "",
) -> str:
    """
    设控件值、从 FineReport 下载 Excel、并按提取规则返回结构化数据。

    Args:
        report_url:   FineReport 报表完整 URL
        controls:     控件操作列表，如 [{'name': '分行', 'value': '武汉'}, ...]
        target_date:  可选，指定年月（yyyy-MM-dd），自动识别日期控件并填入

    Returns:
        JSON 字符串，内含 success、data（结构化结果）或 download_path 字段
    """
    return _fr_download_fine_by_filter(
        report_url=report_url,
        controls=controls or [],
        locators=None,
        target_date=target_date,
    )


# ---------------------------------------------------------------------------
# FastAPI 路由（与 MCP 平行提供，便于调试和直接 curl 调用）
# ---------------------------------------------------------------------------

fr_router = APIRouter(prefix="/api/fine-report", tags=["FineReport"])


class SampleReq(BaseModel):
    report_url: str


class DownloadReq(BaseModel):
    report_url: str
    controls: List[Dict[str, Any]] = []
    locators: Optional[Dict[str, Any]] = None
    target_date: str = ""


@fr_router.post("/sample")
async def api_sample(req: SampleReq) -> dict:
    """REST 接口：获取报表样例"""
    return {"data": _fr_get_report_sample(req.report_url)}


@fr_router.post("/download")
async def api_download(req: DownloadReq) -> dict:
    """REST 接口：设控件值、下载 Excel、按提取规则返回数据"""
    return {"data": _fr_download_fine_by_filter(
        report_url=req.report_url,
        controls=req.controls,
        locators=req.locators,
        target_date=req.target_date,
    )}