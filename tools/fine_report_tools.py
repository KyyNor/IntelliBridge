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
from urllib.parse import quote, quote_plus
from pathlib import Path
from typing import Optional, Dict, List, Any

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from utils.config import config
from utils.logger import logger
from utils.decorators import log_function_info
from utils.cache import cache as cache_manager

from concurrent.futures import ThreadPoolExecutor

from fastmcp import FastMCP
from fastapi import APIRouter
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 模块级浏览器专用单线程执行器，彻底规避 asyncio worker 多线程环境中
# Playwright greenlet 跨线程崩溃问题
# ---------------------------------------------------------------------------

_fr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fr_browser")

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
    """判断当前页面或 URL 是否处于登录页（去掉 ?origin=xxx 参数后再判断，防止误判）"""
    url = getattr(page_or_url, "url", page_or_url)
    path = url.split("?")[0].rstrip("/")
    return path.endswith("/login")


def _login_once(page: Page) -> None:
    """检测到 /login 页面时，强制跳转 SSO 登录页、完成认证、切新环境（SSO 场景专用）"""
    username = config.get("fine_report.username")
    password = config.get("fine_report.password")
    login_url = config.get("fine_report.login_url")

    if not username or not password or not login_url:
        raise RuntimeError("FineReport login_url/username/password 配置缺失")

    logger.info("[FR 登录] 检测到 /login 页面，准备跳转到 SSO 登录页")
    logger.info(f"[FR 登录] SSO 登录地址: {login_url}")
    logger.info("[FR 登录] 正在填充用户名...")
    page.goto(login_url, wait_until="networkidle")
    page.fill("#inputUsername", username)
    logger.info("[FR 登录] 用户名已填充，正在填充密码...")
    page.fill("#inputPassword", password)
    logger.info("[FR 登录] 密码已填充，点击登录按钮...")
    page.click("#submitBtn")
    page.wait_for_load_state("networkidle")
    logger.info("[FR 登录] SSO 登录请求已发送，等待响应...")

    time.sleep(2)

    # 点"点我前往新环境"
    try:
        logger.info("[FR 登录] 尝试查找并点击「点我前往新环境」入口...")
        iframe_el = page.frame_locator('iframe')
        iframe_el.locator('text="点我前往新环境"').click()
        logger.info("[FR 登录] 「新环境」入口点击完成")
    except Exception:
        pass

    time.sleep(2)


