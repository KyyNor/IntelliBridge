# MCP服务功能分析

## 一、MCP服务的能力测试功能设计

### 1. 需求概述

MCP服务的能力测试功能需要对MCP服务的工具和资源进行测试，确保其可用性和性能符合预期。测试内容包括工具测试和资源测试等。

### 2. 数据库表设计

```sql
-- MCP服务测试记录表
CREATE TABLE ib_mcp_service_test_record (
    id INT PRIMARY KEY AUTO_INCREMENT,
    service_id INT NOT NULL,                -- 关联的服务ID
    test_name VARCHAR(255) NOT NULL,        -- 测试名称
    test_type VARCHAR(50) NOT NULL,         -- 测试类型：tool_test, resource_test
    status VARCHAR(20) NOT NULL,            -- 测试状态：success, failed, running
    start_time DATETIME NOT NULL,           -- 开始时间
    end_time DATETIME,                      -- 结束时间
    duration INT,                           -- 持续时间(毫秒)
    created_by VARCHAR(50),                 -- 创建人
    FOREIGN KEY (service_id) REFERENCES ib_mcp_service_info(id) ON DELETE CASCADE
);

-- MCP服务测试详情表
CREATE TABLE ib_mcp_service_test_detail (
    id INT PRIMARY KEY AUTO_INCREMENT,
    record_id INT NOT NULL,                 -- 关联的测试记录ID
    capability_id INT NOT NULL,             -- 关联的能力ID
    capability_name VARCHAR(255) NOT NULL,  -- 能力名称
    capability_type VARCHAR(20) NOT NULL,   -- 能力类型：tool, resource
    test_params TEXT,                       -- 测试参数(JSON格式)
    test_result TEXT,                       -- 测试结果(JSON格式)
    status VARCHAR(20) NOT NULL,            -- 测试状态：success, failed
    error_message TEXT,                     -- 错误信息
    response_time INT,                      -- 响应时间(毫秒)
    FOREIGN KEY (record_id) REFERENCES ib_mcp_service_test_record(id) ON DELETE CASCADE
);
```

### 3. 后台设计

#### 3.1 模型设计

```python
# models/mcp_service_test_model.py
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from utils.db import DatabaseManager
from enum import Enum

Base = declarative_base()

class TestStatus(str, Enum):
    SUCCESS = 'success'
    FAILED = 'failed'
    RUNNING = 'running'

class TestType(str, Enum):
    TOOL_TEST = 'tool_test'
    RESOURCE_TEST = 'resource_test'

class McpServiceTestRecord(Base):
    __tablename__ = 'ib_mcp_service_test_record'
    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(Integer, nullable=False)
    test_name = Column(String(255), nullable=False)
    test_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)
    duration = Column(Integer)
    created_by = Column(String(50))

class McpServiceTestDetail(Base):
    __tablename__ = 'ib_mcp_service_test_detail'
    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(Integer, ForeignKey('ib_mcp_service_test_record.id'), nullable=False)
    capability_id = Column(Integer, nullable=False)
    capability_name = Column(String(255), nullable=False)
    capability_type = Column(String(20), nullable=False)
    test_params = Column(Text)
    test_result = Column(Text)
    status = Column(String(20), nullable=False)
    error_message = Column(Text)
    response_time = Column(Integer)
```

#### 3.2 服务设计

