from typing import Dict, List, Optional
import datetime
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from utils.db import DatabaseManager
from .mcp_service_model import McpService
from enum import Enum

Base = declarative_base()

class CapabilityType(str, Enum):
    TOOL = 'tool'      # 工具
    RESOURCE = 'resource'  # 资源

class McpServiceCapability(Base):
    __tablename__ = 'ib_mcp_service_capability'  # 保持表名不变，避免数据库迁移问题
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(Integer, ForeignKey('ib_mcp_service_info.id'), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    type = Column(String(20), nullable=False, default=CapabilityType.TOOL)  # 类型字段
    parameters = Column(Text)  # 存储JSON格式的参数列表
    created_tm = Column(DateTime, default=datetime.datetime.now())
    updated_tm = Column(DateTime, onupdate=datetime.datetime.now())

class McpServiceCapabilityModel:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        Base.metadata.create_all(bind=self.db.engine)

    def get_capabilities_by_service_id(self, service_id: int) -> List[McpServiceCapability]:
        """
        根据服务ID查询所有工具和资源
        """
        session = self.db.connect()
        try:
            return session.query(McpServiceCapability).filter(
                McpServiceCapability.service_id == service_id
            ).all()
        finally:
            session.close()

    def update_service_capabilities(self, service_id: int, capabilities: List[Dict]) -> List[McpServiceCapability]:
        """
        更新服务的工具和资源列表
        会先删除该服务ID下的所有历史记录，然后插入新的记录
        """
        session = self.db.connect()
        try:
            # 删除该服务ID下的所有历史记录
            session.query(McpServiceCapability).filter(
                McpServiceCapability.service_id == service_id
            ).delete()
            
            # 插入新的记录
            new_capabilities = []
            for capability_data in capabilities:
                capability = McpServiceCapability(
                    service_id=service_id,
                    name=capability_data.get('name'),
                    description=capability_data.get('description'),
                    parameters=capability_data.get('parameters')
                )
                session.add(capability)
                new_capabilities.append(capability)
            
            session.commit()
            return new_capabilities
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()