import json
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
from utils.sql_utils import remove_comments, check_sql_type, enforce_limit

from fastmcp import FastMCP

mysql_mcp = FastMCP("IntelliBridge Mysql")


# 创建路由
router = APIRouter(prefix="/api/mysql", tags=["MySQL"])

# 请求模型
class SearchTablesRequest(BaseModel):
    database_id: str
    keyword: Optional[str] = ""


class DescribeRequest(BaseModel):
    database_id: str
    table_name: str


class QueryRequest(BaseModel):
    database_id: str
    sql: str
    limit: Optional[int] = 10


class MySQLQuery:
    """MySQL 查询工具类"""

    # 最大返回行数
    MAX_LIMIT = 10000

    # 默认返回行数
    DEFAULT_LIMIT = 10

    def __init__(self):
        """初始化查询工具"""
        pass

    def list_databases(self) -> dict:
        """
        列出所有可用的数据库

        Returns:
            Dict 格式的数据库列表
        """
        try:
            # 从连接池获取数据库列表
            databases = mysql_pool.get_database_list()

            if not databases:
                return {"error": "没有可用的数据库"}

            # 第一行是标题，跳过
            db_lines = databases[1:] if databases else []

            # 解析并返回 dict 格式
            db_list = []
            for line in db_lines:
                parts = line.split(",", 1)
                if len(parts) == 2:
                    db_list.append({"database": parts[0], "description": parts[1]})
                elif parts:
                    db_list.append({"database": parts[0], "description": ""})

            logger.info(f"获取数据库列表成功: {len(db_list)} 个数据库")
            return {"databases": db_list, "total": len(db_list)}

        except Exception as e:
            error_msg = f"获取数据库列表失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return {"error": error_msg}

    def search_tables(self, database_id: str, keyword: str = "") -> dict:
        """
        搜索数据库中的表（支持模糊匹配表名和表注释）

        Args:
            database_id: 数据库唯一标识符（如 nodeA_whjcbb）
            keyword: 搜索关键字（可选）

        Returns:
            Dict 格式的表列表
        """
        try:
            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database_id)
            if not node:
                return {"error": f"数据库不存在: {database_id}"}

            cache_key = f"mysql:tables:{database_id}:{keyword}"

            # 尝试从缓存获取
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取表列表: {database_id}")
                return cached_result

            with mysql_pool.get_connection(database_id) as conn:
                cursor = conn.cursor()

                # 获取实际数据库名用于 SQL 查询
                actual_db = mysql_pool.get_database_by_unique_id(database_id)

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
                    cursor.execute(sql, (actual_db, like_pattern, like_pattern))
                else:
                    # 获取所有表
                    sql = """
                        SELECT TABLE_NAME, TABLE_COMMENT
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s
                        ORDER BY TABLE_NAME
                    """
                    cursor.execute(sql, (actual_db,))

                results = cursor.fetchall()

            if not results:
                return {"error": f"数据库 {database_id} 中未找到表"}

            # 构造 dict 格式
            tables = []
            for row in results:
                tables.append({
                    "table_name": row['TABLE_NAME'] if row['TABLE_NAME'] else "",
                    "table_comment": row['TABLE_COMMENT'] if row.get('TABLE_COMMENT') else ""
                })

            # 缓存10分钟
            cache.set(cache_key, {"tables": tables, "total": len(tables)}, expire=600)

            logger.info(f"搜索表成功: {database_id}, 找到 {len(results)} 个表")
            return {"tables": tables, "total": len(tables)}

        except Exception as e:
            error_msg = f"搜索表失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return {"error": error_msg}


    def describe_table(self, database_id: str, table_name: str) -> dict:
        """
        查询表结构

        Args:
            database_id: 数据库唯一标识符（如 nodeA_whjcbb）
            table_name: 表名

        Returns:
            Dict 格式的表结构数据
        """
        try:
            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database_id)
            if not node:
                available_dbs = self.list_databases()
                return {"error": f"数据库不存在: {database_id}", "available_databases": available_dbs}

            cache_key = f"mysql:describe:{database_id}:{table_name}"

            # 尝试从缓存获取
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"从缓存获取表结构: {database_id}.{table_name}")
                return cached_result

            with mysql_pool.get_connection(database_id) as conn:
                cursor = conn.cursor()

                # 获取实际数据库名用于 SQL 查询
                actual_db = mysql_pool.get_database_by_unique_id(database_id)

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
                cursor.execute(sql, (actual_db, table_name))
                results = cursor.fetchall()

            if not results:
                return {"error": f"表 {database_id} ({actual_db}).{table_name} 不存在或无数据"}

            # 构造 dict 格式
            columns = []
            for row in results:
                columns.append({
                    "column_name": row['COLUMN_NAME'] if row['COLUMN_NAME'] else "",
                    "column_type": row['COLUMN_TYPE'] if row['COLUMN_TYPE'] else "",
                    "nullable": row['IS_NULLABLE'] if row['IS_NULLABLE'] else "",
                    "column_key": row['COLUMN_KEY'] if row.get('COLUMN_KEY') else "",
                    "column_default": str(row['COLUMN_DEFAULT']) if row.get('COLUMN_DEFAULT') is not None else "",
                    "column_comment": row['COLUMN_COMMENT'] if row.get('COLUMN_COMMENT') else ""
                })

            result = {"columns": columns, "total": len(columns)}

            # 缓存30分钟
            cache.set(cache_key, result, expire=1800)

            logger.info(f"查询表结构成功: {database_id}.{table_name}")
            return result

        except Exception as e:
            error_msg = f"查询表结构失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            available_dbs = self.list_databases()
            return {"error": error_msg, "available_databases": available_dbs}

    def query_data(self, database_id: str, sql: str, limit: int = 10) -> dict:
        """
        执行查询SQL

        Args:
            database_id: 数据库唯一标识符（如 nodeA_whjcbb）
            sql: 查询 SQL 语句
            limit: 返回数据条数，默认10，最多1000

        Returns:
            Dict 格式的查询结果
        """
        try:
            # 参数校验
            sql = sql.strip()
            if not sql:
                return {"error": "SQL 语句不能为空"}

            # 验证数据库是否存在
            node = mysql_pool.get_node_by_database(database_id)
            if not node:
                return {"error": f"数据库不存在: {database_id}"}

            # 移除注释并检查 SQL 类型（支持开头注释和 WITH 子句，排除 INSERT/DELETE/DROP）
            sql_no_comment = remove_comments(sql)
            type_check = check_sql_type(sql_no_comment, allowed_prefixes=['SELECT', 'WITH'], forbidden_keywords=['INSERT', 'DELETE', 'DROP'])
            if type_check != "ok":
                return {"error": type_check}

            # 限制返回条数
            limit = max(1, min(limit, self.MAX_LIMIT))

            # 标准化 SQL
            normalized_sql = self._normalize_sql(sql)

            # 强制限制最终 SQL，不能被调用方自带的大 LIMIT 绕过。
            final_sql = enforce_limit(normalized_sql, limit, self.MAX_LIMIT)

            # 执行查询
            with mysql_pool.get_connection(database_id) as conn:
                cursor = conn.cursor()

                logger.info(f"执行查询: {final_sql}")
                cursor.execute(final_sql)

                # 获取结果
                results = cursor.fetchall()
                if not results:
                    return {"error": "查询结果为空"}

                # 获取列名
                columns = [desc[0] for desc in cursor.description]

                # 构造 dict 格式
                rows = []
                for row in results:
                    row_dict = {}
                    for key in row.keys():
                        row_dict[key] = row[key]
                    rows.append(row_dict)

                logger.info(f"查询成功，返回 {len(results)} 条数据")
                return {"rows": rows, "columns": columns, "total": len(rows)}

        except Exception as e:
            error_msg = f"查询失败: {str(e)}"
            detail = traceback.format_exc()
            logger.error(f"{error_msg}\n{detail}")
            return {"error": error_msg}

    def _normalize_sql(self, sql: str) -> str:
        """
        标准化 SQL：移除注释、转大写并格式化

        Args:
            sql: 原始 SQL 语句

        Returns:
            标准化后的 SQL 语句
        """
        # 先移除注释
        sql_no_comment = remove_comments(sql)

        try:
            # 使用 sqlglot 格式化 SQL
            formatted = sqlglot.parse_one(sql_no_comment, dialect='mysql').sql(dialect='mysql')
            return formatted
        except Exception as e:
            logger.warning(f"SQL 格式化失败，使用原始 SQL: {e}")
            return sql_no_comment

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
def list_databases():
    """列出所有可用的数据库"""
    result = mysql_query.list_databases()
    return {"data": result}