```python
# services/mcp_service_tester.py
import asyncio
import json
import datetime
from mcp import ClientSession
from mcp.client.sse import sse_client
from models.mcp_service_test_model import McpServiceTestRecord, McpServiceTestDetail, TestStatus, TestType
from models.mcp_service_model import McpService
from models.mcp_service_capability_model import McpServiceCapabilityModel, CapabilityType
from utils.logger import log

class McpServiceTester:
    def __init__(self, db_manager, mcp_service_model, capability_model):
        self.db_manager = db_manager
        self.mcp_service_model = mcp_service_model
        self.capability_model = capability_model
    
    async def test_service_capabilities(self, service_id, test_params=None):
        """测试服务的所有能力"""
        service = self.mcp_service_model.get_service_by_id(service_id)
        if not service:
            return {"error": "Service not found"}
            
        # 创建测试记录
        test_record = McpServiceTestRecord(
            service_id=service_id,
            test_name=f"全面测试-{service.name}",
            test_type=TestType.TOOL_TEST,
            status=TestStatus.RUNNING,
            start_time=datetime.datetime.now()
        )
        
        # 保存测试记录
        with self.db_manager.get_session() as session:
            session.add(test_record)
            session.commit()
            record_id = test_record.id
        
        # 获取服务能力列表
        capabilities = self.capability_model.get_capabilities_by_service_id(service_id)
        
        # 分类能力
        tools = [cap for cap in capabilities if cap.cap_type == CapabilityType.TOOL]
        resources = [cap for cap in capabilities if cap.cap_type == CapabilityType.RESOURCE]
        
        # 测试工具能力
        tool_results = await self._test_tools(service, tools, record_id)
        
        # 测试资源能力
        resource_results = await self._test_resources(service, resources, record_id)
        
        # 更新测试记录状态
        end_time = datetime.datetime.now()
        duration = (end_time - test_record.start_time).total_seconds() * 1000
        
        # 判断测试是否全部成功
        all_success = all(result["status"] == TestStatus.SUCCESS for result in tool_results + resource_results)
        
        with self.db_manager.get_session() as session:
            record = session.query(McpServiceTestRecord).get(record_id)
            record.status = TestStatus.SUCCESS if all_success else TestStatus.FAILED
            record.end_time = end_time
            record.duration = int(duration)
            session.commit()
        
        return {
            "record_id": record_id,
            "status": TestStatus.SUCCESS if all_success else TestStatus.FAILED,
            "tool_results": tool_results,
            "resource_results": resource_results
        }
    
    async def _test_tools(self, service, tools, record_id):
        """测试工具能力"""
        results = []
        
        try:
            async with sse_client(f"http://{service.ip}:{service.port}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    for tool in tools:
                        start_time = datetime.datetime.now()
                        test_detail = McpServiceTestDetail(
                            record_id=record_id,
                            capability_id=tool.id,
                            capability_name=tool.name,
                            capability_type=CapabilityType.TOOL,
                            status=TestStatus.RUNNING
                        )
                        
                        try:
                            # 解析工具参数
                            params = {}
                            if tool.parameters:
                                schema = json.loads(tool.parameters)
                                # 根据参数schema生成测试参数
                                params = self._generate_test_params(schema)
                            
                            # 调用工具
                            test_detail.test_params = json.dumps(params)
                            response = await session.call_tool(tool.name, params)
                            
                            # 记录结果
                            end_time = datetime.datetime.now()
                            duration = (end_time - start_time).total_seconds() * 1000
                            
                            test_detail.test_result = json.dumps(response)
                            test_detail.status = TestStatus.SUCCESS
                            test_detail.response_time = int(duration)
                            
                            result = {
                                "tool_id": tool.id,
                                "tool_name": tool.name,
                                "status": TestStatus.SUCCESS,
                                "response_time": int(duration)
                            }
                        except Exception as e:
                            # 记录错误
                            end_time = datetime.datetime.now()
                            duration = (end_time - start_time).total_seconds() * 1000
                            
                            test_detail.status = TestStatus.FAILED
                            test_detail.error_message = str(e)
                            test_detail.response_time = int(duration)
                            
                            result = {
                                "tool_id": tool.id,
                                "tool_name": tool.name,
                                "status": TestStatus.FAILED,
                                "error": str(e),
                                "response_time": int(duration)
                            }
                        
                        # 保存测试详情
                        with self.db_manager.get_session() as session:
                            session.add(test_detail)
                            session.commit()
                        
                        results.append(result)
        except Exception as e:
            log.error(f"测试工具时发生错误: {str(e)}")
        
        return results
    
    async def _test_resources(self, service, resources, record_id):
        """测试资源能力"""
        results = []
        
        try:
            async with sse_client(f"http://{service.ip}:{service.port}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    for resource in resources:
                        start_time = datetime.datetime.now()
                        test_detail = McpServiceTestDetail(
                            record_id=record_id,
                            capability_id=resource.id,
                            capability_name=resource.name,
                            capability_type=CapabilityType.RESOURCE,
                            status=TestStatus.RUNNING
                        )
                        
                        try:
                            # 获取资源
                            response = await session.get_resource(resource.name)
                            
                            # 记录结果
                            end_time = datetime.datetime.now()
                            duration = (end_time - start_time).total_seconds() * 1000
                            
                            test_detail.test_result = json.dumps({"success": True})
                            test_detail.status = TestStatus.SUCCESS
                            test_detail.response_time = int(duration)
                            
                            result = {
                                "resource_id": resource.id,
                                "resource_name": resource.name,
                                "status": TestStatus.SUCCESS,
                                "response_time": int(duration)
                            }
                        except Exception as e:
                            # 记录错误
                            end_time = datetime.datetime.now()
                            duration = (end_time - start_time).total_seconds() * 1000
                            
                            test_detail.status = TestStatus.FAILED
                            test_detail.error_message = str(e)
                            test_detail.response_time = int(duration)
                            
                            result = {
                                "resource_id": resource.id,
                                "resource_name": resource.name,
                                "status": TestStatus.FAILED,
                                "error": str(e),
                                "response_time": int(duration)
                            }
                        
                        # 保存测试详情
                        with self.db_manager.get_session() as session:
                            session.add(test_detail)
                            session.commit()
                        
                        results.append(result)
        except Exception as e:
            log.error(f"测试资源时发生错误: {str(e)}")
        
        return results
    
    def _generate_test_params(self, schema):
        """根据参数schema生成测试参数"""
        params = {}
        
        if "properties" in schema:
            for prop_name, prop_schema in schema["properties"].items():
                # 根据参数类型生成测试值
                if prop_schema.get("type") == "string":
                    params[prop_name] = f"test_{prop_name}"
                elif prop_schema.get("type") == "number" or prop_schema.get("type") == "integer":
                    params[prop_name] = 1
                elif prop_schema.get("type") == "boolean":
                    params[prop_name] = True
                elif prop_schema.get("type") == "array":
                    params[prop_name] = []
                elif prop_schema.get("type") == "object":
                    params[prop_name] = {}
        
        return params
```

