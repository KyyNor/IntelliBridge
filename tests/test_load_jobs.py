import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

loguru_stub = types.ModuleType("loguru")
loguru_stub.logger = types.SimpleNamespace(
    remove=lambda *a, **k: None,
    add=lambda *a, **k: None,
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)
sys.modules.setdefault("loguru", loguru_stub)

from utils.load_jobs import (
    JOB_ID_PATTERN,
    LoadJobCancelled,
    LoadJobManager,
)


def _wait_terminal(manager, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = manager.get_view(job_id)
        if view is not None and view["status"] in ("ready", "failed"):
            return view
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内进入终态")


def _write_file(ctx, name, payload=b"parquet-bytes"):
    path = ctx.work_dir / name
    path.write_bytes(payload)
    return ctx.add_file(name)


class LoadJobManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "spool"
        self.manager = LoadJobManager(self.root, ttl_seconds=3600, sweep_interval_seconds=9999)

    def tearDown(self):
        self.manager.stop()
        self.tmp.cleanup()

    def test_submit_success_flow(self):
        view = self.manager.submit(
            "mysql", {"database_id": "n_db", "table": "t"}, lambda ctx: _write_file(ctx, "t.parquet")
        )
        self.assertEqual(view["status"], "pending")
        job_id = view["job_id"]
        self.assertTrue(JOB_ID_PATTERN.match(job_id))

        done = _wait_terminal(self.manager, job_id)
        self.assertEqual(done["status"], "ready")
        self.assertNotIn("row_count", done)  # runner 未 set_result 时无业务字段
        self.assertEqual(len(done["files"]), 1)
        record = done["files"][0]
        self.assertEqual(record["name"], "t.parquet")
        self.assertEqual(record["size"], len(b"parquet-bytes"))
        self.assertEqual(record["id"], "file_1")
        self.assertEqual(done["byte_size"], record["size"])
        self.assertIsNotNone(done["expires_at"])

        # job.json 落盘且内容与视图一致
        state = json.loads((self.root / job_id / "job.json").read_text("utf-8"))
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["view"]["files"][0]["sha256"], record["sha256"])

    def test_runner_failure_marks_failed(self):
        def boom(ctx):
            raise RuntimeError("boom")

        view = self.manager.submit("mysql", {}, boom)
        done = _wait_terminal(self.manager, view["job_id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("boom", done["error"])

    def test_open_file_serves_registered_file(self):
        view = self.manager.submit(
            "mysql", {}, lambda ctx: _write_file(ctx, "t.parquet", b"abc")
        )
        job_id = view["job_id"]
        _wait_terminal(self.manager, job_id)
        opened = self.manager.open_file(job_id, "file_1")
        self.assertIsNotNone(opened)
        path, record = opened
        self.assertEqual(path.read_bytes(), b"abc")
        self.assertEqual(record["name"], "t.parquet")

    def test_open_file_rejects_unknown_or_traversal_ids(self):
        self.assertIsNone(self.manager.open_file("../etc", "file_1"))
        self.assertIsNone(self.manager.open_file("load_123", "file_1"))
        view = self.manager.submit("mysql", {}, lambda ctx: _write_file(ctx, "t.parquet"))
        _wait_terminal(self.manager, view["job_id"])
        self.assertIsNone(self.manager.open_file(view["job_id"], "file_9"))
        self.assertIsNone(self.manager.open_file(view["job_id"], "../../job.json"))
        self.assertIsNone(self.manager.get_view("load_../escape"))

    def test_delete_terminal_job_removes_dir_and_view(self):
        view = self.manager.submit(
            "mysql", {}, lambda ctx: _write_file(ctx, "t.parquet")
        )
        job_id = view["job_id"]
        _wait_terminal(self.manager, job_id)
        result = self.manager.delete(job_id)
        self.assertTrue(result["deleted"])
        self.assertIsNone(self.manager.get_view(job_id))
        self.assertFalse((self.root / job_id).exists())

    def test_delete_running_job_cancels_and_cleans(self):
        release = threading.Event()

        def blocking_runner(ctx):
            release.wait(timeout=5)
            ctx.check_alive()  # 取消后必须抛 LoadJobCancelled
            raise AssertionError("不应到达这里")

        view = self.manager.submit("mysql", {}, blocking_runner)
        job_id = view["job_id"]
        _wait_status = time.monotonic() + 5
        while time.monotonic() < _wait_status and self.manager.get_view(job_id)["status"] != "running":
            time.sleep(0.01)

        result = self.manager.delete(job_id)
        self.assertTrue(result["deleted"])
        release.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (self.root / job_id).exists():
            time.sleep(0.01)
        self.assertFalse((self.root / job_id).exists())
        self.assertIsNone(self.manager.get_view(job_id))

    def test_job_timeout_cooperative(self):
        manager = LoadJobManager(
            Path(self.tmp.name) / "spool2", ttl_seconds=3600, job_timeout_seconds=0.05
        )
        try:
            def slow_runner(ctx):
                for _ in range(200):
                    ctx.check_alive()
                    time.sleep(0.01)

            view = manager.submit("mysql", {}, slow_runner)
            done = _wait_terminal(manager, view["job_id"])
            self.assertEqual(done["status"], "failed")
            self.assertIn("执行时限", done["error"])
        finally:
            manager.stop()

    def test_sweep_removes_expired_ready_jobs(self):
        now = {"t": 1000.0}
        manager = LoadJobManager(
            Path(self.tmp.name) / "spool3",
            ttl_seconds=100,
            sweep_interval_seconds=9999,
            clock=lambda: now["t"],
        )
        try:
            view = manager.submit("mysql", {}, lambda ctx: _write_file(ctx, "t.parquet"))
            job_id = view["job_id"]
            _wait_terminal(manager, job_id)

            # 未过期：不清理
            self.assertEqual(manager.sweep_once(now=now["t"] + 50), [])
            self.assertTrue((manager.spool_root / job_id).exists())

            # 超过 TTL：清理目录与内存索引
            removed = manager.sweep_once(now=now["t"] + 200)
            self.assertEqual(removed, [job_id])
            self.assertFalse((manager.spool_root / job_id).exists())
            self.assertIsNone(manager.get_view(job_id))
        finally:
            manager.stop()

    def test_sweep_removes_crash_leftovers_without_state(self):
        orphan = self.root / "load_0123456789abcdef"
        orphan.mkdir(parents=True)
        (orphan / "junk.parquet").write_bytes(b"x")
        self.assertEqual(self.manager.sweep_once(now=time.time() + 7200), [orphan.name])
        self.assertFalse(orphan.exists())

    def test_restart_recovery_from_disk(self):
        manager_a = LoadJobManager(self.root, ttl_seconds=3600, sweep_interval_seconds=9999)
        view = manager_a.submit(
            "mysql", {}, lambda ctx: _write_file(ctx, "t.parquet", b"restart")
        )
        job_id = view["job_id"]
        _wait_terminal(manager_a, job_id)
        # 模拟重启：新 manager 无内存索引
        manager_b = LoadJobManager(self.root, ttl_seconds=3600, sweep_interval_seconds=9999)
        try:
            recovered = manager_b.get_view(job_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["status"], "ready")
            opened = manager_b.open_file(job_id, "file_1")
            self.assertIsNotNone(opened)
            self.assertEqual(opened[0].read_bytes(), b"restart")

            # 磁盘上残留 running 状态（重启前中断）→ 视为 failed
            running_id = "load_1111111111111111"
            running_dir = self.root / running_id
            running_dir.mkdir(parents=True)
            (running_dir / "job.json").write_text(
                json.dumps({"status": "running", "updated_ts": 1, "expires_ts": None, "view": {"job_id": running_id, "status": "running"}}),
                encoding="utf-8",
            )
            stale = manager_b.get_view(running_id)
            self.assertEqual(stale["status"], "failed")
            self.assertIn("重启", stale["error"])
        finally:
            manager_b.stop()


if __name__ == "__main__":
    unittest.main()
