"""Spark SQL 查询性能分析器。

持续从 Spark ThriftServer 的 Spark Web UI（端口 4040）REST API 增量拉取
查询执行记录，分析慢查询和数据倾斜，并把分析报告原子发布 + 落盘成文件。

设计对标 tools/ds_code_search.py：
- 用 AtomicSnapshot 原子发布最新报告
- 用 threading.Timer 自续命的 daemon 线程做后台轮询
- ensure_started() 幂等启动，stop() 优雅停止
- 历史记录通过 utils/cache.py (diskcache) 持久化，进程重启不丢

本轮只产出报告文件，不暴露 MCP/REST（后续按需再加）。
"""

import json
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.cache import cache
from utils.cache_snapshot import AtomicSnapshot
from utils.config import config
from utils.logger import logger
from utils.spark_rest_client import SparkRestClient, parse_metric_bytes

# diskcache 里的历史记录键（expire=None 永久保存）。
_HISTORY_CACHE_KEY = "spark_sql_history"
# diskcache 里的水位线键，记录上次处理到的 (app_id, sql_id)。
_WATERMARK_CACHE_KEY = "spark_sql_watermark"

# 报告里 SQL 文本的最大长度，避免报告文件膨胀。
_MAX_SQL_TEXT_IN_REPORT = 500


