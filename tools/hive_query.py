from pyhive import hive
import pymysql
import re
import hashlib
import sqlglot
import json
import yaml
from pathlib import Path
from typing import Optional, List, Tuple
from fastapi import APIRouter
from pydantic import BaseModel

from utils.hive_pool import hive_pool
from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.cache import cache
from utils.decorators import log_function_info
from utils.mcp import mcp
from utils.sql_utils import remove_comments

# 创建路由
router = APIRouter(prefix="/api/hive", tags=["Hive"])

# 请求模型
class DescribeRequest(BaseModel):
    table_name: str


class QueryRequest(BaseModel):
    sql: str
    limit: Optional[int] = 10


class ListDatabaseRequest(BaseModel):
    blacklist: Optional[List[str]] = []


class ListTableRequest(BaseModel):
    database: str
    table_name: Optional[str] = ""
    page: int = 1
    page_size: int = 50


class HiveQuery:
    """Hive 查询工具类"""

    # 允许的数据库名（不需要过滤条件的数据库）
    ALLOWED_DB = "hxb_dh_data_dim"

    # 必须包含的过滤条件字段
    REQUIRED_FILTER_FIELDS = ["etl_date", "cdate"]

    def __init__(self):
        """初始化查询工具"""
        # 加载数据库黑名单
        self._database_blacklist = self._load_blacklist()

    def _load_blacklist(self) -> List[str]:
        """从配置文件加载黑名单"""
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    cfg = yaml.safe_load(f)
                    return cfg.get('hive', {}).get('database_blacklist', [])
            except Exception as e:
                logger.warning(f"加载黑名单配置失败: {e}")
        return []

    def _get_meta_connection(self):
        """获取 Hive 元数据库连接"""
        return mysql_pool.get_connection("meta_hive_metastore_hive_db")

    def _get_connection(self) -> hive.Connection:
        """获取 Hive 连接（每次从连接池获取，支持自动重连）"""
        return hive_pool.get_connection()

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
            formatted = sqlglot.parse_one(sql_no_comment, dialect='hive').sql(dialect='hive')
            # 转大写
            normalized = formatted.upper()
            return normalized
        except Exception as e:
            logger.warning(f"SQL 格式化失败，使用原始 SQL: {e}")
            return sql_no_comment.upper()

    def _check_sql_type(self, sql: str) -> str:
        """
        检查 SQL 类型是否允许执行

        Args:
            sql: SQL 语句（已移除注释）

        Returns:
            "ok" 表示通过，否则返回错误信息
        """
        sql_stripped = sql.strip()

        # 检查是否包含禁止的关键字（INSERT、DELETE、DROP）
        # 忽略 WITH 子句中的 CTAS
        forbidden_keywords = ['DELETE', 'DROP']

        # 对于非 WITH 开头的语句，检查是否有 INSERT（CTAS 在下面单独处理）
        if not sql_stripped.upper().startswith('WITH'):
            if re.search(r'\bINSERT\b', sql_stripped, re.IGNORECASE):
                return "错误: 不允许执行 INSERT 操作"

        for keyword in forbidden_keywords:
            if re.search(rf'\b{keyword}\b', sql_stripped, re.IGNORECASE):
                return f"错误: 不允许执行 {keyword} 操作"

        # 检查是否以允许的关键词开头（SELECT、WITH、REFRESH）
        # 注意：WITH 后面可能跟着 INSERT（如 WITH temp AS (...) INSERT...），这种情况是允许的 CTE + DML
        if not re.match(r'^\s*(SELECT|WITH|REFRESH)\s', sql_stripped, re.IGNORECASE):
            return "错误: 只允许执行 SELECT、WITH、REFRESH 查询"

        return "ok"

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
            try:
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
            finally:
                cursor.close()

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
            original_sql = sql.strip()
            if not original_sql:
                return "错误: SQL 语句不能为空"

            # 移除注释（用于检查SQL类型）
            sql_no_comment = remove_comments(original_sql)

            # 检查 SQL 类型（是否允许执行）
            type_check_result = self._check_sql_type(sql_no_comment)
            if type_check_result != "ok":
                return type_check_result

            # 限制返回条数
            limit = max(1, min(limit, 1000))

            # 标准化 SQL（转大写并格式化，会自动移除注释）
            normalized_sql = self._normalize_sql(original_sql)

            # 检查表名和过滤条件（使用移除注释后的 SQL）
            filter_check_result = self._check_sql_filter(sql_no_comment)
            if filter_check_result != "ok":
                return filter_check_result

            # 添加 LIMIT 限制到标准化 SQL
            final_sql = normalized_sql
            if not re.search(r"\bLIMIT\s+\d+", normalized_sql, re.IGNORECASE):
                final_sql = f"{normalized_sql} LIMIT {limit}"

            # 执行查询
            conn = self._get_connection()
            cursor = conn.cursor()
            try:
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

                logger.info(f"查询成功，返回 {len(results)} 条数据")
                return result
            finally:
                cursor.close()

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

    def list_databases(self, extra_blacklist: Optional[List[str]] = None) -> str:
        """
        列出所有 Hive 数据库及表数量

        Args:
            extra_blacklist: 额外的黑名单列表

        Returns:
            CSV 格式：database_name,table_num
        """
        # 合并黑名单：配置文件的 + 传入的
        blacklist = set(self._database_blacklist)
        if extra_blacklist:
            blacklist.update(extra_blacklist)

        try:
            with self._get_meta_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                sql = """
                SELECT d.NAME as db_name, COUNT(t.TBL_ID) as table_num
                FROM DBS d
                LEFT JOIN TBLS t ON d.DB_ID = t.DB_ID
                GROUP BY d.DB_ID, d.NAME
                ORDER BY d.NAME
                """
                cursor.execute(sql)
                results = cursor.fetchall()

                # 过滤黑名单
                filtered_results = [
                    r for r in results
                    if r['db_name'] not in blacklist
                ]

                # 转换为 CSV
                output = ["database_name,table_num"]
                for row in filtered_results:
                    output.append(f"{row['db_name']},{row['table_num']}")

                logger.info(f"列出数据库成功，共 {len(filtered_results)} 个")
                return "\n".join(output)
        except Exception as e:
            error_msg = f"列出数据库失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def list_tables(self, database: str, table_name: str = "", page: int = 1, page_size: int = 50) -> str:
        """
        列出指定数据库的表

        Args:
            database: 数据库名
            table_name: 模糊搜索的表名
            page: 页码，从1开始
            page_size: 每页数量，最大50

        Returns:
            JSON 格式，含分页信息和表列表
        """
        # 限制 page_size 最大为 50
        page_size = min(page_size, 50)
        offset = (page - 1) * page_size

        try:
            with self._get_meta_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                # 先获取总数
                count_sql = """
                SELECT COUNT(*) as cnt
                FROM TBLS t
                LEFT JOIN TABLE_PARAMS tp ON t.TBL_ID = tp.TBL_ID AND tp.PARAM_KEY = 'comment'
                WHERE t.DB_ID = (SELECT DB_ID FROM DBS WHERE NAME = %s)
                """
                if table_name:
                    count_sql += " AND (LOWER(t.TBL_NAME) LIKE CONCAT('%%', LOWER(%s), '%%') OR LOWER(COALESCE(tp.PARAM_VALUE, '')) LIKE CONCAT('%%', LOWER(%s), '%%'))"
                    cursor.execute(count_sql, (database, table_name, table_name))
                else:
                    cursor.execute(count_sql, (database,))

                total = cursor.fetchone()['cnt']

                # 再获取分页数据
                data_sql = """
                SELECT t.TBL_NAME, COALESCE(tp.PARAM_VALUE, '') as table_comment
                FROM TBLS t
                LEFT JOIN TABLE_PARAMS tp ON t.TBL_ID = tp.TBL_ID AND tp.PARAM_KEY = 'comment'
                WHERE t.DB_ID = (SELECT DB_ID FROM DBS WHERE NAME = %s)
                """
                if table_name:
                    data_sql += " AND (LOWER(t.TBL_NAME) LIKE CONCAT('%%', LOWER(%s), '%%') OR LOWER(COALESCE(tp.PARAM_VALUE, '')) LIKE CONCAT('%%', LOWER(%s), '%%'))"
                    data_sql += " ORDER BY t.TBL_NAME LIMIT %s OFFSET %s"
                    cursor.execute(data_sql, (database, table_name, table_name, page_size, offset))
                else:
                    data_sql += " ORDER BY t.TBL_NAME LIMIT %s OFFSET %s"
                    cursor.execute(data_sql, (database, page_size, offset))

                tables = cursor.fetchall()

                # 构建返回结果
                result = {
                    "database": database,
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "tables": [
                        {"table_name": t["TBL_NAME"], "table_comment": t["table_comment"]}
                        for t in tables
                    ]
                }

                logger.info(f"列出表成功: {database}, 共 {total} 个表")
                return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            error_msg = f"列出表失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def close(self):
        """关闭连接（委托给连接池）"""
        try:
            hive_pool.close()
            logger.info("查询连接已关闭")
        except Exception as e:
            logger.warning(f"关闭查询连接时出错: {e}")


