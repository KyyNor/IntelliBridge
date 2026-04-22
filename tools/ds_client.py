"""DolphinScheduler 3.1.3 Python Client"""

import json
import re
import time

import requests

from utils.logger import logger
from utils.decorators import log_function_info
from utils.config import config
from fastmcp import FastMCP

# ── 从配置文件加载 ────────────────────────────────────────────────

_ds_cfg: dict = config.get("dolphin_scheduler", {}) or {}

BASE_URL: str = _ds_cfg.get("base_url", "")
TOKEN: str = _ds_cfg.get("token", "")

# 项目白名单，为空则不限制。只有此处列出的项目及其工作流/实例可被操作。
ALLOWED_PROJECTS: list[str] = _ds_cfg.get("allowed_projects", []) or []

if not BASE_URL or not TOKEN:
    logger.warning(
        "DolphinScheduler 未完成配置，请在 config/config.yaml 中填写 "
        "dolphin_scheduler.base_url 与 dolphin_scheduler.token"
    )

# ── MCP 实例 ────────────────────────────────────────────────────

ds_mcp = FastMCP("IntelliBridge DolphinScheduler")

# API 路径
PATH_PROJECTS = "/projects/list"
PATH_WORKFLOWS = "/projects/{project_code}/process-definition"
PATH_WORKFLOW_RELEASE = "/projects/{project_code}/process-definition/{workflow_code}/release"
PATH_SCHEDULES = "/projects/{project_code}/schedules/list"
PATH_SCHEDULE_ONLINE = "/projects/{project_code}/schedules/{schedule_id}/online"
PATH_SCHEDULE_OFFLINE = "/projects/{project_code}/schedules/{schedule_id}/offline"
PATH_EXECUTOR_START = "/projects/{project_code}/executors/start-process-instance"
PATH_EXECUTOR_EXECUTE = "/projects/{project_code}/executors/execute"
PATH_INSTANCES = "/projects/{project_code}/process-instances"
PATH_INSTANCE_TASKS = "/projects/{project_code}/process-instances/{instance_id}/tasks"
PATH_LOG_DETAIL = "/log/detail"


# ── 主类 ────────────────────────────────────────────────────────

