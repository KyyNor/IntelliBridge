"""Create synthetic Hive tables and collect real PyHive error samples."""

import json
import os
import time
from pathlib import Path

from pyhive import hive


HOST = os.environ.get("HIVE_HOST", "localhost")
PORT = int(os.environ.get("HIVE_PORT", "10000"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "/output/hive-error-samples.json"))


ERROR_CASES = [
    {"name": "missing_column", "sql": "SELECT DATA_DT FROM demo.orders"},
    {"name": "missing_table", "sql": "SELECT * FROM demo.not_exists"},
    {"name": "missing_database", "sql": "SELECT * FROM missing_db.orders"},
    {"name": "unknown_alias", "sql": "SELECT o.DATA_DT FROM demo.orders x"},
    {"name": "syntax_error", "sql": "SELECT FROM demo.orders"},
    {
        "name": "nested_missing_column",
        "sql": (
            "WITH t AS (SELECT order_id FROM demo.orders) "
            "SELECT DATA_DT FROM t"
        ),
    },
]


def connect_with_retry():
    last_error = None
    for _ in range(60):
        try:
            return hive.Connection(
                host=HOST,
                port=PORT,
                username="hive",
                database="default",
            )
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"连接 Spark Thrift Server 失败: {last_error}") from last_error


def run_sql(cursor, sql: str) -> None:
    cursor.execute(sql)
    try:
        cursor.fetchall()
    except Exception:
        pass


def serialize_exception(exc: Exception) -> dict:
    cause = getattr(exc, "__cause__", None)
    context = getattr(exc, "__context__", None)
    return {
        "exception_type": f"{type(exc).__module__}.{type(exc).__name__}",
        "message": str(exc),
        "args_repr": repr(getattr(exc, "args", None)),
        "cause_type": (
            f"{type(cause).__module__}.{type(cause).__name__}"
            if cause is not None
            else None
        ),
        "cause_message": str(cause) if cause is not None else None,
        "context_type": (
            f"{type(context).__module__}.{type(context).__name__}"
            if context is not None
            else None
        ),
        "context_message": str(context) if context is not None else None,
    }


def main() -> None:
    connection = connect_with_retry()
    try:
        cursor = connection.cursor()
        try:
            run_sql(cursor, "CREATE DATABASE IF NOT EXISTS demo")
            run_sql(cursor, "DROP TABLE IF EXISTS demo.orders")
            run_sql(
                cursor,
                """
                CREATE TABLE demo.orders (
                    order_id BIGINT,
                    acct_index STRING,
                    created_at STRING
                ) USING parquet
                """,
            )
            run_sql(
                cursor,
                """
                INSERT INTO demo.orders VALUES
                    (1, 'acct-a', '2026-07-15'),
                    (2, 'acct-b', '2026-07-15')
                """,
            )
        finally:
            cursor.close()

        samples = []
        for case in ERROR_CASES:
            cursor = connection.cursor()
            try:
                cursor.execute(case["sql"])
                cursor.fetchall()
                samples.append({**case, "succeeded": True, "error": None})
            except Exception as exc:
                samples.append({
                    **case,
                    "succeeded": False,
                    "error": serialize_exception(exc),
                })
            finally:
                cursor.close()

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(
            json.dumps(samples, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(samples, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
