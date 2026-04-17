"""DolphinScheduler 3.1.3 Python Client"""

import json

import requests

# 项目白名单，为空则不限制。只有此处列出的项目及其工作流/实例可被操作。
ALLOWED_PROJECTS: list[str] = []

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


class DSClient:
    def __init__(self, base_url: str, token: str):
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
        # DS state filter is not strict, do client-side filtering
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
