"""Bounded asynchronous writer used by function-call logging."""

import logging
import queue
import threading
import time
from typing import Callable, Generic, Optional, TypeVar


T = TypeVar("T")
logger = logging.getLogger(__name__)


class BoundedCallLogWriter(Generic[T]):
    """Write records on one daemon worker without unbounded thread creation."""

    def __init__(self, callback: Callable[[T], None], max_queue_size: int = 1024):
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be greater than zero")
        self._callback = callback
        self._queue: queue.Queue[T] = queue.Queue(maxsize=max_queue_size)
        self._state_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stopping = False

    def _ensure_worker(self) -> bool:
        with self._state_lock:
            if self._stopping:
                return False
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="call-log-writer",
                    daemon=True,
                )
                self._worker.start()
            return True

    def submit(self, record: T) -> bool:
        """Queue a record, returning false when the bounded queue is full."""

        if not self._ensure_worker():
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            logger.warning("调用日志队列已满，丢弃一条调用日志")
            return False

    def _run(self) -> None:
        while True:
            try:
                record = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stopping:
                    return
                continue

            try:
                self._callback(record)
            except Exception:
                logger.exception("调用日志后台写入失败")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued records are processed or timeout expires."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(timeout=remaining)
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Drain queued records and stop the worker; safe to call repeatedly."""

        with self._state_lock:
            self._stopping = True
            worker = self._worker
        self.flush(timeout=timeout)
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, timeout))
