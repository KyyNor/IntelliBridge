import time
from threading import Lock
from pyhive import hive
from typing import Optional
from utils.logger import logger
from utils.config import config


class HiveConnectionPool:
    """Hive 连接池"""

    # 重连间隔（秒），默认1小时
    RECONNECT_INTERVAL = 3600

    def __init__(self):
        """初始化连接池配置"""
        self.host = config.get("hive.host", "localhost")
        self.port = config.get("hive.port", 10000)
        self.username = config.get("hive.username", "default")
        self.database = config.get("hive.database", "default")
        self._connection: Optional[hive.Connection] = None
        self._connected_time: Optional[float] = None  # 连接创建时间戳
        self._lock = Lock()

    def _should_reconnect(self) -> bool:
        """判断是否需要重连"""
        if self._connection is None:
            return True
        if self._connected_time is None:
            return True
        # 超过重连间隔则重连
        return (time.time() - self._connected_time) > self.RECONNECT_INTERVAL

    def _force_close(self):
        """强制关闭连接"""
        if self._connection:
            try:
                self._connection.close()
                logger.info("Hive 连接已关闭（定时重连）")
            except Exception as e:
                logger.warning(f"关闭连接时出错: {e}")
            finally:
                self._connection = None
                self._connected_time = None

    def get_connection(self) -> hive.Connection:
        """
        获取 Hive 连接（单例模式，支持定时重连）

        Returns:
            Hive 连接对象
        """
        with self._lock:
            # 检查是否需要重连（首次连接或超过重连间隔）
            if self._should_reconnect():
                self._force_close()

            if self._connection is None:
                try:
                    logger.info(f"正在连接 Hive: {self.host}:{self.port}")
                    self._connection = hive.Connection(
                        host=self.host,
                        port=self.port,
                        username=self.username,
                        database=self.database
                    )
                    self._connected_time = time.time()
                    logger.info("Hive 连接成功")
                except Exception as e:
                    logger.error(f"Hive 连接失败: {e}")
                    raise

            return self._connection

    def close(self):
        """关闭连接"""
        if self._connection:
            try:
                self._connection.close()
                logger.info("Hive 连接已关闭")
            except Exception as e:
                logger.warning(f"关闭连接时出错: {e}")
            finally:
                self._connection = None


# 默认连接池实例
hive_pool = HiveConnectionPool()
