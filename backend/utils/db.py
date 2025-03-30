from typing import Union
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from fastapi import HTTPException
from utils.config_manager import ConfigManager
from pathlib import Path

Base = declarative_base()

class DatabaseManager:
    def __init__(self):
        self.settings = ConfigManager()
        if self.settings.db_type == 'sqlite':
            sqlite_base_url = str(Path(__file__).parent.parent / 'embedded_database')
            db_url = f"sqlite:///{sqlite_base_url}/{self.settings.sqlite_db_path}"
        elif self.settings.db_type == 'mysql':
            mysql_config = self.settings.mysql_config
            db_url = f"mysql+pymysql://{mysql_config['user']}:{mysql_config['password']}@{mysql_config['host']}/{mysql_config['database']}"
        else:
            raise HTTPException(status_code=400, detail="Unsupported database type")
            
        self.engine = create_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    def connect(self):
        try:
            return self.Session()
        except SQLAlchemyError as e:
            raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

    def close(self):
        if self.engine:
            self.engine.dispose()

    def execute_query(self, query: str, params=None, fetch_one=False):
        session = self.connect()
        try:
            result = session.execute(text(query), params or {})
            if fetch_one:
                return result.fetchone()
            return result.fetchall()
        except SQLAlchemyError as e:
            raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")
        finally:
            session.close()

    def execute_update(self, query: str, params=None):
        session = self.connect()
        try:
            result = session.execute(text(query), params or {})
            session.commit()
            return result.rowcount
        except SQLAlchemyError as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Database update error: {str(e)}")
        finally:
            session.close()