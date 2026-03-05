from pyhive import hive
import re
import hashlib
import sqlglot
from typing import Optional, List, Tuple
from utils.hive_pool import hive_pool
from utils.logger import logger
from utils.cache import cache


class HiveQuery:
    """Hive 查询工具类"""

    # 允许的数据库名（不需要过滤条件的数据库）
    ALLOWED_DB = "hxb_dh_data_dim"

    # 必须包含的过滤条件字段
    REQUIRED_FILTER_FIELDS = ["etl_data", "cdate"]

    def __init__(self):
        """初始化查询工具"""
        self.conn = None

    def _get_connection(self) -> hive.Connection:
        """获取 Hive 连接"""
        if self.conn is None or self.conn.closed:
            self.conn = hive_pool.get_connection()
        return self.conn

    def _normalize_sql(self, sql: str) -> str:
        """
        标准化 SQL：转大写并格式化

        Args:
            sql: 原始 SQL 语句

        Returns:
            标准化后的 SQL 语句
        """
        try:
            # 使用 sqlglot 格式化 SQL
            formatted = sqlglot.parse_one(sql, dialect='hive').sql(dialect='hive')
            # 转大写
            normalized = formatted.upper()
            return normalized
        except Exception as e:
            logger.warning(f"SQL 格式化失败，使用原始 SQL: {e}")
            return sql.upper()

    def _get_sql_hash(self, sql: str) -> str:
        """
        计算 SQL 的哈希值

        Args:
            sql: SQL 语句

        Returns:
            MD5 哈希值
        """
        return hashlib.md5(sql.encode('utf-8')).hexdigest()

    def describe_table(self, table_full_name: str) -> str:
        """
        查看表结构

        Args:
            table_full_name: 表名，格式为 "库名.表名"

        Returns:
            CSV 格式的表结构数据
        """
        try:
            # 解析库名和表名
            if "." not in table_full_name:
                return "错误: 表名格式不正确，应为 '库名.表名'"

            database, table = table_full_name.strip().split(".", 1)

            conn = self._get_connection()
            cursor = conn.cursor()

            # 切换数据库
            cursor.execute(f"USE {database}")

            # 查看表结构
            cursor.execute(f"DESCRIBE {table}")
            results = cursor.fetchall()

            if not results:
                return f"错误: 表 {table_full_name} 不存在或无数据"

            # 转换为 CSV 格式
            output = []
            output.append("列名,数据类型,注释")
            for row in results:
                col_name = row[0] if row[0] else ""
                data_type = row[1] if row[1] else ""
                comment = row[2] if len(row) > 2 and row[2] else ""
                output.append(f"{col_name},{data_type},{comment}")

            logger.info(f"查询表结构成功: {table_full_name}")
            return "\n".join(output)

        except Exception as e:
            error_msg = f"查询表结构失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def query_data(self, sql: str, limit: int = 10) -> str:
        """
        查询数据

        Args:
            sql: 查询 SQL 语句
            limit: 返回数据条数，默认10，最多1000

        Returns:
            CSV 格式的查询结果
        """
        try:
            # 参数校验
            sql = sql.strip()
            if not sql:
                return "错误: SQL 语句不能为空"

            # 判断是否为 SELECT 语句
            if not re.match(r"^\s*SELECT\s", sql, re.IGNORECASE):
                return "错误: 只允许执行 SELECT 查询"

            # 限制返回条数
            limit = max(1, min(limit, 1000))

            # 标准化 SQL（转大写并格式化）
            normalized_sql = self._normalize_sql(sql)

            # 检查表名和过滤条件（使用原始 SQL）
            check_result = self._check_sql_filter(sql)
            if check_result != "ok":
                return check_result

            # 添加 LIMIT 限制到标准化 SQL
            final_sql = normalized_sql
            if not re.search(r"\bLIMIT\s+\d+", normalized_sql, re.IGNORECASE):
                final_sql = f"{normalized_sql} LIMIT {limit}"

            # 计算 SQL 哈希
            sql_hash = self._get_sql_hash(final_sql)
            cache_key = f"hive_query:{sql_hash}"

            # 尝试从缓存获取结果
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取查询结果: {sql_hash[:8]}...")
                return cached_result

            # 执行查询
            conn = self._get_connection()
            cursor = conn.cursor()

            logger.info(f"执行查询: {final_sql}")
            cursor.execute(final_sql)

            # 获取结果
            results = cursor.fetchall()
            if not results:
                return "查询结果为空"

            # 获取列名
            columns = [desc[0] for desc in cursor.description]

            # 转换为 CSV 格式
            output = []
            output.append(",".join(columns))
            for row in results:
                # 处理 None 值和包含逗号的字段
                row_str = []
                for item in row:
                    if item is None:
                        row_str.append("")
                    elif isinstance(item, str) and ("," in item or "\n" in item):
                        # 如果包含逗号或换行，用引号包裹
                        row_str.append(f'"{item}"')
                    else:
                        row_str.append(str(item))
                output.append(",".join(row_str))

            result = "\n".join(output)

            # 缓存结果（1小时过期）
            cache.set(cache_key, result, expire=3600)

            logger.info(f"查询成功，返回 {len(results)} 条数据")
            return result

        except Exception as e:
            error_msg = f"查询失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def _check_sql_filter(self, sql: str) -> str:
        """
        检查 SQL 是否包含必要的过滤条件

        Args:
            sql: SQL 语句

        Returns:
            "ok" 表示通过，否则返回错误信息
        """
        # 提取表名（简单正则，不处理复杂子查询）
        table_match = re.search(r"FROM\s+([^\s(]+)", sql, re.IGNORECASE)
        if not table_match:
            return "ok"  # 无法解析表名时放行

        table_full_name = table_match.group(1)

        # 检查是否为允许的数据库
        if table_full_name.startswith(f"{self.ALLOWED_DB}.") or table_full_name.startswith(f"{self.ALLOWED_DB}\\."):
            # 允许的数据库，不需要过滤条件
            return "ok"

        # 其他数据库，必须带过滤条件
        sql_lower = sql.lower()

        # 检查是否包含必需的过滤字段
        has_filter = False
        for field in self.REQUIRED_FILTER_FIELDS:
            # 检查 WHERE field= 或 WHERE field LIKE 等
            if re.search(rf"\bwhere\b.*\b{field}\s*(=|!=|<>|like|in)", sql_lower, re.IGNORECASE):
                has_filter = True
                break

        if not has_filter:
            return f"错误: 查询 {table_full_name} 表必须包含 WHERE {self.REQUIRED_FILTER_FIELDS[0]}=xx 或 WHERE {self.REQUIRED_FILTER_FIELDS[1]}=xx 过滤条件"

        return "ok"

    def close(self):
        """关闭连接"""
        if self.conn and not self.conn.closed:
            self.conn.close()
            logger.info("查询连接已关闭")


# 默认实例
hive_query = HiveQuery()