class DSClient:
    def __init__(self, base_url: str = BASE_URL, token: str = TOKEN):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session = requests.Session()
        self._session.headers.update({"token": token})
        self._project_cache: dict[str, int] = {}  # name -> code
        self._log_buffer: dict[str, list[str]] = {}   # (project, instance_id, task_name) -> [lines]
        self._log_meta: dict[str, dict] = {}           # same key -> {total_lines, loaded_at}

    # ── internal helpers ──────────────────────────────────────────

    def _request(self, method: str, path: str, data: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = dict(self._session.headers)
        logger.info(f"[DS 请求] method={method} | url={url} | headers={headers} | data={data} | params={params}")
        try:
            resp = self._session.request(method, url, data=data, params=params,
                                         timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(
                f"HTTP {e.response.status_code}: {e.response.text}") from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"网络请求失败: {e}") from e

    def clear_project_cache(self):
        """清除项目缓存，强制下次重新加载"""
        self._project_cache.clear()

    def _ensure_project_cache(self) -> list[dict]:
        """加载项目缓存，返回项目列表。已缓存则直接返回缓存数据。"""
        if not self._project_cache:
            PAGE_SIZE = 50
            projects = []
            page_no = 1
            while True:
                resp = self._request("GET", PATH_PROJECTS,
                                     params={"pageSize": PAGE_SIZE, "pageNo": page_no})
                data = resp.get("data", {})
                items = data.get("totalList", [])
                projects.extend(items)
                total_page = data.get("totalPage", 1)
                if page_no >= total_page:
                    break
                page_no += 1
            if ALLOWED_PROJECTS:
                projects = [p for p in projects if p["name"] in ALLOWED_PROJECTS]
            for p in projects:
                self._project_cache[p["name"]] = p["code"]
        return [{"name": n, "code": c} for n, c in self._project_cache.items()]

    def _get_project_code(self, name: str) -> int:
        self._ensure_project_cache()
        if name in self._project_cache:
            return self._project_cache[name]
        available = ", ".join(sorted(self._project_cache.keys()))
        raise ValueError(f"项目不存在: {name}。可用项目: {available}")

    def _get_workflow_code(self, project_code: int, name: str) -> int:
        resp = self._request("GET", PATH_WORKFLOWS.format(project_code=project_code),
                             params={"searchVal": name, "pageSize": 10, "pageNo": 1})
        total_list = resp.get("data", {}).get("totalList", [])
        for wf in total_list:
            if wf["name"] == name:
                return wf["code"]
        similar = [wf["name"] for wf in total_list if wf["name"] != name]
        hint = f"，类似工作流: {', '.join(similar)}" if similar else ""
        raise ValueError(f"工作流不存在: {name}{hint}")

    def _get_schedule_id(self, project_code: int, workflow_code: int) -> int:
        resp = self._request("POST", PATH_SCHEDULES.format(project_code=project_code),
                             data={"projectCode": str(project_code),
                                   "pageSize": 100, "pageNo": 1})
        data = resp.get("data", [])
        schedules = data if isinstance(data, list) else data.get("totalList", [])
        for s in schedules:
            if s.get("processDefinitionCode") == workflow_code:
                return s["id"]
        raise ValueError("工作流尚未创建定时调度，请先在界面上创建调度")

    # ── public API ────────────────────────────────────────────────

    def get_projects(self) -> list[dict]:
        """获取项目清单，返回 [{name, code, description, ...}]"""
        return self._ensure_project_cache()

    def get_workflows(self, project_name: str, search: str = "",
                      page: int = 1) -> dict:
        """获取工作流清单。返回 {totalList, totalPage, totalCount}，每页固定15条。"""
        code = self._get_project_code(project_name)
        params: dict = {"pageSize": 15, "pageNo": page}
        if search:
            params["searchVal"] = search
        KEYS = ["name", "version", "releaseState", "scheduleReleaseState",
                "createTime", "updateTime", "modifyBy"]
        resp = self._request("GET", PATH_WORKFLOWS.format(project_code=code),
                             params=params)
        data = resp.get("data", {})
        workflows = [{k: wf.get(k) for k in KEYS} for wf in data.get("totalList", [])]
        return {
            "totalList": workflows,
            "totalPage": data.get("totalPage", 0),
            "totalCount": data.get("total", 0),
        }

    def get_instances(self, project_name: str, search: str = "",
                      state: str = "") -> list[dict]:
        """获取工作流实例清单。state 可选值:
        SUBMITTED_SUCCESS, RUNNING_EXECUTION, READY_PAUSE, READY_STOP,
        PAUSE, SUCCESS, FAILURE, KILL, WAITING_THREAD, WAITING_DEPEND
        """
        code = self._get_project_code(project_name)
        page = 1
        results = []
        while page <= 100:
            params: dict = {"pageSize": 100, "pageNo": page}
            if search:
                params["searchVal"] = search
            if state:
                params["state"] = state
            resp = self._request("GET", PATH_INSTANCES.format(project_code=code),
                                 params=params)
            data = resp.get("data", {})
            results.extend(data.get("totalList", []))
            if page >= data.get("totalPage", 1):
                break
            page += 1
        if state:
            results = [r for r in results if r.get("state") == state]
        return results

    def list_instance_tasks(self, project_name: str, instance_id: int) -> list[dict]:
        """获取实例下的任务列表，快速视图，仅返回关键字段及 focus_level。"""
        pc = self._get_project_code(project_name)
        resp = self._request("GET",
                             PATH_INSTANCE_TASKS.format(project_code=pc, instance_id=instance_id))
        task_list = resp.get("data", {}).get("taskList", [])
        if not task_list:
            return []

        FOCUS_FAIL = {"FAILURE", "KILL"}
        FOCUS_OK   = {"SUCCESS"}

        def focus_of(state: str) -> str:
            s = str(state).upper().replace("-", "_")
            if s in FOCUS_FAIL:
                return "FAILURE"
            if s in FOCUS_OK:
                return "SUCCESS"
            return "OTHER"

        return [
            {
                "task_code": t.get("id"),
                "task_name": t.get("name", "unknown"),
                "task_type": t.get("type", ""),
                "state":     t.get("state", "UNKNOWN"),
                "start_time": t.get("startTime"),
                "end_time":  t.get("endTime"),
                "host":      t.get("host", ""),
                "focus_level": focus_of(t.get("state", "")),
            }
            for t in task_list
        ]

    # ── workflow operations ───────────────────────────────────────

    def workflow_online(self, project_name: str, workflow_name: str) -> dict:
        """上线工作流"""
        pc = self._get_project_code(project_name)
        wc = self._get_workflow_code(pc, workflow_name)
        return self._request("POST",
                             PATH_WORKFLOW_RELEASE.format(project_code=pc, workflow_code=wc),
                             data={"releaseState": "ONLINE"})

    def workflow_offline(self, project_name: str, workflow_name: str) -> dict:
        """下线工作流"""
        pc = self._get_project_code(project_name)
        wc = self._get_workflow_code(pc, workflow_name)
        return self._request("POST",
                             PATH_WORKFLOW_RELEASE.format(project_code=pc, workflow_code=wc),
                             data={"releaseState": "OFFLINE"})

    def schedule_online(self, project_name: str, workflow_name: str) -> dict:
        """定时上线"""
        pc = self._get_project_code(project_name)
        wc = self._get_workflow_code(pc, workflow_name)
        sid = self._get_schedule_id(pc, wc)
        return self._request("POST", PATH_SCHEDULE_ONLINE.format(project_code=pc, schedule_id=sid),
                             data={"scheduleId": str(sid)})

    def schedule_offline(self, project_name: str, workflow_name: str) -> dict:
        """定时下线"""
        pc = self._get_project_code(project_name)
        wc = self._get_workflow_code(pc, workflow_name)
        sid = self._get_schedule_id(pc, wc)
        return self._request("POST", PATH_SCHEDULE_OFFLINE.format(project_code=pc, schedule_id=sid))

    def complement_data(self, project_name: str, workflow_name: str,
                        start_date: str, end_date: str,
                        parallel: bool = False) -> dict:
        """补数。日期格式: YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"""
        pc = self._get_project_code(project_name)
        wc = self._get_workflow_code(pc, workflow_name)
        start = f"{start_date} 00:00:00" if len(start_date) == 10 else start_date
        end = f"{end_date} 00:00:00" if len(end_date) == 10 else end_date
        schedule_time = json.dumps({
            "complementStartDate": start,
            "complementEndDate": end,
        })
        return self._request(
            "POST",
            PATH_EXECUTOR_START.format(project_code=pc),
            data={
                "processDefinitionCode": str(wc),
                "execType": "COMPLEMENT_DATA",
                "failureStrategy": "END",
                "warningType": "NONE",
                "warningGroupId": "0",
                "runMode": "RUN_MODE_PARALLEL" if parallel else "RUN_MODE_SERIAL",
                "processInstancePriority": "MEDIUM",
                "workerGroup": "default",
                "scheduleTime": schedule_time,
            })

    # ── instance operations ───────────────────────────────────────

    _EXECUTE_TYPES = {
        "REPEAT_RUNNING": "重跑",
        "STOP": "停止",
        "START_FAILURE_TASK_PROCESS": "从失败继续",
    }

    def execute_instance(self, project_name: str, instance_id: int,
                         execute_type: str = "") -> dict:
        """操作实例。execute_type 可选值见 _EXECUTE_TYPES，为空或非法时返回支持的类型列表。"""
        if execute_type not in self._EXECUTE_TYPES:
            return {"error": f"不支持的 execute_type: {execute_type!r}",
                    "supported_types": self._EXECUTE_TYPES}
        pc = self._get_project_code(project_name)
        return self._request("POST", PATH_EXECUTOR_EXECUTE.format(project_code=pc),
                             data={"processInstanceId": str(instance_id),
                                   "executeType": execute_type})

    def load_task_full_log(self, project_name: str, instance_id: int,
                        task_name: str) -> dict:
        """预加载单个任务的全量日志（每页1000行，循环直到读完），存入内存缓冲区。

        Returns:
            {"task_name": ..., "total_lines": N, "loaded_at": timestamp, "task_id": ...}
        Raises:
            ValueError: 未找到指定的 task
        """
        pc = self._get_project_code(project_name)

        # 找到 task_id
        resp = self._request("GET",
                             PATH_INSTANCE_TASKS.format(project_code=pc, instance_id=instance_id))
        task_list = resp.get("data", {}).get("taskList", [])
        matched = None
        for t in task_list:
            if t.get("name") == task_name:
                matched = t
                break
        if not matched:
            raise ValueError(f"实例 {instance_id} 中未找到任务: {task_name}")

        task_id = matched.get("id")
        key = (project_name, instance_id, task_name)
        if key in self._log_buffer and self._log_meta.get(key, {}).get("task_id") == task_id:
            logger.info(f"[DS 日志] {task_name} 已在 buffer 中，跳过加载")
            return self._log_meta[key]

        # 分页拉取
        CHUNK = 1000
        offset = 0
        buffer: list[str] = []
        while True:
            resp = self._request("GET", PATH_LOG_DETAIL,
                                 params={"taskInstanceId": task_id,
                                         "skipLineNum": offset, "limit": CHUNK})
            msg = resp.get("data", {})
            msg = msg.get("message", "") if isinstance(msg, dict) else str(msg)
            lines = msg.splitlines()
            if not lines:
                break
            buffer.extend(lines)
            if len(lines) < CHUNK:
                break
            offset += CHUNK

        now = time.time()
        self._log_buffer[key] = buffer
        self._log_meta[key] = {
            "task_name": task_name,
            "task_id": task_id,
            "total_lines": len(buffer),
            "loaded_at": now,
        }
        logger.info(f"[DS 日志] {task_name} 加载完毕，总行数={len(buffer)}")
        return self._log_meta[key]

    def get_task_log(self, project_name: str, instance_id: int,
                     task_name: str, *,
                     mode: str = "simple",
                     offset: int = 0,
                     limit: int = 200) -> dict:
        """基于缓冲区的日志展示。

        mode="simple" （默认）：全文搜索错误行（error/exception/fail/traceback），
            命中的行连带前后各10行上下文一起返回，适合快速定位问题。
        mode="detail"：直接从 buffer 中间切片读取，支持 offset+limit 翻页，
            用于查看原始日志正文。
        """
        key = (project_name, instance_id, task_name)
        if key not in self._log_buffer:
            raise RuntimeError(
                f"日志未预加载，请先调用 load_task_full_log "
                f"(project_name={project_name!r}, instance_id={instance_id}, task_name={task_name!r})"
            )

        lines = self._log_buffer[key]
        total = len(lines)

        if mode == "detail":
            safe_end = min(offset + limit, total)
            slice_lines = lines[offset:safe_end]
            return {
                "task_name":  task_name,
                "total_lines": total,
                "offset":     offset,
                "limit":      limit,
                "lines":      slice_lines,
                "has_more":   safe_end < total,
            }

        # --- simple 模式 ---
        # (?i)=忽略大小写，\b词边界
        PATTERNS = [
            re.compile(r"\berror\b", re.IGNORECASE),
            re.compile(r"exception", re.IGNORECASE),
            re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE),
            re.compile(r"traceback", re.IGNORECASE),
        ]
        CTX = 10  # 前后各10行上下文
        matches: list[dict] = []

        for i, line in enumerate(lines):
            if any(p.search(line) for p in PATTERNS):
                ctx_start = max(0, i - CTX)
                ctx_end   = min(total, i + CTX + 1)
                matches.append({
                    "line_no":  i,
                    "context":  [
                        f"[{j}] {lines[j]}" for j in range(ctx_start, ctx_end)
                    ],
                })

        # 按行号去重，相邻命中（跨越少于CTX行）视为同一批次，只保留首个
        # 这样即使有上百个错误，也不会返回数百个几乎一样的上下文块
        collapsed: list[dict] = []
        last_end = -1
        for m in matches:
            if m["line_no"] > last_end:
                collapsed.append(m)
                last_end = m["line_no"] + CTX * 2  # 本块的尾行，重叠检测门槛放宽到2倍ctx

        return {
            "task_name":      task_name,
            "total_lines":    total,
            "matched_count":  len(collapsed),
            "matches":        collapsed,
        }


