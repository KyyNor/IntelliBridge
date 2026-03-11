import re
import hashlib
from typing import Optional

import traceback
import sqlglot
from fastapi import APIRouter
from pydantic import BaseModel

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.cache import cache
from utils.decorators import log_function_info
from utils.mcp import mcp

# 创建路由
router = APIRouter(prefix="/api/mysql", tags=["MySQL"])

# 请求模型
class SearchTablesRequest(BaseModel):
    database: str
    keyword: Optional[str] = ""


class DescribeRequest(BaseModel):
    database: str
    table_name: str


class QueryRequest(BaseModel):
    database: str
    sql: str
    limit: Optional[int] = 10


class MySQLQuery:
    """MySQL 查询工具类"""

    # 最大返回行数
    MAX_LIMIT = 1000

    # 默认返回行数
    DEFAULT_LIMIT = 10

    def __init__(self):
        """初始化查询工具"""
        pass

    def list_databases(self) -> str:
        """
        列出所有可用的数据库

        Returns:
            逗号分隔的数据库名列表
        """
        try:
            cache_key = "mysql:databases:list"

            # 尝试从缓存获取
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info("从缓存获取数据库列表")
                return cached_result

            # 从连接池获取数据库列表
            databases = mysql_pool.get_database_list()

            if not databases:
                return "错误: 没有可用的数据库"

            result = ",".join(databases)

            # 缓存1小时
            cache.set(cache_key, result, expire=3600)

            logger.info(f"获取数据库列表成功: {len(databases)} 个数据库")
            return result

        except Exception as e:
            error_msg = f"获取数据库列表失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return error_msg

    def search_tables(self, database: str, keyword: str = "") -> str:
        """
        搜索数据库中的表（支持模糊匹配表名和表注释）

        Args:
            database: 数据库名
            keyword: 搜索关键字（可选）

        Returns:
            CSV 格式的表列表（表名,表注释）
        """
        try:
            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database)
            if not node:
                return f"错误: 数据库不存在: {database}"

            cache_key = f"mysql:tables:{database}:{keyword}"

            # 尝试从缓存获取
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取表列表: {database}")
                return cached_result

            with mysql_pool.get_connection(database) as conn:
                cursor = conn.cursor()

                # 构建查询SQL
                if keyword:
                    # 模糊匹配表名或表注释
                    sql = """
                        SELECT TABLE_NAME, TABLE_COMMENT
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s
                        AND (TABLE_NAME LIKE %s OR TABLE_COMMENT LIKE %s)
                        ORDER BY TABLE_NAME
                    """
                    like_pattern = f"%{keyword}%"
                    cursor.execute(sql, (database, like_pattern, like_pattern))
                else:
                    # 获取所有表
                    sql = """
                        SELECT TABLE_NAME, TABLE_COMMENT
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s
                        ORDER BY TABLE_NAME
                    """
                    cursor.execute(sql, (database,))

                results = cursor.fetchall()

            if not results:
                return f"数据库 {database} 中未找到表"

            # 转换为 CSV 格式
            output = []
            output.append("表名,表注释")
            for row in results:
                table_name = row['TABLE_NAME'] if row['TABLE_NAME'] else ""
                table_comment = row['TABLE_COMMENT'] if row.get('TABLE_COMMENT') else ""
                # 处理注释中的逗号
                if "," in table_comment:
                    table_comment = f'"{table_comment}"'
                output.append(f"{table_name},{table_comment}")

            result = "\n".join(output)

            # 缓存10分钟
            cache.set(cache_key, result, expire=600)

            logger.info(f"搜索表成功: {database}, 找到 {len(results)} 个表")
            return result

        except Exception as e:
            error_msg = f"搜索表失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return error_msg


    def describe_table(self, database: str, table_name: str) -> str:
        """
        查询表结构

        Args:
            database: 数据库名
            table_name: 表名

        Returns:
            CSV 格式的表结构数据
        """
        try:
            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database)
            if not node:
                return f"错误: 数据库不存在: {database}"

            cache_key = f"mysql:describe:{database}:{table_name}"

            # 尝试从缓存获取
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取表结构: {database}.{table_name}")
                return cached_result

            with mysql_pool.get_connection(database) as conn:
                cursor = conn.cursor()

                # 查询表结构
                sql = """
                    SELECT
                        COLUMN_NAME,
                        COLUMN_TYPE,
                        IS_NULLABLE,
                        COLUMN_KEY,
                        COLUMN_DEFAULT,
                        COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                """
                cursor.execute(sql, (database, table_name))
                results = cursor.fetchall()

            if not results:
                return f"错误: 表 {database}.{table_name} 不存在或无数据"

            # 转换为 CSV 格式
            output = []
            output.append("字段名,类型,是否可空,键,默认值,注释")
            for row in results:
                col_name = row['COLUMN_NAME'] if row['COLUMN_NAME'] else ""
                col_type = row['COLUMN_TYPE'] if row['COLUMN_TYPE'] else ""
                is_nullable = row['IS_NULLABLE'] if row['IS_NULLABLE'] else ""
                col_key = row['COLUMN_KEY'] if row.get('COLUMN_KEY') else ""
                col_default = str(row['COLUMN_DEFAULT']) if row.get('COLUMN_DEFAULT') is not None else ""
                col_comment = row['COLUMN_COMMENT'] if row.get('COLUMN_COMMENT') else ""

                # 处理包含逗号的字段
                if "," in col_comment:
                    col_comment = f'"{col_comment}"'
                if "," in col_default:
                    col_default = f'"{col_default}"'

                output.append(f"{col_name},{col_type},{is_nullable},{col_key},{col_default},{col_comment}")

            result = "\n".join(output)

            # 缓存30分钟
            cache.set(cache_key, result, expire=1800)

            logger.info(f"查询表结构成功: {database}.{table_name}")
            return result

        except Exception as e:
            error_msg = f"查询表结构失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return error_msg

    def query_data(self, database: str, sql: str, limit: int = 10) -> str:
        """
        执行查询SQL

        Args:
            database: 数据库名
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

            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database)
            if not node:
                return f"错误: 数据库不存在: {database}"

            # 判断是否为 SELECT 语句
            if not re.match(r"^\s*SELECT\s", sql, re.IGNORECASE):
                return "错误: 只允许执行 SELECT 查询"

            # 限制返回条数
            limit = max(1, min(limit, self.MAX_LIMIT))

            # 标准化 SQL
            normalized_sql = self._normalize_sql(sql)

            # 添加 LIMIT 限制
            final_sql = normalized_sql
            if not re.search(r"\bLIMIT\s+\d+", normalized_sql, re.IGNORECASE):
                final_sql = f"{normalized_sql} LIMIT {limit}"

            # 计算 SQL 哈希
            sql_hash = self._get_sql_hash(final_sql)
            cache_key = f"mysql:query:{database}:{sql_hash}"

            # 尝试从缓存获取结果
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取查询结果: {sql_hash[:8]}...")
                return cached_result

            # 执行查询
            with mysql_pool.get_connection(database) as conn:
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
                    for item in row.values():  # 字典需要用 .values() 获取值
                        if item is None:
                            row_str.append("")
                        elif isinstance(item, str) and ("," in item or "\n" in item):
                            row_str.append(f'"{item}"')
                        else:
                            row_str.append(str(item))
                    output.append(",".join(row_str))

                result = "\n".join(output)

            # 缓存5分钟
            cache.set(cache_key, result, expire=300)

            logger.info(f"查询成功，返回 {len(results)} 条数据")
            return result

        except Exception as e:
            error_msg = f"查询失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return error_msg

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
            formatted = sqlglot.parse_one(sql, dialect='mysql').sql(dialect='mysql')
            return formatted
        except Exception as e:
            logger.warning(f"SQL 格式化失败，使用原始 SQL: {e}")
            return sql

    def _get_sql_hash(self, sql: str) -> str:
        """
        计算 SQL 的哈希值

        Args:
            sql: SQL 语句

        Returns:
            MD5 哈希值
        """
        return hashlib.md5(sql.encode('utf-8')).hexdigest()

# 默认实例
mysql_query = MySQLQuery()


# ==================== API 路由 ====================

@router.get("/databases")
async def list_databases():
    """列出所有可用的数据库"""
    result = mysql_query.list_databases()
    return {"data": result}


@router.post("/search_tables")
async def search_tables(request: SearchTablesRequest):
    """搜索数据库中的表"""
    result = mysql_query.search_tables(request.database, request.keyword)
    return {"data": result}


@router.post("/describe")
async def describe_table(request: DescribeRequest):
    """查询表结构"""
    result = mysql_query.describe_table(request.database, request.table_name)
    return {"data": result}


@router.post("/query")
async def query_mysql_data(request: QueryRequest):
    """执行查询SQL"""
    result = mysql_query.query_data(request.database, request.sql, request.limit)
    return {"data": result}


# ==================== MCP 工具 ====================

@mcp.tool()
@log_function_info
def mysql_list_databases() -> str:
    """
    列出所有可用的MySQL数据库

    返回所有可访问的数据库名列表（不显示节点信息）

    Returns:
        逗号分隔的数据库名列表
    """
    return mysql_query.list_databases()


@mcp.tool()
@log_function_info
def mysql_search_tables(database: str, keyword: str = "") -> str:
    """
    搜索MySQL数据库中的表

    Args:
        database: 数据库名
        keyword: 搜索关键字（可选），支持模糊匹配表名和表注释

    Returns:
        CSV 格式的表列表（表名,表注释）
    """
    return mysql_query.search_tables(database, keyword)


@mcp.tool()
@log_function_info
def mysql_describe(database: str, table_name: str) -> str:
    """
    查询MySQL表结构

    Args:
        database: 数据库名
        table_name: 表名

    Returns:
        CSV 格式的表结构数据（字段名,类型,是否可空,键,默认值,注释）
    """
    return mysql_query.describe_table(database, table_name)


@mcp.tool()
@log_function_info
def mysql_query_tool(database: str, sql: str, limit: int = 10) -> str:
    """
    执行MySQL查询SQL

    Args:
        database: 数据库名
        sql: 查询 SQL 语句（仅允许 SELECT）
        limit: 返回数据条数，默认10，最多1000

    Returns:
        CSV 格式的查询结果
    """
    return mysql_query.query_data(database, sql, limit)