#### 3.3 API设计

```python
# routers/mcp_service_tester.py
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Optional
from models.mcp_service_model import McpServiceModel
from models.mcp_service_capability_model import McpServiceCapabilityModel
from models.mcp_service_test_model import McpServiceTestRecord
from services.mcp_service_tester import McpServiceTester
from utils.db import DatabaseManager
from utils.logger import log

router = APIRouter(prefix="/api/mcp_service_tester", tags=["MCP Service Tester"])

def get_db_manager():
    return DatabaseManager()

def get_mcp_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceModel(db_manager)

def get_capability_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceCapabilityModel(db_manager)

def get_service_tester(
    db_manager: DatabaseManager = Depends(get_db_manager),
    mcp_service_model: McpServiceModel = Depends(get_mcp_service_model),
    capability_model: McpServiceCapabilityModel = Depends(get_capability_model)
):
    return McpServiceTester(db_manager, mcp_service_model, capability_model)

@router.post("/test/{service_id}")
async def test_service(
    service_id: int,
    test_params: Optional[Dict] = None,
    service_tester: McpServiceTester = Depends(get_service_tester)
):
    """测试指定服务的所有能力"""
    log.info(f"开始测试服务: {service_id}")
    result = await service_tester.test_service_capabilities(service_id, test_params)
    return result

@router.get("/records")
async def get_test_records(
    service_id: Optional[int] = None,
    db_manager: DatabaseManager = Depends(get_db_manager)
):
    """获取测试记录列表"""
    with db_manager.get_session() as session:
        query = session.query(McpServiceTestRecord)
        if service_id:
            query = query.filter(McpServiceTestRecord.service_id == service_id)
        records = query.order_by(McpServiceTestRecord.start_time.desc()).all()
        return {"records": records}

@router.get("/records/{record_id}")
async def get_test_record_details(
    record_id: int,
    db_manager: DatabaseManager = Depends(get_db_manager)
):
    """获取测试记录详情"""
    with db_manager.get_session() as session:
        record = session.query(McpServiceTestRecord).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="Test record not found")
        
        details = session.query(McpServiceTestDetail).filter(
            McpServiceTestDetail.record_id == record_id
        ).all()
        
        return {
            "record": record,
            "details": details
        }
```

### 4. 前端设计

#### 4.1 服务测试页面

服务测试页面应包含以下功能：

