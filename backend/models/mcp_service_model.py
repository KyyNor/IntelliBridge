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
        with self.db.get_session() as session:
            return session.query(McpService).get(service_id)

    def get_service_by_endpoint(self, endpoint: str) -> Optional[McpService]:
        """
        根据endpoint名称查找服务
        """
        with self.db.get_session() as session:
            return session.query(McpService).filter(McpService.endpoint == endpoint).first()

    def register_mcp_service(self, mcp_service: McpService):
        with self.db.get_session() as session:
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

    def list_mcp_services(self):
        with self.db.get_session() as session:
            return session.query(McpService).all()
            
    def update_service_status(self, mcp_service: McpService):
        with self.db.get_session() as session:
            try:
                session.merge(mcp_service)
                session.commit()
                return mcp_service
            except Exception as e:
                session.rollback()
                raise e
            
    def delete_service(self, service_id: int) -> bool:
        """
        删除指定ID的服务
        """
        with self.db.get_session() as session:
            try:
                service = session.query(McpService).get(service_id)
                if not service:
                    return False
                session.delete(service)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                raise e