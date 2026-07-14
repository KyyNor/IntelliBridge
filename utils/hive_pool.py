"""Bounded Hive query resource manager.

PyHive connections are not shared between requests. A semaphore limits the
number of active connections to the Spark concurrency budget.
"""

from contextlib import contextmanager
import threading
from typing import Callable, Optional

from pyhive import hive

from utils.config import config
from utils.logger import logger


class HiveConnectionPool:
    """Manage independent Hive connections behind a bounded query budget."""

    def __init__(
        self,
        max_concurrent: Optional[int] = None,
        queue_timeout: Optional[float] = None,
        connection_factory: Optional[Callable[..., object]] = None,
    ):
        self.host = config.get("hive.host", "localhost")
        self.port = config.get("hive.port", 10000)
        self.username = config.get("hive.username", "default")
        self.database = config.get("hive.database", "default")
        self.max_concurrent = max(
            1,
            int(
                max_concurrent
                if max_concurrent is not None
                else config.get("hive.max_concurrent", 2)
            ),
        )
        self.queue_timeout = float(
            queue_timeout
            if queue_timeout is not None
            else config.get("hive.queue_timeout", 60.0)
        )
        if self.queue_timeout <= 0:
            raise ValueError("queue_timeout must be greater than zero")

        self._connection_factory = connection_factory or hive.Connection
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)
        self._state_lock = threading.Lock()
        self._connections: set[object] = set()
        self._active = 0
        self._closed = False

    @contextmanager
    def get_connection(self, timeout: Optional[float] = None):
        """Yield one exclusive Hive connection and close it on exit."""

        wait_timeout = self.queue_timeout if timeout is None else float(timeout)
        if wait_timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        acquired = self._semaphore.acquire(timeout=wait_timeout)
        if not acquired:
            raise TimeoutError(
                f"Hive 查询并发已满（最多 {self.max_concurrent} 个），"
                f"等待超过 {wait_timeout:g} 秒"
            )

        connection = None
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Hive 连接管理器已关闭")

            logger.info(f"正在连接 Hive: {self.host}:{self.port}")
            connection = self._connection_factory(
                host=self.host,
                port=self.port,
                username=self.username,
                database=self.database,
            )
            with self._state_lock:
                self._connections.add(connection)
                self._active += 1
            logger.info("Hive 连接成功")
            yield connection
        finally:
            if connection is not None:
                with self._state_lock:
                    self._connections.discard(connection)
                    self._active = max(0, self._active - 1)
                try:
                    connection.close()
                    logger.info("Hive 查询连接已关闭")
                except Exception as exc:
                    logger.warning(f"关闭 Hive 查询连接时出错: {exc}")
            self._semaphore.release()

    connection = get_connection

    def get_status(self) -> dict:
        """Return resource state for readiness checks."""

        with self._state_lock:
            active = self._active
            closed = self._closed
        return {
            "healthy": not closed,
            "closed": closed,
            "max_concurrent": self.max_concurrent,
            "active": active,
        }

    def close(self) -> None:
        """Prevent new queries and close active connections during shutdown."""

        with self._state_lock:
            self._closed = True
            connections = list(self._connections)

        for connection in connections:
            try:
                connection.close()
            except Exception as exc:
                logger.warning(f"关闭 Hive 连接时出错: {exc}")


hive_pool = HiveConnectionPool()