1. 服务选择：下拉列表选择要测试的服务
2. 测试参数配置：可选的测试参数配置
3. 开始测试按钮：点击后开始测试
4. 测试进度显示：显示测试进度和状态
5. 测试结果展示：
   - 总体测试结果（成功/失败）
   - 测试持续时间
   - 各能力测试结果列表（工具和资源分开显示）
   - 每个能力的测试状态、响应时间和错误信息（如果有）

#### 4.2 测试历史记录页面

测试历史记录页面应包含以下功能：

1. 测试记录列表：显示所有测试记录
   - 服务名称
   - 测试名称
   - 测试类型
   - 测试状态
   - 开始时间
   - 持续时间
2. 筛选功能：按服务、状态、时间等筛选
3. 详情查看：点击记录查看详细测试结果

## 二、MCP服务的自动组装服务功能设计

### 1. 需求概述

MCP服务的自动组装功能需要支持从多个不同的MCP服务中选择能力，组装成一个新的MCP服务。需要考虑：

1. 父服务能力消失时，组装服务对应能力也应不可用
2. 目前每次更新MCP服务能力时都是全量删除，需考虑在组装场景下如何更新MCP服务能力

### 2. 数据库表设计

```sql
-- MCP组装服务表
CREATE TABLE ib_mcp_composite_service (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL UNIQUE,     -- 组装服务名称
    description TEXT,                      -- 服务描述
    endpoint VARCHAR(255) NOT NULL,        -- 服务端点
    ip VARCHAR(50),                        -- 服务IP
    port INT,                              -- 服务端口
    version VARCHAR(50),                   -- 服务版本
    status VARCHAR(20) DEFAULT 'offline',  -- 服务状态
    created_tm DATETIME,                   -- 创建时间
    updated_tm DATETIME                    -- 更新时间
);

-- MCP组装服务能力映射表
CREATE TABLE ib_mcp_composite_capability_mapping (
    id INT PRIMARY KEY AUTO_INCREMENT,
    composite_service_id INT NOT NULL,     -- 组装服务ID
    source_service_id INT NOT NULL,        -- 源服务ID
    capability_id INT NOT NULL,            -- 能力ID
    capability_name VARCHAR(255) NOT NULL, -- 能力名称
    capability_type VARCHAR(20) NOT NULL,  -- 能力类型
    alias_name VARCHAR(255),               -- 别名（可选，用于在组装服务中重命名能力）
    created_tm DATETIME,                   -- 创建时间
    FOREIGN KEY (composite_service_id) REFERENCES ib_mcp_composite_service(id) ON DELETE CASCADE,
    FOREIGN KEY (source_service_id) REFERENCES ib_mcp_service_info(id) ON DELETE CASCADE
);
```

### 3. 后台设计

#### 3.1 模型设计

