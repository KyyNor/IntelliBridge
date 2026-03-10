import pymysql
from typing import Optional, Dict, List

from utils.logger import logger
from utils.config import config


class MySQLConnectionPool:
    """MySQL 连接池（按节点管理连接）"""

    def __init__(self):
        """初始化连接池配置"""
        self.nodes_config = self._load_nodes_config()
        self._connections: Dict[str, pymysql.Connection] = {}
        self._db_to_node_map = self._build_db_to_node_mapping()

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

    def get_connection(self, database: str) -> pymysql.Connection:
        """
        获取指定数据库的MySQL连接（单例模式）

        Args:
            database: 数据库名

        Returns:
            MySQL连接对象

        Raises:
            ValueError: 数据库不存在
        """
        node = self.get_node_by_database(database)
        if not node:
            raise ValueError(f"数据库不存在: {database}")

        connection_key = f"{node['name']}_{database}"

        if connection_key not in self._connections:
            try:
                logger.info(f"正在连接 MySQL: {node['host']}:{node['port']}/{database}")
                conn = pymysql.connect(
                    host=node['host'],
                    port=node['port'],
                    user=node['username'],
                    password=node['password'],
                    database=database,
                    charset=node.get('charset', 'utf8mb4'),
                    cursorclass=pymysql.cursors.DictCursor
                )
                self._connections[connection_key] = conn
                logger.info(f"MySQL 连接成功: {database}")
            except Exception as e:
                logger.error(f"MySQL 连接失败: {e}")
                raise

        return self._connections[connection_key]

    def close_database(self, database: str):
        """关闭指定数据库的连接"""
        node = self.get_node_by_database(database)
        if not node:
            return

        connection_key = f"{node['name']}_{database}"
        if connection_key in self._connections:
            try:
                self._connections[connection_key].close()
                logger.info(f"MySQL 连接已关闭: {database}")
            except Exception as e:
                logger.warning(f"关闭连接时出错: {e}")
            finally:
                del self._connections[connection_key]

    def close_all(self):
        """关闭所有连接"""
        for key, conn in self._connections.items():
            try:
                conn.close()
            except Exception as e:
                logger.warning(f"关闭连接 {key} 时出错: {e}")
        self._connections.clear()
        logger.info("所有 MySQL 连接已关闭")


# 默认连接池实例
mysql_pool = MySQLConnectionPool()
