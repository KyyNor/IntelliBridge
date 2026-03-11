from contextlib import contextmanager
from typing import Optional, Dict, List

import pymysql
from dbutils.pooled_db import PooledDB

from utils.logger import logger
from utils.config import config


class MySQLConnectionPool:
    """MySQL 连接池（按节点管理连接池）"""

    def __init__(self):
        """初始化连接池配置"""
        self.nodes_config = self._load_nodes_config()
        self._pools: Dict[str, PooledDB] = {}
        self._db_to_node_map = self._build_db_to_node_mapping()
        self._init_pools()

    def _load_nodes_config(self) -> list:
        """从配置文件加载节点配置"""
        return config.get("mysql.nodes", [])

    def _build_db_to_node_mapping(self) -> Dict[str, str]:
        """
        构建数据库到节点的映射关系

        Returns:
            {"database_x": "node_a", "database_y": "node_a", ...}
        """
        mapping = {}
        for node in self.nodes_config:
            node_name = node["name"]
            for db_name in node.get("databases", []):
                mapping[db_name] = node_name
        return mapping

    def _init_pools(self):
        """初始化所有节点的连接池"""
        for node in self.nodes_config:
            node_name = node["name"]
            try:
                pool = PooledDB(
                    creator=pymysql,
                    maxconnections=int(node.get('max_connections', 10)),
                    mincached=int(node.get('min_cached', 0)),
                    maxcached=int(node.get('max_cached', 5)),
                    maxshared=int(node.get('max_shared', 0)),
                    blocking=True,
                    maxusage=int(node.get('max_usage', 0)),
                    setsession=[],
                    ping=1,
                    host=node['host'],
                    port=node['port'],
                    user=node['username'],
                    password=node['password'],
                    charset=node.get('charset', 'utf8mb4'),
                )
                self._pools[node_name] = pool
                logger.info(f"MySQL 连接池初始化成功: {node_name}")
            except Exception as e:
                logger.error(f"初始化连接池失败 {node_name}: {e}")

    def get_database_list(self) -> List[str]:
        """
        获取所有可用的数据库列表

        Returns:
            ["database_x", "database_y", "database_z", ...]
        """
        databases = []
        for node in self.nodes_config:
            databases.extend(node.get("databases", []))
        return sorted(databases)

    def get_node_by_database(self, database: str) -> Optional[Dict]:
        """
        根据数据库名获取对应的节点配置

        Args:
            database: 数据库名

        Returns:
            节点配置字典，不存在返回None
        """
        node_name = self._db_to_node_map.get(database)
        if not node_name:
            return None

        for node in self.nodes_config:
            if node["name"] == node_name:
                return node
        return None

    def get_pool(self, database: str) -> PooledDB:
        """
        获取指定数据库对应的连接池

        Args:
            database: 数据库名

        Returns:
            PooledDB 连接池对象

        Raises:
            ValueError: 数据库不存在
        """
        node = self.get_node_by_database(database)
        if not node:
            raise ValueError(f"数据库不存在: {database}")

        pool = self._pools.get(node['name'])
        if not pool:
            raise ValueError(f"连接池不存在: {node['name']}")

        return pool

    @contextmanager
    def get_connection(self, database: str):
        """
        获取指定数据库的MySQL连接（上下文管理器方式）

        Args:
            database: 数据库名

        Yields:
            MySQL连接对象

        Raises:
            ValueError: 数据库不存在
        """
        node = self.get_node_by_database(database)
        if not node:
            raise ValueError(f"数据库不存在: {database}")

        pool = self._pools.get(node['name'])
        if not pool:
            raise ValueError(f"连接池不存在: {node['name']}")

        logger.info(f"从连接池获取连接: {node['name']}/{database}")
        conn = pool.connection()

        try:
            yield conn
        finally:
            conn.close()  # 归还连接到池
            logger.info(f"连接已归还到池: {node['name']}/{database}")

    def close_pool(self, database: str):
        """关闭指定数据库的连接池"""
        node = self.get_node_by_database(database)
        if not node:
            return

        pool = self._pools.get(node['name'])
        if pool:
            pool.close()
            logger.info(f"MySQL 连接池已关闭: {node['name']}")

    def close_all_pools(self):
        """关闭所有连接池"""
        for name, pool in self._pools.items():
            try:
                pool.close()
                logger.info(f"MySQL 连接池已关闭: {name}")
            except Exception as e:
                logger.warning(f"关闭连接池 {name} 时出错: {e}")
        self._pools.clear()
        logger.info("所有 MySQL 连接池已关闭")


# 默认连接池实例
mysql_pool = MySQLConnectionPool()