```python
# models/mcp_composite_service_model.py
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from utils.db import DatabaseManager
from models.mcp_service_model import ServiceStatus
import datetime

Base = declarative_base()

class McpCompositeService(Base):
    __tablename__ = 'ib_mcp_composite_service'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text)
    endpoint = Column(String(255), nullable=False)
    ip = Column(String(50))
    port = Column(Integer)
    version = Column(String(50))
    status = Column(String(20), default=ServiceStatus.OFFLINE)
    created_tm = Column(DateTime, default=datetime.datetime.now())
    updated_tm = Column(DateTime, onupdate=datetime.datetime.now())

class McpCompositeCapabilityMapping(Base):
    __tablename__ = 'ib_mcp_composite_capability_mapping'
    id = Column(Integer, primary_key=True, autoincrement=True)
    composite_service_id = Column(Integer, ForeignKey('ib_mcp_composite_service.id'), nullable=False)
    source_service_id = Column(Integer, ForeignKey('ib_mcp_service_info.id'), nullable=False)
    capability_id = Column(Integer, nullable=False)
    capability_name = Column(String(255), nullable=False)
    capability_type = Column(String(20), nullable=False)
    alias_name = Column(String(255))
    created_tm = Column(DateTime, default=datetime.datetime.now())

class McpCompositeServiceModel:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        Base.metadata.create_all(bind=self.db.engine)
    
    def create_composite_service(self, service_data):
        """创建组装服务"""
        with self.db.get_session() as session:
            try:
                composite_service = McpCompositeService(
                    name=service_data.get("name"),
                    description=service_data.get("description"),
                    endpoint=service_data.get("endpoint"),
                    ip=service_data.get("ip"),
                    port=service_data.get("port"),
                    version=service_data.get("version"),
                    status=ServiceStatus.OFFLINE,
                    created_tm=datetime.datetime.now(),
                    updated_tm=datetime.datetime.now()
                )
                session.add(composite_service)
                session.commit()
                return composite_service
            except Exception as e:
                session.rollback()
                raise e
    
    def update_composite_service(self, service_id, service_data):
        """更新组装服务"""
        with self.db.get_session() as session:
            try:
                service = session.query(McpCompositeService).get(service_id)
                if not service:
                    return None
                
                service.name = service_data.get("name", service.name)
                service.description = service_data.get("description", service.description)
                service.endpoint = service_data.get("endpoint", service.endpoint)
                service.ip = service_data.get("ip", service.ip)
                service.port = service_data.get("port", service.port)
                service.version = service_data.get("version", service.version)
                service.updated_tm = datetime.datetime.now()
                
                session.commit()
                return service
            except Exception as e:
                session.rollback()
                raise e
    
    def delete_composite_service(self, service_id):
        """删除组装服务"""
        with self.db.get_session() as session:
            try:
                service = session.query(McpCompositeService).get(service_id)
                if not service:
                    return False
                
                session.delete(service)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                raise e
    
    def get_composite_service(self, service_id):
        """获取组装服务"""
        with self.db.get_session() as session:
            return session.query(McpCompositeService).get(service_id)
    
    def list_composite_services(self):
        """获取所有组装服务"""
        with self.db.get_session() as session:
            return session.query(McpCompositeService).all()
    
    def add_capability_mapping(self, mapping_data):
        """添加能力映射"""
        with self.db.get_session() as session:
            try:
                mapping = McpCompositeCapabilityMapping(
                    composite_service_id=mapping_data.get("composite_service_id"),
                    source_service_id=mapping_data.get("source_service_id"),
                    capability_id=mapping_data.get("capability_id"),
                    capability_name=mapping_data.get("capability_name"),
                    capability_type=mapping_data.get("capability_type"),
                    alias_name=mapping_data.get("alias_name"),
                    created_tm=datetime.datetime.now()
                )
                session.add(mapping)
                session.commit()
                return mapping
            except Exception as e:
                session.rollback()
                raise e
    
    def delete_capability_mapping(self, mapping_id):
        """删除能力映射"""
        with self.db.get_session() as session:
            try:
                mapping = session.query(McpCompositeCapabilityMapping).get(mapping_id)
                if not mapping:
                    return False
                
                session.delete(mapping)
                session.commit()
                return True
            except Exception as e:
                session.rollback()
                raise e
    
    def get_capability_mappings(self, composite_service_id):
        """获取组装服务的能力映射"""
        with self.db.get_session() as session:
            return session.query(McpCompositeCapabilityMapping).filter(
                McpCompositeCapabilityMapping.composite_service_id == composite_service_id
            ).all()
```

#### 3.2 服务设计

```python
# services/mcp_composite_service_manager.py
import asyncio
import datetime
from typing import List, Dict
from models.mcp_service_model import McpServiceModel, ServiceStatus
from models.mcp_service_capability_model import McpServiceCapabilityModel, CapabilityType
from models.mcp_composite_service_model import McpCompositeServiceModel
from utils.logger import log

class McpCompositeServiceManager:
    def __init__(self, db_manager, mcp_service_model, capability_model, composite_service_model):
        self.db_manager = db_manager
        self.mcp_service_model = mcp_service_model
        self.capability_model = capability_model
        self.composite_service_model = composite_service_model
    
    async def create_composite_service(self, service_data, capability_mappings):
        """创建组装服务及其能力映射"""
        try:
            # 创建组装服务
            composite_service = self.composite_service_model.create_composite_service(service_data)
            
            # 添加能力映射
            for mapping in capability_mappings:
                mapping["composite_service_id"] = composite_service.id
                self.composite_service_model.add_capability_mapping(mapping)
            
            # 注册到MCP服务管理器
            await self._register_to_mcp_manager(composite_service)
            
            return composite_service
        except Exception as e:
            log.error(