# ── 默认单例 ────────────────────────────────────────────────────

_ds_client = DSClient()


# ══════════════════════════════════════════════════════════════════════
# MCP 工具层（透明转发到默认单例）
# ══════════════════════════════════════════════════════════════════════

# ── 项目 & 工作流查询 ──────────────────────────────────────────

@ds_mcp.tool(name="get_projects")
@log_function_info
def mcp_get_projects() -> str:
    """
    获取 DolphinScheduler 项目清单。

    Returns:
        JSON 格式的项目列表，每项含 name/code/description 等字段
    """
    try:
        projects = _ds_client.get_projects()
        return json.dumps(projects, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_projects 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="get_workflows")
@log_function_info
def mcp_get_workflows(project_name: str, search: str = "", page: int = 1) -> str:
    """
    获取工作流清单。

    Args:
        project_name: 项目名称（必填）
        search: 工作流名称模糊搜索关键字（可选）
        page: 页码，从1开始（可选，默认1）

    Returns:
        JSON 格式，含 totalList（工作流列表）、totalPage、totalCount
    """
    try:
        result = _ds_client.get_workflows(project_name, search, page)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_workflows 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="get_instances")
@log_function_info
def mcp_get_instances(project_name: str, search: str = "",
                       state: str = "") -> str:
    """
    获取工作流实例清单。

    Args:
        project_name: 项目名称（必填）
        search: 实例名称模糊搜索关键字（可选）
        state: 状态过滤，可选值 SUBMITTED_SUCCESS / RUNNING_EXECUTION /
               PAUSE / SUCCESS / FAILURE / KILL 等（可选，空则不过滤）

    Returns:
        JSON 格式的实例列表（分页自动聚合，最多100页×100条）
    """
    try:
        instances = _ds_client.get_instances(project_name, search, state)
        brief = [
            {"id": i.get("id"), "name": i.get("name"),
             "state": i.get("state"), "host": i.get("host"),
             "startTime": i.get("startTime"), "endTime": i.get("endTime")}
            for i in instances
        ]
        return json.dumps(brief, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_instances 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 工作流上下线 ────────────────────────────────────────────────

@ds_mcp.tool(name="workflow_online")
@log_function_info
def mcp_workflow_online(project_name: str, workflow_name: str) -> str:
    """
    上线工作流（发布）。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）

    Returns:
        DS API 原生响应，success=true 表示成功
    """
    try:
        resp = _ds_client.workflow_online(project_name, workflow_name)
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"workflow_online 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="workflow_offline")
@log_function_info
def mcp_workflow_offline(project_name: str, workflow_name: str) -> str:
    """
    下线工作流。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）

    Returns:
        DS API 原生响应，success=true 表示成功
    """
    try:
        resp = _ds_client.workflow_offline(project_name, workflow_name)
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"workflow_offline 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 定时调度 ────────────────────────────────────────────────────

@ds_mcp.tool(name="schedule_online")
@log_function_info
def mcp_schedule_online(project_name: str, workflow_name: str) -> str:
    """
    将工作流的定时调度上线（定时任务开始生效）。
    注意：工作流本身须先调用 workflow_online，才能进行定时上线。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）

    Returns:
        DS API 原生响应，success=true 表示成功
    """
    try:
        resp = _ds_client.schedule_online(project_name, workflow_name)
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"schedule_online 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="schedule_offline")
@log_function_info
def mcp_schedule_offline(project_name: str, workflow_name: str) -> str:
    """
    将工作流的定时调度下线（暂停定时任务）。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）

    Returns:
        DS API 原生响应，success=true 表示成功
    """
    try:
        resp = _ds_client.schedule_offline(project_name, workflow_name)
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"schedule_offline 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 补数 ───────────────────────────────────────────────────────

@ds_mcp.tool(name="complement_data")
@log_function_info
def mcp_complement_data(project_name: str, workflow_name: str,
                          start_date: str, end_date: str) -> str:
    """
    对工作流进行日期区间补数。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）
        start_date: 开始日期，格式 YYYY-MM-DD 或完整 datetime（必填）
        end_date: 结束日期，同上格式（必填）

    Returns:
        DS API 原生响应，包含提交的补数实例信息
    """
    try:
        resp = _ds_client.complement_data(
            project_name, workflow_name, start_date, end_date
        )
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"complement_data 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── 实例操作 ────────────────────────────────────────────────────

@ds_mcp.tool(name="execute_instance")
@log_function_info
def mcp_execute_instance(project_name: str, instance_id: int,
                           execute_type: str = "") -> str:
    """
    对工作流实例执行操作（重跑/停止/从失败处继续）。

    Args:
        project_name: 项目名称（必填）
        instance_id: 实例ID（必填）
        execute_type: 操作类型，支持三种：
                     - REPEAT_RUNNING           重跑
                     - STOP                     停止
                     - START_FAILURE_TASK_PROCESS  从失败节点继续
                     （若留空或非法值，返回支持的类型列表）

    Returns:
        DS API 原生响应；输入非法时返回 {error, supported_types} 提示
    """
    try:
        resp = _ds_client.execute_instance(project_name, instance_id, execute_type)
        return json.dumps(resp, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"execute_instance 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="list_instance_tasks")
@log_function_info
def mcp_list_instance_tasks(project_name: str, instance_id: int) -> str:
    """
    查看实例下所有任务的状态概览（不含日志，快速定位失败任务）。

    Args:
        project_name: 项目名称（必填）
        instance_id: 实例ID（必填）

    Returns:
        JSON 数组，每项含 task_name/task_type/state/start_time/end_time/host/focus_level
    """
    try:
        tasks = _ds_client.list_instance_tasks(project_name, instance_id)
        return json.dumps(tasks, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"list_instance_tasks 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="load_task_full_log")
@log_function_info
def mcp_load_task_full_log(project_name: str, instance_id: int,
                            task_name: str) -> str:
    """
    预加载单个任务的所有日志（每页1000行自动循环读完），此后可直接调
    get_task_log 进行 simple/detail 查看，多次调用不会重复拉取。

    Args:
        project_name: 项目名称（必填）
        instance_id: 实例ID（必填）
        task_name: 具体任务名称（必填，可从 list_instance_tasks 结果取得）（必填）

    Returns:
        JSON，含 task_name/total_lines/loaded_at
    """
    try:
        meta = _ds_client.load_task_full_log(project_name, instance_id, task_name)
        return json.dumps(meta, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"load_task_full_log 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@ds_mcp.tool(name="get_task_log")
@log_function_info
def mcp_get_task_log(project_name: str, instance_id: int,
                     task_name: str,
                     mode: str = "simple",
                     offset: int = 0,
                     limit: int = 200) -> str:
    """
    查看任务日志。须先调用 load_task_full_log 完成预加载。

    Args:
        project_name: 项目名称（必填）
        instance_id: 实例ID（必填）
        task_name: 任务名称（必填）
        mode: 查看模式。可选 "simple"（默认，只返回错误行及±10行上下文）
                   或 "detail"（返回原始日志片段，支持 offset+limit 分页）
        offset: 起始行号，mode=detail 时生效，默认0
        limit: 最大返回行数，mode=detail 时生效，默认200

    Returns:
        JSON 对象。simple 模式下含 matched_count + matches（行号+上下文）；
        detail 模式下含 lines + has_more
    """
    try:
        result = _ds_client.get_task_log(
            project_name, instance_id, task_name,
            mode=mode, offset=offset, limit=limit
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_task_log 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)