def _ensure_logged_in(page: Page) -> None:
    """确保当前 Page 已登录；若已在登录页则触发 SSO 登录流程"""
    global _logged_in
    if _is_on_login_page(page.url):
        logger.info(f"[FR 登录] 当前页面仍为登录页（URL={page.url}），触发 SSO 登录流程")
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
    """利用 JavaScript 自动识别报表中最可能的时间控件，并将 target_date 填入。"""

    # 未登录状态下 _g 不存在，跳过以免报错，等登录后再处理
    if _is_on_login_page(page.url):
        logger.info("[FR] 当前仍在登录页，跳过日期控件填充，等待登录流程完成")
        return

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
        self.browserless_download_path = config.get(
            "fine_report.browserless_download_path",
            "./downloads/browserless_download_path",
        )
        self.download_timeout_ms = config.get("fine_report.download_timeout_ms", 60000)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def get_report_sample(self, report_url: str) -> str:
        """
        获取报表样例：访问报表 → 下载Excel → openpyxl提取文本 → 返回控件信息+页面内容摘要。

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
            logger.info(f"[FR sample] goto 完成，当前实际页面 URL: {page.url}")
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

            download_file_path = f'{self.browserless_download_path}/{dl_info.value.suggested_filename}'
            _wait_for_file_stable(download_file_path)

            fname = f"fr_sample_{uuid.uuid4().hex[:8]}.xlsx"
            tmp_file = dl_dir / fname

            logger.info(f"文件最终存储路径：{tmp_file}")
            shutil.copy(download_file_path, tmp_file)

            # openpyxl 解析
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(tmp_file), data_only=True)
                parts = []
                for ws in wb.worksheets:
                    rows = ws.iter_rows(values_only=True)
                    for row in rows:
                        cell_vals = [
                            str(c) for c in row if c is not None
                        ]
                        if cell_vals:
                            parts.append(" | ".join(cell_vals))
                page_md = "\n".join(parts)
            except Exception as e:
                logger.warning(f"openpyxl 解析失败: {e}")
                page_md = "(表格内容不可读)"

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
    # 共用底层：从打开报表到下载到本地文件
    # ------------------------------------------------------------------

    def _download_to_local_file(
        self,
        report_url: str,
        controls: List[Dict[str, Any]],
        target_date: str,
    ) -> tuple[Path, Page]:
        """
        执行打开报表→设控件→触发下载→复制文件到本地临时目录的全流程。
        返回 (本地文件路径, page对象)。
        调用方负责在 finally 中关闭 page 并删除文件。
        """
        context = _get_browser_context()
        page = context.new_page()

        page.goto(report_url, wait_until="networkidle")
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        _ensure_logged_in(page)
        page.wait_for_load_state("networkidle")

        page.goto(report_url, wait_until="networkidle")
        logger.info(f"[FR] goto 完成，当前实际页面 URL: {page.url}")
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

        download_file_path = f'{self.browserless_download_path}/{dl_info.value.suggested_filename}'
        _wait_for_file_stable(download_file_path)

        fname = f"fr_sample_{uuid.uuid4().hex[:8]}.xlsx"
        tmp_file = dl_dir / fname

        logger.info(f"文件最终存储路径：{tmp_file}")
        shutil.copy(download_file_path, tmp_file)
        return tmp_file, page

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def download_fine_by_filter(
        self,
        report_url: str,
        controls: List[Dict[str, Any]],
        locators: Optional[Dict[str, Any]] = None,
        target_date: str = "",
    ) -> Dict[str, Any]:
        """
        设控件值（含日期控件自动填充）→ 提交 → 下载Excel → 按 locators 提取数据。

        Args:
            report_url:   FineReport 报表完整 URL
            controls:     普通控件操作列表，格式: [{'name': '控件名', 'value': '值'}, ...]
            locators:     数据提取规则，见 extract_data_from_excel
            target_date:  可选，指定目标月份最后一天的日期（格式 yyyy-MM-dd）

        Returns:
            {"success": true/false, "data": {...}, "error": ""}
        """
        logger.info(
            f"download_fine_by_filter 开始 | url={report_url} | "
            f"target_date={target_date} | controls={controls}"
        )

        page = None
        tmp_file = None

        try:
            tmp_file, page = self._download_to_local_file(report_url, controls, target_date)

            if locators:
                data = extract_data_from_excel(str(tmp_file), locators)
                return {"success": True, "data": data}
            else:
                # fallback：无 locators 时返回整张 Excel 的内容
                import pandas as pd
                df = pd.read_excel(str(tmp_file), header=None)
                excel_json = {
                    "columns": [str(c) for c in df.columns.tolist()],
                    "rows": df.values.tolist(),
                    "shape": [df.shape[0], df.shape[1]],
                }
                return {
                    "success": True,
                    "data": excel_json,
                    "message": "未提供 locators，已返回整表内容",
                    "download_path": str(tmp_file),
                }

        except Exception as e:
            logger.exception(f"download_fine_by_filter 失败: {e}")
            return {"success": False, "data": {}, "error": str(e)}

        finally:
            if page:
                page.close()
            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

    def download_fine_paginated(
        self,
        report_url: str,
        controls: List[Dict[str, Any]],
        target_date: str = "",
        *,
        page_num: int = 1,
        rows_per_page: int = 100,
    ) -> Dict[str, Any]:
        """
        设控件值 → 下载Excel → 自动推断表头行 → 返回 CSV 式分页数据。

        表头自动推断策略：在前 max_header_scan 行中找到非空比例最高的行；
        该行每格若为 NaN 则沿用上一格的值（模拟合并单元格的向下延伸）。

        Args:
            report_url:     FineReport 报表完整 URL
            controls:       普通控件操作列表
            target_date:    可选，目标日期（格式 yyyy-MM-dd）
            page_num:       页码（从1起），默认1
            rows_per_page:  每页行数，默认100

        Returns:
            {
                "success": true/false,
                "headers": ["排名","网点"...],      # 推断不到时为空[]
                "pre_headers": "...",              # 表头以上的行，逗号拼接，空值不参与
                "rows": [["1","汉正街支行",...], ...],
                "page_num": 1,
                "total_num": 83,
                "total_pages": 1,
            }
            出错时: {"success": false, "error": "..."}
        """
        logger.info(
            f"download_fine_paginated 开始 | url={report_url} | "
            f"target_date={target_date} | page_num={page_num}"
        )

        import pandas as pd

        page_obj = None
        tmp_file = None

        try:
            tmp_file, page_obj = self._download_to_local_file(report_url, controls, target_date)

            df = pd.read_excel(str(tmp_file), header=None)
            total_rows = len(df)
            num_cols = len(df.columns)

            # ---- 推断表头：在前 HEADER_SCAN_MAX_ROWS 行中选非空比例最高的，
            #              且该比例须 >= HEADER_MIN_RATIO，否则视为未推断到 ----
            HEADER_SCAN_MAX_ROWS = 5
            HEADER_MIN_RATIO = 0.4
            header_row_idx = 0
            max_valid_ratio = -1.0

            scan_end = min(HEADER_SCAN_MAX_ROWS, total_rows)
            for i in range(scan_end):
                row_vals = df.iloc[i]
                non_null = sum(1 for v in row_vals if pd.notna(v) and str(v).strip() != "")
                ratio = non_null / num_cols if num_cols else 0
                if ratio > max_valid_ratio:
                    max_valid_ratio = ratio
                    header_row_idx = i

            header_found_flag = max_valid_ratio >= HEADER_MIN_RATIO

            # ---- 构建表头：推断成功时生成列名列表；否则为空 ----
            if header_found_flag:
                header_raw = df.iloc[header_row_idx].tolist()
                headers = []
                last_valid = ""
                for v in header_raw:
                    if pd.notna(v) and str(v).strip():
                        last_valid = str(v).strip()
                    headers.append(last_valid if last_valid else "")
            else:
                headers = []

            # ---- 构建 pre_headers：表头行之前的所有行，非空值用逗号拼接 ----
            pre_row_strings: List[str] = []
            for i in range(header_row_idx if header_found_flag else total_rows):
                cells = df.iloc[i]
                joined_parts = [
                    str(c).strip()
                    for c in cells
                    if pd.notna(c) and str(c).strip() != ""
                ]
                pre_row_strings.append(",".join(joined_parts))

            pre_headers = ",".join(pre_row_strings)

            # ---- 确定数据起始行 ----
            data_start = header_row_idx + 1 if header_found_flag else 0
            data_df = df.drop(range(data_start))

            # 计算分页
            total_items = len(data_df)
            total_pages = max(1, (total_items + rows_per_page - 1) // rows_per_page)
            page_num = max(1, min(page_num, total_pages))  # 边界约束
            start = (page_num - 1) * rows_per_page
            end = start + rows_per_page
            page_rows = data_df.iloc[start:end].values.tolist()

            return {
                "success": True,
                "headers": headers,
                "pre_headers": pre_headers,
                "rows": page_rows,
                "page_num": page_num,
                "total_num": total_items,
                "total_pages": total_pages,
            }

        except Exception as e:
            logger.exception(f"download_fine_paginated 失败: {e}")
            return {"success": False, "error": str(e)}

        finally:
            if page_obj:
                page_obj.close()
            if tmp_file and tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 公开工具函数（MCP 工具底层实现，供 router 和 MCP 共同调用）
# ---------------------------------------------------------------------------

def _executor_submit_and_wait(fn, *args, **kwargs):
    """在线程池中执行 fn 并阻塞等待结果"""
    return _fr_executor.submit(fn, *args, **kwargs).result()


def _slice_page_from_base_result(
    base_result: Dict[str, Any],
    page_num: int,
    rows_per_page: int,
) -> Dict[str, Any]:
    """
    从已缓存的全量结果中，按 page_num + rows_per_page 切出一页返回。
    缓存命中时走此路径，保证不同 page_num 共享同一次 Excel 下载的结果。
    """
    total_items = base_result.get("total_num", 0)
    total_pages = max(1, (total_items + rows_per_page - 1) // rows_per_page)
    page_num = max(1, min(page_num, total_pages))
    start = (page_num - 1) * rows_per_page
    end = start + rows_per_page
    page_rows = base_result.get("rows", [])[start:end]

    return {
        "success": True,
        "headers": base_result.get("headers", []),
        "pre_headers": base_result.get("pre_headers", ""),
        "rows": page_rows,
        "page_num": page_num,
        "total_num": total_items,
        "total_pages": total_pages,
    }


def _build_report_url(report_path: str) -> str:
    """将 CPT 路径拼接为完整报表 URL，前缀从配置读取"""
    prefix = config.get("fine_report.fine_report_prefix", "")
    return prefix.rstrip("/") + "/" + quote(quote_plus(report_path))


def _fr_get_report_sample(report_path: str) -> str:
    report_url = _build_report_url(report_path)
    logger.info(f"[FR] 拼接报表 URL: {report_url}")
    cache_key = f"fr_sample:{report_path}"
    cached = cache_manager.get(cache_key)
    if cached is not None:
        logger.info(f"[缓存命中] report_sample {report_path}")
        return cached
    result = _fr_executor.submit(FineReportTools().get_report_sample, report_url).result()
    # 仅成功时缓存；失败（如登录失败、页面报错）不写入缓存，避免错误结果被长期复用
    if not result.startswith("# 错误"):
        cache_manager.set(cache_key, result, expire=3 * 86400)
    else:
        logger.warning(f"[缓存跳过] report_sample {report_path} 登录/下载失败，不予缓存")
    return result


def _fr_download_fine_paginated(
    report_path: str,
    controls: List[Dict[str, Any]] = None,
    target_date: str = "",
    *,
    page_num: int = 1,
    rows_per_page: int = 100,
) -> Dict[str, Any]:
    if controls is None:
        controls = []

    # ---- 缓存键不含 page_num：同一报表同一参数共享一份全量数据缓存 ----
    BASE_CACHE_KEY = f"fr_paginated_base:{report_path}:{json.dumps(controls, sort_keys=True)}:{target_date}"

    cached_base = cache_manager.get(BASE_CACHE_KEY)
    if cached_base is not None:
        logger.info(f"[缓存命中·全量] download_fine_paginated {report_path}，正在从中切第 {page_num} 页")
        return _slice_page_from_base_result(cached_base, page_num, rows_per_page)

    # 缓存未命中，走完整下载流程（全量下载，全速缓存）
    report_url = _build_report_url(report_path)
    logger.info(f"[FR] 拼接报表 URL: {report_url}")

    base_result = _executor_submit_and_wait(
        FineReportTools().download_fine_paginated,
        report_url, controls, target_date,
        page_num=1, rows_per_page=9999999,
    )

    if not base_result.get("success"):
        logger.warning(f"[缓存跳过] download_fine_paginated {report_path} 下载失败，不予缓存")
        return base_result

    # 仅成功时缓存全量结果
    cache_manager.set(BASE_CACHE_KEY, base_result, expire=86400)
    logger.info(f"[缓存写入] download_fine_paginated 全量结果已缓存，总行数={base_result.get('total_num')}，缓存 key: {BASE_CACHE_KEY}")

    return _slice_page_from_base_result(base_result, page_num, rows_per_page)


def _fr_download_fine_by_filter(
    report_path: str,
    controls: List[Dict[str, Any]] = None,
    locators: Optional[Dict[str, Any]] = None,
    target_date: str = "",
) -> Dict[str, Any]:
    if controls is None:
        controls = []
    report_url = _build_report_url(report_path)
    logger.info(f"[FR] 拼接报表 URL: {report_url}")

    cache_key = f"fr_download:{report_path}:{json.dumps(controls, sort_keys=True)}:{target_date}"
    cached = cache_manager.get(cache_key)
    if cached is not None:
        logger.info(f"[缓存命中] download_fine_by_filter {report_url} controls={controls} target_date={target_date}")
        return cached

    result = _fr_executor.submit(
        FineReportTools().download_fine_by_filter,
        report_url, controls, locators, target_date,
    ).result()
    # 仅成功时缓存；失败时不写入缓存
    try:
        if result.get("success"):
            cache_manager.set(cache_key, result, expire=86400)
        else:
            logger.warning(f"[缓存跳过] download_fine_by_filter {report_path} 请求失败，不予缓存")
    except Exception:
        logger.warning(f"[缓存跳过] download_fine_by_filter {report_path} 无法解析返回值，不予缓存")
    return result


# ---------------------------------------------------------------------------
# FastMCP 工具注册（与 hive_query.py、agent_browser.py 风格一致）
# ---------------------------------------------------------------------------

fr_mcp = FastMCP("IntelliBridge FineReport")


@fr_mcp.tool(name="get_report_sample")
@log_function_info
def fr_get_report_sample(report_path: str) -> str:
    """
    获取 FineReport 报表样例：返回报表上的控件清单和当前页面数据的 Markdown 摘要。

    Args:
        report_path: FineReport 报表的 CPT 路径（如 /a/b/cpt）

    Returns:
        Markdown 格式的抽样信息
    """
    return _fr_get_report_sample(report_path)

@fr_mcp.tool(name="download_fine_paginated")
@log_function_info
def fr_download_fine_paginated(
    report_path: str,
    controls: List[Dict[str, Any]] = None,
    target_date: str = "",
    page_num: int = 1,
    rows_per_page: int = 100,
) -> Dict[str, Any]:
    """
    设控件值、从 FineReport 下载 Excel，以 CSV 格式返回分页数据。

    表头自动推断：在前 5 行中选用非空比例最高的行，空格用前一个非空值填充。

    Args:
        report_path:   FineReport 报表的 CPT 路径（如 /abc/test.cpt）
        controls:       控件操作列表，如 [{'name': '分行', 'value': '武汉'}, ...]
        target_date:    可选，指定年月（yyyy-MM-dd），自动识别日期控件并填入
        page_num:       页码，从1起，默认1
        rows_per_page:  每页行数，默认100

    Returns:
        {
            "success": true/false,
            "headers": ["排名","网点"...],     # 推断不到时为[]
            "pre_headers": "...",             # 表头以上的行，逗号拼接，空值不参与
            "rows": [["1","汉正街支行",...], ...],
            "page_num": 1,
            "total_num": 83,
            "total_pages": 1
        }
    """
    return _fr_download_fine_paginated(
        report_path=report_path,
        controls=controls or [],
        target_date=target_date,
        page_num=page_num,
        rows_per_page=rows_per_page,
    )


# ---------------------------------------------------------------------------
# FastAPI 路由（与 MCP 平行提供，便于调试和直接 curl 调用）
# ---------------------------------------------------------------------------

fr_router = APIRouter(prefix="/api/fine-report", tags=["FineReport"])


class SampleReq(BaseModel):
    report_path: str = Field(description="FineReport 报表的 CPT 路径（如 /abc/test.cpt）")


class DownloadReq(BaseModel):
    report_path: str = Field(description="FineReport 报表的 CPT 路径（如 /abc/test.cpt）")
    controls: List[Dict[str, Any]] = []
    locators: Optional[Dict[str, Any]] = None
    target_date: str = ""


class PaginatedReq(BaseModel):
    report_path: str = Field(description="FineReport 报表的 CPT 路径（如 /abc/test.cpt）")
    controls: List[Dict[str, Any]] = []
    target_date: str = ""
    page_num: int = Field(default=1, ge=1, description="页码，从1起")
    rows_per_page: int = Field(default=100, ge=1, le=1000, description="每页行数")


@fr_router.post("/sample")
async def api_sample(req: SampleReq) -> dict:
    """REST 接口：获取报表样例"""
    return {"data": _fr_get_report_sample(req.report_path)}


@fr_router.post("/download")
async def api_download(req: DownloadReq) -> dict:
    """REST 接口：设控件值、下载 Excel、按提取规则返回数据"""
    return {"data": _fr_download_fine_by_filter(
        report_path=req.report_path,
        controls=req.controls,
        locators=req.locators,
        target_date=req.target_date,
    )}


@fr_router.post("/paginated")
async def api_paginated(req: PaginatedReq) -> dict:
    """REST 接口：CSV 式分页下载 Excel 数据"""
    return {"data": _fr_download_fine_paginated(
        report_path=req.report_path,
        controls=req.controls,
        target_date=req.target_date,
        page_num=req.page_num,
        rows_per_page=req.rows_per_page,
    )}