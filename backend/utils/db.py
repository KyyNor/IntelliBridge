from typing import Union, Optional, Dict, Any
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session, Session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.pool import QueuePool
from fastapi import HTTPException
from contextlib import contextmanager
from utils.config_manager import ConfigManager
from utils.logger import log
from pathlib import Path

Base = declarative_base()


class DatabaseManager:
    _instance = None
    _session_registry: Dict[int, Session] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.settings = ConfigManager()
            log.info("初始化数据库管理器")

            # 连接池配置 - 优化连接池参数
            pool_options = {
                'pool_size': 10,              # 连接池大小
                'max_overflow': 20,           # 最大溢出连接数
                'pool_timeout': 60,           # 连接池获取连接的超时时间
                'pool_recycle': 1800,         # 连接回收时间（秒）
                'pool_pre_ping': True,        # 连接前检查连接是否有效
                'echo': False                 # 不输出SQL语句
            }

            if cls._instance.settings.db_type == 'sqlite':
                sqlite_base_url = str(Path(__file__).parent.parent / 'embedded_database')
                db_url = f"sqlite:///{sqlite_base_url}/{cls._instance.settings.sqlite_db_path}"
                log.info(f"使用SQLite数据库: {db_url}")
                # SQLite不需要大连接池
                pool_options['pool_size'] = 1
                pool_options['max_overflow'] = 0
            elif cls._instance.settings.db_type == 'mysql':
                mysql_config = cls._instance.settings.mysql_config
                db_url = f"mysql+pymysql://{mysql_config['user']}:{mysql_config['password']}@{mysql_config['host']}/{mysql_config['database']}"
                log.info(f"使用MySQL数据库: {mysql_config['host']}/{mysql_config['database']}")
            else:
                log.error(f"不支持的数据库类型: {cls._instance.settings.db_type}")
                raise HTTPException(status_code=400, detail="Unsupported database type")

            # 创建带连接池的引擎
            cls._instance.engine = create_engine(
                db_url,
                poolclass=QueuePool,
                **pool_options
            )
            # 创建会话工厂
            cls._instance.Session = scoped_session(sessionmaker(bind=cls._instance.engine))
        return cls._instance

    def connect(self) -> Session:
        """获取数据库会话，优先从会话注册表中获取已存在的会话"""
        try:
            # 尝试获取当前线程/协程的会话
            session = self.Session()
            log.debug("成功获取数据库会话")
            return session
        except SQLAlchemyError as e:
            log.error(f"数据库连接错误: {str(e)}")
            # 尝试重新初始化连接池
            self.engine.dispose()
            log.info("已重置连接池，尝试重新连接")
            try:
                # 重置会话工厂
                self.reset_session()
                session = self.Session()
                return session
            except SQLAlchemyError as e2:
                log.error(f"重新连接失败: {str(e2)}")
                raise HTTPException(status_code=500, detail=f"Database connection error: {str(e2)}")


    def close(self):
        """关闭所有会话并释放连接池资源"""
        self.Session.remove()
        if self.engine:
            self.engine.dispose()
            log.debug("数据库连接池已关闭")
            
    def reset_session(self):
        """重置会话工厂，用于解决连接池耗尽问题"""
        self.Session.remove()
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        log.debug("会话工厂已重置")

    @contextmanager
    def get_session(self) -> Session:
        """会话上下文管理器，用于with语句"""
        session = None
        try:
            session = self.connect()
            yield session
        except Exception as e:
            if session:
                session.rollback()
            # 尝试重置会话工厂
            self.reset_session()
            raise e
        finally:
            # 确保会话被关闭，即使在异常情况下
            if session:
                session.close()
            
    def execute_query(self, query: str, params=None, fetch_one=False):
        log.debug(f"执行查询: {query}")
        with self.get_session() as session:
            try:
                result = session.execute(text(query), params or {})
                if fetch_one:
                    return result.fetchone()
                return result.fetchall()
            except SQLAlchemyError as e:
                log.error(f"数据库查询错误: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")

    def execute_update(self, query: str, params=None):
        log.debug(f"执行更新: {query}")
        with self.get_session() as session:
            try:
                result = session.execute(text(query), params or {})
                session.commit()
                log.debug(f"更新完成，影响行数: {result.rowcount}")
                return result.rowcount
            except SQLAlchemyError as e:
                session.rollback()
                log.error(f"数据库更新错误: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Database update error: {str(e)}")