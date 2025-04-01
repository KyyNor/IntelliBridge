from typing import Dict, Optional
import datetime
from enum import Enum
from sqlalchemy import Column, String, Text, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from utils.db import DatabaseManager


Base = declarative_base()

class ServiceStatus(str, Enum):
    OFFLINE = 'offline'
    ONLINE = 'online'
    MAINTENANCE = 'maintenance'

class CreationType(str, Enum):
    MANUAL = 'manual'
    AUTO = 'auto'


class McpService(Base):
    __tablename__ = 'ib_mcp_service_info'
    id = Column(Integer, primary_key=True, autoincrement=True)  # 新增自增主键
    name = Column(String(255), unique=True)  # 修改为unique约束
    description = Column(Text)
    endpoint = Column(String(255), nullable=False)
    ip = Column(String(50))
    port = Column(Integer)
    version = Column(String(50))
    created_tm = Column(DateTime, default=datetime.datetime.now())
    updated_tm = Column(DateTime, onupdate=datetime.datetime.now())
    status = Column(String(20), default=ServiceStatus.OFFLINE)
    status_check_tm = Column(DateTime)
    creation_type = Column(String(20), default=CreationType.MANUAL)



class McpServiceModel:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        Base.metadata.create_all(bind=self.db.engine)

    def get_service_by_id(self, service_id: int) -> Optional[McpService]:
        session = self.db.connect()
        try:
            return session.query(McpService).get(service_id)
        finally:
            session.close()

    def get_service_by_endpoint(self, endpoint: str) -> Optional[McpService]:
        """
        根据endpoint名称查找服务
        """
        session = self.db.connect()
        try:
            return session.query(McpService).filter(McpService.endpoint == endpoint).first()
        finally:
            session.close()

    def register_mcp_service(self, mcp_service: McpService):
        session = self.db.connect()

        try:
            if mcp_service.id:
                existing = session.query(McpService).get(mcp_service.id)
                if existing:
                    mcp_service.created_tm = existing.created_tm
            session.merge(mcp_service)
            session.commit()
            return mcp_service
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def list_mcp_services(self):
        session = self.db.connect()
        try:
            return session.query(McpService).all()
        finally:
            session.close()
            
    def update_service_status(self, mcp_service: McpService):
        session = self.db.connect()
        try:
            session.merge(mcp_service)
            session.commit()
            return mcp_service
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()