@router.post("/search_tables")
def search_tables(request: SearchTablesRequest):
    """搜索数据库中的表"""
    result = mysql_query.search_tables(request.database_id, request.keyword)
    return {"data": result}


@router.post("/describe")
def describe_table(request: DescribeRequest):
    """查询表结构"""
    result = mysql_query.describe_table(request.database_id, request.table_name)
    return {"data": result}


@router.post("/query")
def query_mysql_data(request: QueryRequest):
    """执行查询SQL"""
    result = mysql_query.query_data(request.database_id, request.sql, request.limit)
    return {"data": result}


# ==================== MCP 工具 ====================

@mysql_mcp.tool(name="list_databases")
@log_function_info
def mysql_list_databases() -> dict:
    """列出所有可用的MySQL数据库"""
    return mysql_query.list_databases()


@mysql_mcp.tool(name="search_tables")
@log_function_info
def mysql_search_tables(database_id: str, keyword: str = "") -> dict:
    """搜索MySQL数据库中的表，支持模糊匹配表名和表注释

    Args:
        database_id: 数据库唯一标识符（如 nodeA_whjcbb）
        keyword: 搜索关键字（可选）

    Returns:
        Dict 格式的表列表，包含 tables 数组和 total 总数
    """
    return mysql_query.search_tables(database_id, keyword)


@mysql_mcp.tool(name="describe")
@log_function_info
def mysql_describe(database_id: str, table_name: str) -> dict:
    """查询MySQL表的字段结构和属性信息

    Args:
        database_id: 数据库唯一标识符（如 nodeA_whjcbb）
        table_name: 要查询的表名

    Returns:
        Dict 格式的表结构数据，包含 columns 数组（含字段名、类型、可空性、主键等）和 total 总数
    """
    return mysql_query.describe_table(database_id, table_name)


@mysql_mcp.tool(name="query")
@log_function_info
def mysql_query_tool(database_id: str, sql: str, limit: int = 10) -> dict:
    """执行MySQL SELECT查询语句并返回结果集

    Args:
        database_id: 数据库唯一标识符（如 nodeA_whjcbb）
        sql: SELECT 查询语句
        limit: 最大返回行数，默认10，上限1000

    Returns:
        Dict 格式的查询结果，包含 rows 数据行数组、columns 列名数组和 total 行数统计
    """
    return mysql_query.query_data(database_id, sql, limit)
