"""DolphinScheduler 3.1.3 Python Client"""

import json

import requests
import yaml
from pathlib import Path

from utils.logger import logger
from utils.decorators import log_function_info
from fastmcp import FastMCP

# ── 从配置文件加载 ────────────────────────────────────────────────

_config_path = Path(__file__).parent.parent / "config" / "config.yaml"
_ds_cfg: dict = {}

if _config_path.exists():
    try:
        with open(_config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            _ds_cfg = cfg.get("dolphin_scheduler", {}) or {}
    except Exception as e:
        logger.warning(f"DolphinScheduler 配置加载失败: {e}")

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

    # ── internal helpers ──────────────────────────────────────────

    def _request(self, method: str, path: str, data: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
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
            resp = self._request("GET", PATH_PROJECTS)
            projects = resp.get("data", [])
            if ALLOWED_PROJECTS:
                projects = [p for p in projects if p["name"] in ALLOWED_PROJECTS]
            for p in projects:
                self._project_cache[p["name"]] = p["code"]
            return projects
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
        resp = self._request("GET", PATH_WORKFLOWS.format(project_code=code),
                             params=params)
        data = resp.get("data", {})
        return {
            "totalList": data.get("totalList", []),
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

    def get_instance_logs(self, project_name: str, instance_id: int) -> list[dict]:
        """获取实例下所有任务的日志。返回 [{task_name, state, log}, ...]"""
        pc = self._get_project_code(project_name)
        resp = self._request("GET",
                             PATH_INSTANCE_TASKS.format(project_code=pc, instance_id=instance_id))
        task_list = resp.get("data", {}).get("taskList", [])
        if not task_list:
            return []
        results = []
        for task in task_list:
            task_id = task.get("id")
            task_name = task.get("name", "unknown")
            state = task.get("state", "UNKNOWN")
            log_text = ""
            if task_id:
                try:
                    log_resp = self._request(
                        "GET", PATH_LOG_DETAIL,
                        params={"taskInstanceId": task_id,
                                "skipLineNum": 0, "limit": 10000})
                    log_data = log_resp.get("data", {})
                    if isinstance(log_data, dict):
                        log_text = log_data.get("message", "")
                    else:
                        log_text = str(log_data)
                except (RuntimeError, IOError, json.JSONDecodeError) as e:
                    log_text = f"获取日志失败: {e}"
            results.append({
                "task_name": task_name,
                "state": state,
                "log": log_text,
            })
        return results


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
                          start_date: str, end_date: str,
                          parallel: bool = False) -> str:
    """
    对工作流进行日期区间补数。

    Args:
        project_name: 项目名称（必填）
        workflow_name: 工作流名称（必填）
        start_date: 开始日期，格式 YYYY-MM-DD 或完整 datetime（必填）
        end_date: 结束日期，同上格式（必填）
        parallel: 是否并行执行（默认 False=串行）

    Returns:
        DS API 原生响应，包含提交的补数实例信息
    """
    try:
        resp = _ds_client.complement_data(
            project_name, workflow_name, start_date, end_date, parallel
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


@ds_mcp.tool(name="get_instance_logs")
@log_function_info
def mcp_get_instance_logs(project_name: str, instance_id: int) -> str:
    """
    获取工作流实例下所有任务节点的运行日志。

    Args:
        project_name: 项目名称（必填）
        instance_id: 实例ID（必填）

    Returns:
        JSON 数组，每项含 task_name（任务名）/state（状态）/log（日志正文）
    """
    try:
        logs = _ds_client.get_instance_logs(project_name, instance_id)
        return json.dumps(logs, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"get_instance_logs 失败: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)