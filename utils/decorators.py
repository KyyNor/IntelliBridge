import time
import functools
import uuid
from typing import Callable, Any
from utils.logger import logger


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

        try:
            # 执行函数
            result = func(*args, **kwargs)

            # 计算耗时
            elapsed_time = time.time() - start_time

            # 记录成功
            logger.info(f"[{request_id}] [{func_name}] 执行成功，耗时: {elapsed_time:.3f}秒")

            return result

        except Exception as e:
            # 计算耗时
            elapsed_time = time.time() - start_time

            # 记录异常
            logger.error(f"[{request_id}] [{func_name}] 执行失败，耗时: {elapsed_time:.3f}秒，错误: {str(e)}")
            raise

    return wrapper
