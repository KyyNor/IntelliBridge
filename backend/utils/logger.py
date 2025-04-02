from loguru import logger
import sys
import os


class Logger:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Logger, cls).__new__(cls)
            cls._instance._initialize_logger()
        return cls._instance
    
    def _initialize_logger(self):
        # 创建logs目录（如果不存在）
        if not os.path.exists("logs"):
            os.makedirs("logs")
            
        # 配置日志格式和输出
        logger.remove()  # 移除默认的处理器
        
        # 添加控制台输出
        logger.add(
            sys.stdout,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level="INFO"
        )
        
        # 添加文件输出
        logger.add(
            "logs/app_{time}.log",
            rotation="500 MB",  # 日志文件大小超过500MB时轮转
            retention="10 days",  # 保留10天的日志
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            level="DEBUG"
        )
    
    @staticmethod
    def get_logger():
        return logger

# 创建一个全局的logger实例
log = Logger.get_logger()
