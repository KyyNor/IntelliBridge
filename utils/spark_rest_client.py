"""Spark Web UI REST API 客户端。

封装对 Spark ThriftServer 的 Spark Web UI（默认端口 4040）的 REST API 调用，
用于查询性能分析。所有请求都带短超时且异常会被吞掉并记日志——这是一个后台
采集器，不能因为单次网络抖动导致整个轮询循环中断。

参考：https://spark.apache.org/docs/latest/monitoring.html#rest-api
"""

import re
from typing import Dict, List, Optional

import requests

from utils.config import config
from utils.logger import logger

# Spark REST API 请求超时（秒）。后台采集器宁可丢一轮也不能卡死。
_DEFAULT_TIMEOUT = 5

# 用于从 REST API 的 metric value 文本里抽取数值的正则。
# 例如 "total (min, med, max (stageId: taskId))\n281.0 B (140.0 B, 141.0 B, 141.0 B (st"
# 我们只关心开头的 total 值。
_TOTAL_PATTERN = re.compile(r"^\s*([0-9.,]+)\s*([KMGTPEZY]?i?B|B|ms|s)?", re.IGNORECASE)

# 字节单位到字节的换算因子。
_BYTE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
    "PB": 1024 ** 5,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
    "PIB": 1024 ** 5,
}


def parse_metric_bytes(value: str) -> Optional[float]:
    """从 Spark REST API 的 metric value 文本里解析出字节数。

    Spark 的 SQL metric value 形如：
      - "4"                      （纯计数）
      - "281.0 B"                （单值带单位）
      - "total (min, med, max (stageId: taskId))\\n281.0 B (140.0 B, 141.0 B, ..."
        （分布值；total 是换行后第一行的第一个数值）

    返回字节数（float），无法解析时返回 None。
    """
    if not value:
        return None
    text = str(value).strip()
    lines = text.split("\n")
    # 找到第一行以数字开头的行（跳过 "total (min, med, max ...)" 这种表头行）
    target_line = None
    for line in lines:
        stripped = line.strip()
        if stripped and stripped[0].isdigit():
            target_line = stripped
            break
    if target_line is None:
        return None
    match = _TOTAL_PATTERN.match(target_line)
    if not match:
        return None
    number_text, unit = match.group(1), (match.group(2) or "")
    try:
        number = float(number_text.replace(",", ""))
    except ValueError:
        return None
    unit_key = unit.upper()
    if not unit:
        # 纯数字，没有单位 —— 可能是计数（records）而非字节
        return number
    factor = _BYTE_UNITS.get(unit_key)
    if factor is None:
        # 未知单位（如 ms/s），按裸数值返回
        return number
    return number * factor


class SparkRestClient:
    """Spark Web UI REST API 的最小客户端。

    针对 ThriftServer 场景：只关心单个活跃 Spark 应用（ThriftServer 启动后
    只有一个 app），按 SQL execution id 增量拉取查询记录。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        # 不做默认值兜底 —— base_url 由上层（SparkSqlAnalyzer）决定是否启用。
        # 传入 None 或空串时 base_url 为空串，后续所有请求会失败并记 debug 日志。
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 低层请求
    # ------------------------------------------------------------------
    def _get(self, path: str) -> Optional[object]:
        """发起 GET 请求，返回解析后的 JSON。失败返回 None 并记日志。"""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.debug(f"Spark REST 请求失败: {url} - {exc}")
            return None
        if resp.status_code != 200:
            logger.debug(f"Spark REST 非 200: {url} -> {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError as exc:
            logger.debug(f"Spark REST JSON 解析失败: {url} - {exc}")
            return None

    # ------------------------------------------------------------------
    # 应用层
    # ------------------------------------------------------------------
    def get_app_id(self) -> Optional[str]:
        """返回当前活跃 Spark 应用的 id。

        ThriftServer 只有一个 app，取第一个。返回 None 表示 Web UI 未启用 /
        未启动 / 当前没有应用。
        """
        data = self._get("/api/v1/applications")
        if not data or not isinstance(data, list):
            return None
        for app in data:
            app_id = app.get("id")
            if app_id:
                return app_id
        return None

    def list_sql_executions(self, app_id: str, since_id: int = 0) -> List[Dict]:
        """返回 id > since_id 的 SQL 执行摘要列表（升序）。

        摘要字段：id, description, status, submissionTime, duration,
        runningJobIds, successJobIds, failedJobIds, planDescription, nodes。
        """
        data = self._get(f"/api/v1/applications/{app_id}/sql")
        if not isinstance(data, list):
            return []
        return [s for s in data if isinstance(s, dict) and s.get("id", -1) > since_id]

    def get_sql_detail(self, app_id: str, sql_id: int) -> Optional[Dict]:
        """返回单条 SQL 执行的完整详情（含 plan nodes 的 metrics）。

        list_sql_executions 返回的摘要其实已经包含 nodes/metrics，本方法
        仅在需要单独下钻某条 SQL 时使用。
        """
        data = self._get(f"/api/v1/applications/{app_id}/sql/{sql_id}")
        if not isinstance(data, dict):
            return None
        return data

    def get_job(self, app_id: str, job_id: int) -> Optional[Dict]:
        """返回单个 job 的详情（含 stageIds）。用于把 SQL → job → stage 串起来。"""
        data = self._get(f"/api/v1/applications/{app_id}/jobs/{job_id}")
        if not isinstance(data, dict):
            return None
        return data

    def get_stage_tasks(self, app_id: str, stage_id: int, attempt_id: int = 0) -> List[Dict]:
        """返回某个 stage 的一次 attempt 下所有 task 的明细。

        每个 task 含 duration、host、taskMetrics.shuffleReadMetrics /
        shuffleWriteMetrics 等。用于数据倾斜下钻分析。
        """
        data = self._get(f"/api/v1/applications/{app_id}/stages/{stage_id}/{attempt_id}")
        if not isinstance(data, dict):
            return []
        tasks_map = data.get("tasks") or {}
        if not isinstance(tasks_map, dict):
            return []
        # tasks_map 的 value 才是 task 详情
        return [t for t in tasks_map.values() if isinstance(t, dict)]
