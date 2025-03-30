from typing import Dict, Optional
from sqlalchemy import Column, String, Text, Integer
from sqlalchemy.ext.declarative import declarative_base
from utils.db import DatabaseManager

Base = declarative_base()

class Service(Base):
    __tablename__ = 'ib_services_info'
    
    id = Column(Integer, primary_key=True, autoincrement=True)  # 新增自增主键
    name = Column(String(255), unique=True)  # 修改为unique约束
    description = Column(Text)
    endpoint = Column(String(255), nullable=False)

class ServiceModel:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        Base.metadata.create_all(bind=self.db.engine)

    def register_service(self, service: Service):
        session = self.db.connect()
        try:
            session.merge(service)
            session.commit()
            return service
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def list_services(self):
        session = self.db.connect()
        try:
            return session.query(Service).all()
        finally:
            session.close()