"""Load Job 注册表：内存索引 + 磁盘 spool + 协作取消 + TTL 清理。

设计要点：
- job 状态机 pending → running → ready | failed；DELETE 取消运行中的任务（协作式）；
- 每个任务的文件只允许落在 `<spool_root>/<job_id>/` 下，id 严格正则校验，防路径逃逸；
- 状态每次变化都原子落盘 job.json，进程重启后 ready/failed 任务仍可下载（running 残留视为失败）；
- TTL sweeper 以自重排 Timer 守护线程周期清理过期目录与崩溃残留。
"""

import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from utils.logger import logger

JOB_ID_PATTERN = re.compile(r"^load_[0-9a-f]{16}$")
FILE_ID_PATTERN = re.compile(r"^file_\d+$")

TERMINAL_STATUSES = {"ready", "failed"}
_STATE_FILE = "job.json"


class LoadJobError(Exception):
    """Load 任务业务错误（归一化后写入 job error）。"""


class LoadJobCancelled(Exception):
    """任务被 DELETE 主动取消。"""


class LoadJobTimeout(Exception):
    """任务超过执行时限。"""


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class LoadJobContext:
    """runner 内部使用的任务上下文。"""

    def __init__(self, job: dict, manager: "LoadJobManager"):
        self._job = job
        self._manager = manager

    @property
    def job_id(self) -> str:
        return self._job["job_id"]

    @property
    def params(self) -> dict:
        return self._job["params"]

    @property
    def work_dir(self) -> Path:
        return self._job["dir"]

    def check_alive(self) -> None:
        """批次间调用：响应取消与执行时限，避免长导出无法中断。"""
        if self._job["cancel_event"].is_set():
            raise LoadJobCancelled("任务已被取消")
        if time.monotonic() >= self._job["deadline_mono"]:
            raise LoadJobTimeout(f"任务超过执行时限（{self._manager.job_timeout_seconds:g} 秒）")

    def add_file(self, name: str) -> dict:
        """登记一个已生成的导出文件（计算 size/sha256），返回文件描述。"""
        return self._manager._register_file(self._job, name)

    def set_result(self, **fields) -> None:
        self._manager._merge_result(self._job, fields)