class SparkSqlAnalyzer:
    """Spark SQL 查询性能分析器（后台轮询 + 原子报告发布）。"""

    def __init__(self):
        self._enabled: bool = bool(config.get("spark_sql_analyzer.enabled", False))
        self._base_url: str = config.get(
            "spark_sql_analyzer.base_url", "http://localhost:4040"
        )
        self._poll_interval: float = float(
            config.get("spark_sql_analyzer.poll_interval_seconds", 30)
        )
        self._report_path: Path = Path(
            config.get("spark_sql_analyzer.report_path", "logs/spark_sql_report.json")
        )
        self._retention: int = int(config.get("spark_sql_analyzer.history_retention", 5000))
        self._slow_query_ms: float = float(
            config.get("spark_sql_analyzer.slow_query_ms", 30000)
        )
        self._skew_enabled: bool = bool(
            config.get("spark_sql_analyzer.skew.enabled", True)
        )
        self._shuffle_skew_ratio: float = float(
            config.get("spark_sql_analyzer.skew.shuffle_skew_ratio", 4.0)
        )
        self._duration_skew_ratio: float = float(
            config.get("spark_sql_analyzer.skew.duration_skew_ratio", 4.0)
        )
        self._min_tasks_for_skew: int = int(
            config.get("spark_sql_analyzer.skew.min_tasks_for_skew", 20)
        )

        self._client = SparkRestClient(base_url=self._base_url)

        # 原子快照：发布的最新报告，读者永远拿到完整一致的报告。
        self._snapshot: AtomicSnapshot[Dict[str, Any]] = AtomicSnapshot(
            {"generated_at": None, "status": "not_started"}
        )

        # 内存历史：启动时从 diskcache 恢复。每条记录是规范化后的 SQL 执行摘要。
        self._history: List[Dict[str, Any]] = []
        # 水位线：(app_id, max_sql_id)，避免重复处理。
        self._watermark: Dict[str, int] = {}

        # 后台调度
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._started = False

    # ==================================================================
    # 生命周期
    # ==================================================================
    def ensure_started(self) -> None:
        """幂等启动后台轮询。由 main.py 的 lifespan 调用。"""
        if not self._enabled:
            logger.info("Spark SQL 分析器未启用（spark_sql_analyzer.enabled=false）")
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        # 恢复历史 + 水位线
        self._restore_state()
        logger.info(
            f"Spark SQL 分析器已启动，每 {self._poll_interval}s 轮询 "
            f"{self._base_url}，报告写入 {self._report_path}"
        )
        # 立即跑一轮，再调度下一次
        self._poll_once()

    def stop(self) -> None:
        """停止后台轮询。由 main.py shutdown_resources() 调用。"""
        with self._lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
                self._refresh_timer = None
            self._started = False
        logger.info("Spark SQL 分析器已停止")

    def get_report(self) -> Dict[str, Any]:
        """获取最新报告快照（供后续的 MCP/REST 工具调用）。"""
        return self._snapshot.get()

    # ==================================================================
    # 状态恢复与持久化
    # ==================================================================
    def _restore_state(self) -> None:
        """启动时从 diskcache 恢复历史记录和水位线。"""
        history = cache.get(_HISTORY_CACHE_KEY, default=[])
        if isinstance(history, list):
            self._history = history
        watermark = cache.get(_WATERMARK_CACHE_KEY, default={})
        if isinstance(watermark, dict):
            self._watermark = watermark
        logger.info(
            f"Spark SQL 分析器恢复状态：{len(self._history)} 条历史，"
            f"水位线 {self._watermark}"
        )

    def _persist_state(self) -> None:
        """把历史记录和水位线写回 diskcache（裁剪到 retention 上限）。"""
        # 按时间裁剪：保留最近的 retention 条
        if len(self._history) > self._retention:
            self._history = self._history[-self._retention :]
        try:
            cache.set(_HISTORY_CACHE_KEY, self._history, expire=None)
            cache.set(_WATERMARK_CACHE_KEY, self._watermark, expire=None)
        except Exception as exc:
            logger.error(f"Spark SQL 分析器持久化状态失败: {exc}")

    # ==================================================================
    # 轮询主循环
    # ==================================================================
    def _poll_once(self) -> None:
        """跑一轮：增量拉取 → 入历史 → 分析 → 发布 + 写文件 → 调度下一次。"""
        try:
            app_id = self._client.get_app_id()
            if app_id is None:
                logger.debug(
                    f"Spark Web UI 不可用或无活跃应用 ({self._base_url})，跳过本轮"
                )
            else:
                self._fetch_new_queries(app_id)
                report = self._analyze(app_id)
                self._publish_report(report)
        except Exception as exc:
            # 后台任务绝不能因为一轮异常中断整个轮询链
            logger.error(f"Spark SQL 分析器轮询异常: {exc}")
        finally:
            self._schedule_next()

    def _fetch_new_queries(self, app_id: str) -> None:
        """从 REST API 增量拉取新 SQL 执行记录，并入历史。"""
        since_id = self._watermark.get(app_id, -1)
        new_executions = self._client.list_sql_executions(app_id, since_id=since_id)
        if not new_executions:
            return

        new_records: List[Dict[str, Any]] = []
        max_id = since_id
        for exec_summary in new_executions:
            sql_id = exec_summary.get("id")
            if sql_id is None or sql_id <= since_id:
                continue
            record = self._normalize_execution(exec_summary)
            new_records.append(record)
            if sql_id > max_id:
                max_id = sql_id

        if new_records:
            self._history.extend(new_records)
            self._watermark[app_id] = max_id
            self._persist_state()
            logger.info(
                f"Spark SQL 分析器拉取 {len(new_records)} 条新查询 "
                f"(app={app_id}, up to sql_id={max_id})"
            )

    def _normalize_execution(self, exec_summary: Dict[str, Any]) -> Dict[str, Any]:
        """把 REST API 的 SQL 执行摘要规范化为内部记录。"""
        success_jobs = exec_summary.get("successJobIds") or []
        failed_jobs = exec_summary.get("failedJobIds") or []
        running_jobs = exec_summary.get("runningJobIds") or []
        all_jobs = list({*success_jobs, *failed_jobs, *running_jobs})

        return {
            "app_id": None,  # 由调用方在分析时回填（拉取时已知）
            "sql_id": exec_summary.get("id"),
            "description": exec_summary.get("description") or "",
            "status": exec_summary.get("status") or "",
            "submission_time": exec_summary.get("submissionTime"),
            "duration_ms": exec_summary.get("duration") or 0,
            "job_ids": sorted(all_jobs),
            "failed_job_ids": sorted(failed_jobs),
            # 保留 nodes 用于 SQL 级粗筛（shuffle / spill 量），但 planDescription
            # 太长不存历史，只在分析当轮临时持有。
            "node_summary": self._summarize_nodes(exec_summary.get("nodes") or []),
        }

    def _summarize_nodes(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从 plan nodes 里抽出与性能相关的 metric 摘要。

        每个 node 取 shuffle 写入字节、shuffle 读取字节、spill 内存/磁盘字节，
        用于 SQL 级粗筛是否有倾斜迹象。
        """
        summary: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_summary: Dict[str, Any] = {
                "node_id": node.get("nodeId"),
                "name": node.get("nodeName") or "",
            }
            metrics = node.get("metrics") or []
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                name = (metric.get("name") or "").lower()
                raw_value = metric.get("value")
                if "shuffle bytes written" in name:
                    node_summary["shuffle_bytes_written"] = parse_metric_bytes(raw_value)
                elif "shuffle bytes read" in name or "local bytes read" in name:
                    # local bytes read 是 shuffle read 的主要组成
                    node_summary.setdefault(
                        "shuffle_bytes_read", parse_metric_bytes(raw_value)
                    )
                elif "spill" in name and "memory" in name:
                    node_summary["spill_memory_bytes"] = parse_metric_bytes(raw_value)
                elif "spill" in name and "disk" in name:
                    node_summary["spill_disk_bytes"] = parse_metric_bytes(raw_value)
            summary.append(node_summary)
        return summary

    # ==================================================================
    # 分析
    # ==================================================================
    def _analyze(self, app_id: str) -> Dict[str, Any]:
        """基于当前历史生成完整分析报告。"""
        # 只分析当前 app 的记录（跨 app 重启后旧 app 的记录仅作历史保留）
        records = [r for r in self._history if True]  # 全量分析，不按 app 过滤
        for r in records:
            r["app_id"] = r.get("app_id") or app_id

        durations = [r["duration_ms"] for r in records if r.get("duration_ms")]
        slow_queries = self._find_slow_queries(records)
        failed_queries = self._find_failed_queries(records)
        skew_findings = self._detect_skew(records, app_id)

        report = {
            "generated_at": _now_iso(),
            "app_id": app_id,
            "base_url": self._base_url,
            "window": {
                "total_queries": len(records),
                "since": records[0]["submission_time"] if records else None,
                "until": records[-1]["submission_time"] if records else None,
            },
            "summary": {
                "total_queries": len(records),
                "slow_query_count": len(slow_queries),
                "failed_query_count": len(failed_queries),
                "skew_finding_count": len(skew_findings),
                "p50_ms": _percentile(durations, 50),
                "p95_ms": _percentile(durations, 95),
                "p99_ms": _percentile(durations, 99),
                "max_ms": max(durations) if durations else 0,
            },
            "slow_queries": slow_queries,
            "skew_findings": skew_findings,
            "failed_queries": failed_queries,
            "top_queries_by_duration": self._top_queries(records, "duration_ms", 10),
            "top_queries_by_shuffle": self._top_queries_by_shuffle(records, 10),
        }
        return report

    def _find_slow_queries(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """找出 duration 超过阈值的查询。"""
        results: List[Dict[str, Any]] = []
        for r in records:
            duration = r.get("duration_ms") or 0
            if duration >= self._slow_query_ms:
                results.append(
                    {
                        "sql_id": r.get("sql_id"),
                        "app_id": r.get("app_id"),
                        "sql": _truncate_sql(r.get("description")),
                        "duration_ms": duration,
                        "threshold_ms": self._slow_query_ms,
                        "submission_time": r.get("submission_time"),
                        "job_ids": r.get("job_ids"),
                    }
                )
        results.sort(key=lambda x: x["duration_ms"], reverse=True)
        return results

    def _find_failed_queries(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """找出失败的查询。"""
        results: List[Dict[str, Any]] = []
        for r in records:
            status = (r.get("status") or "").upper()
            if status in ("FAILED", "ERROR") or r.get("failed_job_ids"):
                results.append(
                    {
                        "sql_id": r.get("sql_id"),
                        "app_id": r.get("app_id"),
                        "sql": _truncate_sql(r.get("description")),
                        "status": r.get("status"),
                        "failed_job_ids": r.get("failed_job_ids"),
                        "submission_time": r.get("submission_time"),
                    }
                )
        return results

    def _detect_skew(
        self, records: List[Dict[str, Any]], app_id: str
    ) -> List[Dict[str, Any]]:
        """两级数据倾斜检测。

        第一级（SQL 级，便宜）：扫每条记录的 node_summary，发现存在较大 shuffle
        量的 Exchange 节点就标记为「疑似」。
        第二级（task 级，下钻）：对疑似查询，沿 SQL→job→stage→task 拉取 task 明细，
        计算 max/median 比值，超过配置阈值且样本足够才「确认」。
        """
        if not self._skew_enabled:
            return []

        findings: List[Dict[str, Any]] = []

        for r in records:
            # 第一级：SQL 级粗筛
            suspected_nodes = self._suspected_skew_nodes(r)
            if not suspected_nodes:
                continue

            job_ids = r.get("job_ids") or []
            if not job_ids:
                # 没有关联 job（纯命令型 SQL），无法下钻到 task，只记 SQL 级疑似
                for node in suspected_nodes:
                    findings.append(
                        self._make_skew_finding(
                            r, app_id, node, severity="suspected", detail=None
                        )
                    )
                continue

            # 第二级：task 级下钻
            stage_skews = self._drill_stage_skew(app_id, job_ids)
            if stage_skews:
                for skew in stage_skews:
                    findings.append(
                        self._make_skew_finding(
                            r, app_id, suspected_nodes[0],
                            severity="confirmed", detail=skew,
                        )
                    )
            else:
                # task 级没确认（可能 task 数太少或比值未达阈值），保留疑似
                for node in suspected_nodes:
                    findings.append(
                        self._make_skew_finding(
                            r, app_id, node, severity="suspected", detail=None
                        )
                    )

        return findings

    def _suspected_skew_nodes(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """SQL 级粗筛：返回有显著 shuffle 量的节点列表。"""
        suspected: List[Dict[str, Any]] = []
        for node in record.get("node_summary") or []:
            shuffle_written = node.get("shuffle_bytes_written") or 0
            shuffle_read = node.get("shuffle_bytes_read") or 0
            spill = node.get("spill_memory_bytes") or 0
            # 启发式：有 shuffle 量（>1MB）或有 spill，就值得下钻确认
            if shuffle_written > 1024 * 1024 or shuffle_read > 1024 * 1024 or spill > 0:
                suspected.append(node)
        return suspected

    def _drill_stage_skew(
        self, app_id: str, job_ids: List[int]
    ) -> List[Dict[str, Any]]:
        """对疑似查询下钻到 task 级，确认是否真的倾斜。

        沿 job → stageIds → stage tasks 链路拉取，计算每个 stage 的
        shuffle bytes / duration 的 max/median 比值。
        """
        stage_skews: List[Dict[str, Any]] = []

        # 收集所有相关 stage id（去重）
        stage_ids: List[int] = []
        for job_id in job_ids:
            job = self._client.get_job(app_id, job_id)
            if not job:
                continue
            for sid in job.get("stageIds") or []:
                if sid not in stage_ids:
                    stage_ids.append(sid)

        for stage_id in stage_ids:
            tasks = self._client.get_stage_tasks(app_id, stage_id, attempt_id=0)
            if len(tasks) < self._min_tasks_for_skew:
                continue

            # 计算 shuffle bytes（读+写）和 duration 的分布
            shuffle_bytes_list: List[float] = []
            durations: List[float] = []
            max_task_ref: Dict[str, Any] = {}
            max_shuffle = -1.0
            for t in tasks:
                tm = t.get("taskMetrics") or {}
                sw = (tm.get("shuffleWriteMetrics") or {}).get("bytesWritten") or 0
                sr = (tm.get("shuffleReadMetrics") or {})
                read_bytes = (
                    (sr.get("localBytesRead") or 0)
                    + (sr.get("remoteBytesRead") or 0)
                    + (sr.get("remoteBytesReadToDisk") or 0)
                )
                total_shuffle = sw + read_bytes
                shuffle_bytes_list.append(total_shuffle)
                durations.append(t.get("duration") or 0)
                if total_shuffle > max_shuffle:
                    max_shuffle = total_shuffle
                    max_task_ref = {
                        "task_id": t.get("taskId"),
                        "host": t.get("host"),
                        "duration_ms": t.get("duration"),
                        "shuffle_bytes": total_shuffle,
                    }

            skew_found = self._evaluate_skew_for_metric(
                stage_id, "shuffle_bytes", shuffle_bytes_list, max_task_ref
            )
            if skew_found:
                stage_skews.append(skew_found)
                continue  # 同一 stage 只报一种最显著的

            # shuffle 没倾斜，再看 duration
            skew_found = self._evaluate_skew_for_metric(
                stage_id, "duration_ms", durations, max_task_ref
            )
            if skew_found:
                stage_skews.append(skew_found)

        return stage_skews

    def _evaluate_skew_for_metric(
        self,
        stage_id: int,
        metric_name: str,
        values: List[float],
        max_task_ref: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """计算单个 metric 的 max/median 比值，超过阈值返回倾斜描述。"""
        if not values or len(values) < self._min_tasks_for_skew:
            return None
        max_val = max(values)
        median_val = statistics.median(values)
        if median_val <= 0:
            # median 为 0 时比值无意义；只有当大量 task 为 0 而少数非 0 才算倾斜
            nonzero = [v for v in values if v > 0]
            if len(nonzero) > 0 and len(nonzero) < len(values) * 0.3:
                ratio = float("inf")
            else:
                return None
        else:
            ratio = max_val / median_val

        if metric_name == "shuffle_bytes":
            threshold_ratio = self._shuffle_skew_ratio
        else:
            threshold_ratio = self._duration_skew_ratio

        if ratio < threshold_ratio:
            return None

        return {
            "stage_id": stage_id,
            "metric": metric_name,
            "max": max_val,
            "median": median_val,
            "ratio": ratio if ratio != float("inf") else -1,
            "task_count": len(values),
            "max_task": max_task_ref,
        }

    def _make_skew_finding(
        self,
        record: Dict[str, Any],
        app_id: str,
        node: Dict[str, Any],
        severity: str,
        detail: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        finding = {
            "sql_id": record.get("sql_id"),
            "app_id": app_id,
            "severity": severity,
            "node": node.get("name"),
            "node_id": node.get("node_id"),
            "sql": _truncate_sql(record.get("description")),
            "submission_time": record.get("submission_time"),
        }
        if detail:
            finding["stage_id"] = detail.get("stage_id")
            finding["metric"] = detail.get("metric")
            finding["max"] = detail.get("max")
            finding["median"] = detail.get("median")
            finding["ratio"] = detail.get("ratio")
            finding["task_count"] = detail.get("task_count")
            finding["max_task"] = detail.get("max_task")
        return finding

    def _top_queries(
        self, records: List[Dict[str, Any]], key: str, limit: int
    ) -> List[Dict[str, Any]]:
        """按某个数值字段取 top N。"""
        valid = [r for r in records if r.get(key) is not None]
        valid.sort(key=lambda x: x.get(key) or 0, reverse=True)
        return [
            {
                "sql_id": r.get("sql_id"),
                "app_id": r.get("app_id"),
                "sql": _truncate_sql(r.get("description")),
                key: r.get(key),
                "submission_time": r.get("submission_time"),
            }
            for r in valid[:limit]
        ]

    def _top_queries_by_shuffle(
        self, records: List[Dict[str, Any]], limit: int
    ) -> List[Dict[str, Any]]:
        """按 SQL 级总 shuffle 字节取 top N（汇总所有 node 的 shuffle）。"""
        scored: List[Dict[str, Any]] = []
        for r in records:
            total_shuffle = 0.0
            for node in r.get("node_summary") or []:
                total_shuffle += (node.get("shuffle_bytes_written") or 0) + (
                    node.get("shuffle_bytes_read") or 0
                )
            if total_shuffle > 0:
                scored.append(
                    {
                        "sql_id": r.get("sql_id"),
                        "app_id": r.get("app_id"),
                        "sql": _truncate_sql(r.get("description")),
                        "shuffle_bytes": total_shuffle,
                        "submission_time": r.get("submission_time"),
                    }
                )
        scored.sort(key=lambda x: x["shuffle_bytes"], reverse=True)
        return scored[:limit]

    # ==================================================================
    # 报告发布
    # ==================================================================
    def _publish_report(self, report: Dict[str, Any]) -> None:
        """原子发布 + 写文件。"""
        self._snapshot.replace(report)
        self._write_report_file(report)

    def _write_report_file(self, report: Dict[str, Any]) -> None:
        """把报告覆盖写到配置的文件路径（相对项目根目录）。"""
        try:
            # 相对路径基于项目根目录（本文件在 tools/ 下，父目录是项目根）
            report_path = self._report_path
            if not report_path.is_absolute():
                project_root = Path(__file__).resolve().parent.parent
                report_path = project_root / report_path
            report_path.parent.mkdir(parents=True, exist_ok=True)
            # 先写临时文件再 rename，保证原子替换（读者不会看到半截 JSON）
            tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            tmp_path.replace(report_path)
        except Exception as exc:
            logger.error(f"Spark SQL 分析器写报告文件失败: {exc}")

    def _schedule_next(self) -> None:
        """调度下一轮轮询（自续命）。"""
        with self._lock:
            if not self._started:
                return
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            self._refresh_timer = threading.Timer(
                self._poll_interval, self._poll_once
            )
            self._refresh_timer.daemon = True
            self._refresh_timer.start()


# ======================================================================
# 辅助函数
# ======================================================================
def _now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _percentile(values: List[float], p: float) -> float:
    """计算百分位数（线性插值）。空列表返回 0。"""
    if not values:
        return 0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    return statistics.quantiles(sorted_vals, n=100, method="inclusive")[int(p) - 1]


def _truncate_sql(text: Optional[str]) -> str:
    """截断 SQL 文本，避免报告膨胀。"""
    if not text:
        return ""
    text = str(text)
    if len(text) <= _MAX_SQL_TEXT_IN_REPORT:
        return text
    return text[: _MAX_SQL_TEXT_IN_REPORT - 1].rstrip() + "…"


# 模块级单例。区别于其它工具模块，本模块不创建 FastMCP 实例——
# 本轮只做记录 + 分析 + 文件输出，不暴露 MCP/REST。
spark_analyzer = SparkSqlAnalyzer()
