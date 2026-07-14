import time
import json
import threading
import functools
import uuid
from datetime import datetime
from typing import Callable, Any
from utils.logger import logger
from utils.mysql_pool import mysql_pool
from utils.call_log_writer import BoundedCallLogWriter


def _insert_func_call_log(record: dict) -> None:
    with mysql_pool.get_connection("mysql_121_data_factory") as conn:
        with conn.cursor() as cursor:
            cursor.execute(record["sql"], record["params"])
        conn.commit()


_call_log_writer = BoundedCallLogWriter(_insert_func_call_log, max_queue_size=1024)


def _save_func_call_log(request_id: str, func_name: str, module: str, params: dict,
                         started_at: datetime, elapsed: int, is_success: bool, error_msg: str = ""):
    """异步提交函数调用日志；队列满时丢弃，不能阻塞业务请求。"""
    params_json = json.dumps(params, ensure_ascii=False, default=str)
    started_at_str = started_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sql = """
        INSERT INTO llm_func_call_log
            (request_id, func_name, module, params, started_at, elapsed, is_success, error_msg)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    _call_log_writer.submit({
        "sql": sql,
        "params": (
            request_id, func_name, module, params_json,
            started_at_str, elapsed, 1 if is_success else 0, error_msg,
        ),
    })


def shutdown_call_log_writer(timeout: float = 5.0) -> None:
    """Flush and stop the process-wide call-log worker."""

    _call_log_writer.stop(timeout=timeout)


def log_function_info(func: Callable) -> Callable:
    """
    函数执行日志装饰器 - 记录方法名、参数和执行耗时

    Args:
        func: 被装饰的函数

    Returns:
        包装后的函数
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        # 生成请求 ID（取前8位）
        request_id = str(uuid.uuid4())[:8]

        # 获取方法名
        func_name = func.__name__

        # 记录开始
        logger.info(f"[{request_id}] [{func_name}] 开始执行")
        logger.debug(f"[{request_id}] [{func_name}] 参数: kwargs={kwargs}")

        # 记录开始时间
        start_time = time.time()

        # 模块名，取 func.__module__ 前缀
        module = func.__module__ or ""

        # 开始时间戳
        started_ts = datetime.fromtimestamp(start_time)

        try:
            # 执行函数
            result = func(*args, **kwargs)

            # 计算耗时
            elapsed = int((time.time() - start_time))

            # 记录成功
            logger.info(f"[{request_id}] [{func_name}] 执行成功，耗时: {elapsed}s")

            # 入库
            _save_func_call_log(request_id, func_name, module, kwargs,
                                started_ts, elapsed, True, "")

            return result

        except Exception as e:
            # 计算耗时
            elapsed = int((time.time() - start_time))

            # 记录异常
            logger.error(f"[{request_id}] [{func_name}] 执行失败，耗时: {elapsed}s，错误: {str(e)}")

            # 入库
            _save_func_call_log(request_id, func_name, module, kwargs,
                                started_ts, elapsed, False, str(e))

            raise

    return wrapper


#  CREATE TABLE IF NOT EXISTS `llm_func_call_log` (                                     
#     `id`           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
#     `request_id`   VARCHAR(16)          NOT NULL DEFAULT '',                                                                                                                                                                  
#     `func_name`    VARCHAR(255)        NOT NULL DEFAULT '',                                                                                                                                                                   
#     `module`       VARCHAR(255)        NOT NULL DEFAULT '',                                                                                                                                                                   
#     `params`       TEXT                  NULL,                                                                                                                                                                                
#     `started_at`   DATETIME(3)          NOT NULL,                                                                                                                                                                             
#     `elapsed`      INT UNSIGNED         NOT NULL DEFAULT 0,                                                                                                                                                                   
#     `is_success`   TINYINT UNSIGNED    NOT NULL DEFAULT 1,                                                                                                                                                                    
#     `error_msg`    TEXT                  NULL,                                                                                                                                                                                
                                                                                                                                                                                                                              
#     INDEX idx_request_id(`request_id`),                                                                                                                                                                                       
#     INDEX idx_func_name(`func_name`),                                                                                                                                                                                         
#     INDEX idx_started_at(`started_at`)                                                                                                                                                                                        
#   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='函数调用日志表';