class LoadJobManager:
    """管理 Load Job 的提交、状态机、spool 文件与 TTL 回收。"""

    def __init__(
        self,
        spool_root,
        *,
        ttl_seconds: float = 3600.0,
        sweep_interval_seconds: float = 300.0,
        max_workers: int = 2,
        job_timeout_seconds: float = 1800.0,
        clock: Callable[[], float] = time.time,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.spool_root = Path(spool_root)
        self.ttl_seconds = float(ttl_seconds)
        self.sweep_interval_seconds = float(sweep_interval_seconds)
        self.job_timeout_seconds = float(job_timeout_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: Dict[str, dict] = {}
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="load-job"
        )
        self._sweep_timer: Optional[threading.Timer] = None
        self._started = False

    # ==================== 生命周期 ====================

    def ensure_started(self) -> None:
        """启动 TTL sweeper（幂等）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._schedule_next()
        logger.info(
            f"Load spool 清理器已启动: root={self.spool_root} "
            f"ttl={self.ttl_seconds:g}s interval={self.sweep_interval_seconds:g}s"
        )

    def stop(self) -> None:
        with self._lock:
            self._started = False
            timer, self._sweep_timer = self._sweep_timer, None
        if timer is not None:
            timer.cancel()
        self._executor.shutdown(wait=False)
        logger.info("Load spool 清理器已停止")

    def _schedule_next(self) -> None:
        with self._lock:
            if not self._started:
                return
            timer = threading.Timer(self.sweep_interval_seconds, self._sweep_tick)
            self._sweep_timer = timer
        timer.daemon = True
        timer.start()

    def _sweep_tick(self) -> None:
        try:
            removed = self.sweep_once()
            if removed:
                logger.info(f"Load spool TTL 清理: {len(removed)} 个任务 ({', '.join(removed)})")
        except Exception as exc:
            logger.warning(f"Load spool 清理失败: {exc}")
        finally:
            self._schedule_next()

    # ==================== 提交与执行 ====================

    def submit(self, kind: str, params: dict, runner: Callable[[LoadJobContext], dict]) -> dict:
        """创建 job 并异步执行 runner，立即返回 pending 视图。"""
        job_id = f"load_{uuid.uuid4().hex[:16]}"
        job: dict = {
            "job_id": job_id,
            "kind": kind,
            "params": dict(params),
            "status": "pending",
            "created_at": self._clock(),
            "updated_at": self._clock(),
            "expires_ts": None,
            "terminal_ts": None,
            "error": None,
            "result": {},
            "files": [],
            "cancel_event": threading.Event(),
            "deleted": False,
            "deadline_mono": time.monotonic() + self.job_timeout_seconds,
            "dir": self.spool_root / job_id,
        }
        with self._lock:
            self._jobs[job_id] = job
            job["dir"].mkdir(parents=True, exist_ok=True)
            self._persist_locked(job)
            snapshot = self.view(job)  # 先取 pending 快照，避免与线程竞争
        self._executor.submit(self._run_job, job, runner)
        return snapshot

    def _run_job(self, job: dict, runner: Callable[[LoadJobContext], dict]) -> None:
        self._transition(job, "running")
        try:
            if job["cancel_event"].is_set():
                raise LoadJobCancelled("任务已被取消")
            result = runner(LoadJobContext(job, self)) or {}
            with self._lock:
                if job["deleted"]:
                    raise LoadJobCancelled("任务已被取消")
                job["result"].update(result)
                self._finish_locked(job, "ready")
            logger.info(f"Load job 完成: {job['job_id']}")
        except BaseException as exc:  # job 失败必须收敛为 failed 状态，绝不让线程裸抛
            message = str(exc) or exc.__class__.__name__
            self._transition(job, "failed", error=message)
            logger.error(f"Load job 失败: {job['job_id']} - {message}")
        finally:
            self._cleanup_if_deleted(job)

    def _transition(self, job: dict, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            job["status"] = status
            job["error"] = error
            job["updated_at"] = self._clock()
            if status in TERMINAL_STATUSES:
                self._finish_locked(job, status)
            else:
                self._persist_locked(job)

    def _finish_locked(self, job: dict, status: str) -> None:
        job["status"] = status
        job["terminal_ts"] = self._clock()
        job["updated_at"] = job["terminal_ts"]
        job["expires_ts"] = job["terminal_ts"] + self.ttl_seconds
        self._persist_locked(job)

    def _cleanup_if_deleted(self, job: dict) -> None:
        with self._lock:
            deleted = job["deleted"]
            if deleted:
                self._jobs.pop(job["job_id"], None)
        if deleted:
            shutil.rmtree(job["dir"], ignore_errors=True)
            logger.info(f"Load job 已清理: {job['job_id']}")

    # ==================== 查询 ====================

    def view(self, job: dict) -> dict:
        with self._lock:
            view = {
                "job_id": job["job_id"],
                "kind": job["kind"],
                "status": job["status"],
                "params": dict(job["params"]),
                "created_at": _iso(job["created_at"]),
                "updated_at": _iso(job["updated_at"]),
                "expires_at": _iso(job["expires_ts"]),
                "error": job["error"],
                "files": [dict(f) for f in job["files"]],
            }
            view.update(dict(job["result"]))
        view["byte_size"] = sum(f["size"] for f in view["files"])
        return view

    def get_view(self, job_id: str) -> Optional[dict]:
        if not JOB_ID_PATTERN.match(job_id or ""):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return self.view(job)
        # 进程重启恢复：从磁盘读 job.json；running/pending 是重启前残留 → 视为失败
        state = self._read_state(self.spool_root / job_id)
        if state is None:
            return None
        view = state.get("view") or {}
        if state.get("status") in ("pending", "running"):
            view = dict(view)
            view["status"] = "failed"
            view["error"] = "服务重启，任务已中断"
        return view or None

    def open_file(self, job_id: str, file_id: str) -> Optional[Tuple[Path, dict]]:
        """解析下载请求；id 严格校验且路径必须落在任务目录内，防路径逃逸。"""
        if not JOB_ID_PATTERN.match(job_id or "") or not FILE_ID_PATTERN.match(file_id or ""):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            record = next((f for f in job["files"] if f["id"] == file_id), None) if job else None
            job_dir = job["dir"] if job else None
        if record is None:
            # 重启恢复：ready 任务内存索引丢失时从磁盘找
            state = self._read_state(self.spool_root / job_id)
            if state is None or state.get("status") != "ready":
                return None
            record = next((f for f in (state.get("view") or {}).get("files", []) if f["id"] == file_id), None)
            job_dir = self.spool_root / job_id
        if record is None:
            return None
        path = job_dir / record["name"]
        try:
            if path.resolve().parent != job_dir.resolve():
                return None
        except OSError:
            return None
        if not path.is_file():
            return None
        return path, record

    def delete(self, job_id: str) -> Optional[dict]:
        """主动清理：取消运行中的任务并删除目录；不存在返回 None。"""
        if not JOB_ID_PATTERN.match(job_id or ""):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                # 内存无记录（从未存在或已被 TTL 清理），顺带清掉磁盘残留
                shutil.rmtree(self.spool_root / job_id, ignore_errors=True)
                return None
            previous_status = job["status"]
            job["deleted"] = True
            job["cancel_event"].set()
            if job["status"] in TERMINAL_STATUSES:
                self._jobs.pop(job_id, None)
                shutil.rmtree(job["dir"], ignore_errors=True)
                logger.info(f"Load job 已清理: {job_id}")
        return {"job_id": job_id, "status": previous_status, "deleted": True}

    # ==================== 文件登记 / 结果合并 ====================

    def _register_file(self, job: dict, name: str) -> dict:
        path = job["dir"] / name
        size, sha256 = _hash_file(path)
        with self._lock:
            record = {
                "id": f"file_{len(job['files']) + 1}",
                "name": name,
                "format": "parquet",
                "size": size,
                "sha256": sha256,
                "download_url": f"/api/load/{job['job_id']}/files/file_{len(job['files']) + 1}",
            }
            job["files"].append(record)
            self._persist_locked(job)
        return record

    def _merge_result(self, job: dict, fields: dict) -> None:
        with self._lock:
            job["result"].update(fields)
            self._persist_locked(job)

    # ==================== 落盘 / TTL ====================

    def _persist_locked(self, job: dict) -> None:
        state = {
            "status": job["status"],
            "updated_ts": job["updated_at"],
            "expires_ts": job["expires_ts"],
            "view": self.view(job),
        }
        target = job["dir"] / _STATE_FILE
        tmp = job["dir"] / (_STATE_FILE + ".tmp")
        try:
            job["dir"].mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            logger.warning(f"Load job 状态落盘失败: {job['job_id']} - {exc}")

    def _read_state(self, job_dir: Path) -> Optional[dict]:
        try:
            return json.loads((job_dir / _STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def sweep_once(self, now: Optional[float] = None) -> List[str]:
        """清理过期任务目录；返回被清理的 job_id 列表。"""
        current = self._clock() if now is None else now
        removed: List[str] = []
        if not self.spool_root.exists():
            return removed
        for entry in sorted(self.spool_root.iterdir()):
            if not JOB_ID_PATTERN.match(entry.name) or not entry.is_dir():
                continue
            state = self._read_state(entry)
            if state is None:
                # 崩溃残留（无 job.json）：按目录修改时间兜底清理
                try:
                    expired = current - entry.stat().st_mtime > self.ttl_seconds
                except OSError:
                    continue
            elif state.get("status") in TERMINAL_STATUSES and state.get("expires_ts") is not None:
                expired = current >= float(state["expires_ts"])
            else:
                # 运行中超时未推进（线程异常滞留）：按双倍 TTL 兜底
                expired = current - float(state.get("updated_ts") or 0) > 2 * self.ttl_seconds
            if expired:
                shutil.rmtree(entry, ignore_errors=True)
                with self._lock:
                    self._jobs.pop(entry.name, None)
                removed.append(entry.name)
        return removed


def _hash_file(path: Path) -> Tuple[int, str]:
    import hashlib

    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
