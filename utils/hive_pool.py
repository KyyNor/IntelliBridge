from pyhive import hive
from typing import Optional
from utils.logger import logger
from utils.config import config


class HiveConnectionPool:
    """Hive 连接池"""

    def __init__(self):
        """初始化连接池配置"""
        self.host = config.get("hive.host", "localhost")
        self.port = config.get("hive.port", 10000)
        self.username = config.get("hive.username", "default")
        self.database = config.get("hive.database", "default")
        self._connection: Optional[hive.Connection] = None

    def get_connection(self) -> hive.Connection:
        """
        获取 Hive 连接（单例模式）

        Returns:
            Hive 连接对象
        """
        if self._connection is None or self._connection.closed:
            try:
                logger.info(f"正在连接 Hive: {self.host}:{self.port}")
                self._connection = hive.Connection(
                    host=self.host,
                    port=self.port,
                    username=self.username,
                    database=self.database
                )
                logger.info("Hive 连接成功")
            except Exception as e:
                logger.error(f"Hive 连接失败: {e}")
                raise

        return self._connection

    def close(self):
        """关闭连接"""
        if self._connection and not self._connection.closed:
            self._connection.close()
            logger.info("Hive 连接已关闭")


# 默认连接池实例
hive_pool = HiveConnectionPool()
