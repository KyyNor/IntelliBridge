import sys
from loguru import logger
from pathlib import Path


class Logger:
    """日志工具类，基于 loguru"""

    def __init__(self, log_dir: str = None, log_level: str = "INFO"):
        """
        初始化日志配置

        Args:
            log_dir: 日志文件存储目录，默认为项目根目录下的 logs 文件夹
            log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        """
        if log_dir is None:
            # 获取项目根目录
            log_dir = Path(__file__).parent.parent / "logs"
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_level = log_level

        # 移除默认的处理器
        logger.remove()

        # 控制台输出
        logger.add(
            sys.stdout,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level=log_level,
            colorize=True
        )

        # 文件输出 - 所有日志
        logger.add(
            self.log_dir / "app_{time:YYYY-MM-DD}.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level=log_level,
            rotation="00:00",  # 每天午夜轮转
            retention="30 days",  # 保留30天
            compression="zip",  # 压缩旧日志
            encoding="utf-8"
        )

        # 文件输出 - 错误日志
        logger.add(
            self.log_dir / "error_{time:YYYY-MM-DD}.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level="ERROR",
            rotation="00:00",
            retention="30 days",
            compression="zip",
            encoding="utf-8"
        )

    def get_logger(self):
        """获取 logger 实例"""
        return logger


# 默认日志实例
logger = Logger().get_logger()
