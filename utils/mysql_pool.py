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

    def _build_db_to_node_mapping(self) -> Dict[str, Dict[str, str]]:
        """
        构建数据库到节点的映射关系

        Returns:
            (
                {"nodeA_whjcbb": {"node": nodeA, "database": "whjcbb", "description":"决策报表库"}, ...},
            )
        """
        mapping = {}
        for node in self.nodes_config:
            node_name = node["name"]
            databases = node.get("databases", [])
            for db in databases:
                # 兼容旧配置（字符串格式）和新配置（对象格式）
                if isinstance(db, str):
                    db_name = db
                    db_description = ""
                else:
                    db_name = db.get("name", "")
                    db_description = db.get("description", "")

                if not db_name:
                    continue

                # 生成唯一标识符：{node_name}_{database}
                unique_id = f"{node_name}_{db_name}"
                mapping[unique_id] = {"node": node_name, "database": db_name, "description": db_description}
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
                    cursorclass=pymysql.cursors.DictCursor,
                )
                self._pools[node_name] = pool
                logger.info(f"MySQL 连接池初始化成功: {node_name}")
            except Exception as e:
                logger.error(f"初始化连接池失败 {node_name}: {e}")

    def get_database_list(self) -> List[str]:
        """
        获取所有可用的数据库唯一标识符列表

        Returns:
            ["nodeA_whjcbb", "nodeB_whjcbb", ...]
        """
        # 转换为 CSV 格式（数据库标识符,描述）
        output = []
        output.append("数据库,描述")
        for k, v in self._db_to_node_map.items():
            description = v.get('description')
            # 处理描述中的逗号
            if description and "," in description:
                description = f'"{description}"'
            output.append(f"{k},{description}")

        return output


    def get_database_by_unique_id(self, unique_id: str) -> Optional[str]:
        """
        根据唯一标识符获取实际的数据库名

        Args:
            unique_id: 唯一标识符（如 nodeA_whjcbb）

        Returns:
            实际的数据库名，不存在返回None
        """
        mapping = self._db_to_node_map.get(unique_id)
        if mapping:
            return mapping["database"]
        return None

    def get_node_by_database(self, unique_id: str) -> Optional[Dict]:
        """
        根据唯一标识符获取对应的节点配置

        Args:
            unique_id: 唯一标识符（如 nodeA_whjcbb）

        Returns:
            节点配置字典，不存在返回None
        """
        # 尝试作为唯一标识符解析
        mapping = self._db_to_node_map.get(unique_id)
        if mapping:
            node_name = mapping["node"]
            for node in self.nodes_config:
                if node["name"] == node_name:
                    return node
            return None

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
    def get_connection(self, unique_id: str):
        """
        获取指定数据库的MySQL连接（上下文管理器方式）

        Args:
            unique_id: 唯一标识符（如 nodeA_whjcbb）

        Yields:
            MySQL连接对象

        Raises:
            ValueError: 数据库不存在
        """
        node = self.get_node_by_database(unique_id)
        if not node:
            raise ValueError(f"数据库不存在: {unique_id}")

        # 获取实际数据库名
        actual_db = self.get_database_by_unique_id(unique_id)
        if not actual_db:
            raise ValueError(f"无法解析数据库名: {unique_id}")

        pool = self._pools.get(node['name'])
        if not pool:
            raise ValueError(f"连接池不存在: {node['name']}")

        logger.info(f"从连接池获取连接: {node['name']}/{actual_db}")
        conn = pool.connection()

        try:
            # 使用 SQL 语句切换到指定数据库
            with conn.cursor() as cursor:
                cursor.execute(f"USE `{actual_db}`")
            yield conn
        finally:
            conn.close()  # 归还连接到池
            logger.info(f"连接已归还到池: {node['name']}/{actual_db}")

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