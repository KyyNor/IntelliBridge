from typing import Union
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from fastapi import HTTPException
from utils.config_manager import ConfigManager
from utils.logger import log
from pathlib import Path

Base = declarative_base()


class DatabaseManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance.settings = ConfigManager()
            log.info("初始化数据库管理器")

            if cls._instance.settings.db_type == 'sqlite':
                sqlite_base_url = str(Path(__file__).parent.parent / 'embedded_database')
                db_url = f"sqlite:///{sqlite_base_url}/{cls._instance.settings.sqlite_db_path}"
                log.info(f"使用SQLite数据库: {db_url}")
            elif cls._instance.settings.db_type == 'mysql':
                mysql_config = cls._instance.settings.mysql_config
                db_url = f"mysql+pymysql://{mysql_config['user']}:{mysql_config['password']}@{mysql_config['host']}/{mysql_config['database']}"
                log.info(f"使用MySQL数据库: {mysql_config['host']}/{mysql_config['database']}")
            else:
                log.error(f"不支持的数据库类型: {cls._instance.settings.db_type}")
                raise HTTPException(status_code=400, detail="Unsupported database type")

            cls._instance.engine = create_engine(db_url)
            cls._instance.Session = sessionmaker(bind=cls._instance.engine)
        return cls._instance

    # def __init__(self):
    #     self.settings = ConfigManager()
    #     log.info("初始化数据库管理器")
    #
    #     if self.settings.db_type == 'sqlite':
    #         sqlite_base_url = str(Path(__file__).parent.parent / 'embedded_database')
    #         db_url = f"sqlite:///{sqlite_base_url}/{self.settings.sqlite_db_path}"
    #         log.info(f"使用SQLite数据库: {db_url}")
    #     elif self.settings.db_type == 'mysql':
    #         mysql_config = self.settings.mysql_config
    #         db_url = f"mysql+pymysql://{mysql_config['user']}:{mysql_config['password']}@{mysql_config['host']}/{mysql_config['database']}"
    #         log.info(f"使用MySQL数据库: {mysql_config['host']}/{mysql_config['database']}")
    #     else:
    #         log.error(f"不支持的数据库类型: {self.settings.db_type}")
    #         raise HTTPException(status_code=400, detail="Unsupported database type")
    #
    #     self.engine = create_engine(db_url)
    #     self.Session = sessionmaker(bind=self.engine)

    def connect(self):
        try:
            session = self.Session()
            log.debug("成功创建数据库会话")
            return session
        except SQLAlchemyError as e:
            log.error(f"数据库连接错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

    def close(self):
        if self.engine:
            self.engine.dispose()
            log.debug("数据库连接已关闭")

    def execute_query(self, query: str, params=None, fetch_one=False):
        log.debug(f"执行查询: {query}")
        session = self.connect()
        try:
            result = session.execute(text(query), params or {})
            if fetch_one:
                return result.fetchone()
            return result.fetchall()
        except SQLAlchemyError as e:
            log.error(f"数据库查询错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")
        finally:
            session.close()

    def execute_update(self, query: str, params=None):
        log.debug(f"执行更新: {query}")
        session = self.connect()
        try:
            result = session.execute(text(query), params or {})
            session.commit()
            log.debug(f"更新完成，影响行数: {result.rowcount}")
            return result.rowcount
        except SQLAlchemyError as e:
            session.rollback()
            log.error(f"数据库更新错误: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Database update error: {str(e)}")
        finally:
            session.close()