# 默认实例
hive_query = HiveQuery()


# API 路由
@router.post("/describe")
async def describe_table(request: DescribeRequest):
    """查看 Hive 表结构"""
    result = hive_query.describe_table(request.table_name)
    return {"data": result}


@router.post("/query")
async def query_hive_data(request: QueryRequest):
    """查询 Hive 数据"""
    result = hive_query.query_data(request.sql, request.limit)
    return {"data": result}


@router.post("/list-databases")
async def list_databases(request: ListDatabaseRequest):
    """列出所有 Hive 数据库及表数量"""
    result = hive_query.list_databases(request.blacklist)
    return {"data": result}


@router.post("/list-tables")
async def list_tables(request: ListTableRequest):
    """列出指定数据库的表"""
    result = hive_query.list_tables(
        request.database,
        request.table_name,
        request.page,
        request.page_size
    )
    return {"data": result}


# MCP 工具
@mcp.tool()
@log_function_info
def hive_describe(table_name: str) -> str:
    """
    查看 Hive 表结构

    Args:
        table_name: 表名，格式为 "库名.表名"

    Returns:
        CSV 格式的表结构数据
    """
    return hive_query.describe_table(table_name)


@mcp.tool()
@log_function_info
def hive_query_tool(sql: str, limit: int = 10) -> str:
    """
    查询 Hive 数据，允许执行SELECT、WITH开头的查询语句，或REFRESH 开头的刷新语句

    Args:
        sql: 查询 SQL 语句
        limit: 返回数据条数，默认10，最多1000

    Returns:
        CSV 格式的查询结果
    """
    return hive_query.query_data(sql, limit)


@mcp.tool()
@log_function_info
def hive_list_databases() -> str:
    """
    列出所有 Hive 数据库及表数量，支持黑名单过滤

    Returns:
        CSV 格式：database_name,table_num
    """
    return hive_query.list_databases()


@mcp.tool()
@log_function_info
def hive_list_tables(database: str, table_name: str = "", page: int = 1, page_size: int = 50) -> str:
    """
    列出指定数据库的表，支持模糊搜索和分页

    Args:
        database: 数据库名称
        table_name: 模糊搜索的表名（忽略大小写）
        page: 页码，从1开始
        page_size: 每页数量，默认50，最大50

    Returns:
        JSON 格式，含分页信息和表列表
    """
    return hive_query.list_tables(database, table_name, page, page_size)
