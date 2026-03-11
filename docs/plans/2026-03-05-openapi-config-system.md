# OpenAPI 动态配置系统实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**目标:** 构建一个动态 REST API 配置系统，允许用户通过 Web 界面配置接口，并自动注册为 MCP 工具，实现即时生效的 API 调用能力。

**架构:** FastAPI + Jinja2 + SQLAlchemy + FastMCP，采用服务端渲染的统一架构，所有模块在同一应用中，配置变更即时生效，无需重启服务。

**技术栈:**
- FastAPI 0.115.0 + Uvicorn
- SQLAlchemy 2.x + Pydantic 2.x
- FastMCP 3.1.0
- Jinja2 3.1.6
- cryptography (加密)
- MySQL 数据库
- TailwindCSS (UI)

---

## Task 1: 数据库模型层

**文件:**
- 创建: `models/__init__.py`
- 创建: `models/base.py`
- 创建: `models/project.py`
- 创建: `models/api_config.py`
- 创建: `models/api_log.py`

### Step 1: 创建模型包初始化文件

```bash
touch models/__init__.py
```

### Step 2: 编写基础模型类

创建: `models/base.py`

```python
from sqlalchemy import DeclarativeBase, Mapped, mapped_column
from datetime import datetime
from typing import Optional

class Base(DeclarativeBase):
    """所有模型的基类"""
    pass

class TimestampMixin:
    """时间戳混入类"""
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
```

### Step 3: 编写项目模型

创建: `models/project.py`

```python
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base, TimestampMixin

class Project(Base, TimestampMixin):
    """项目模型"""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 关联关系
    api_configs: Mapped[list["ApiConfig"]] = relationship(
        "ApiConfig", back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Project(id={self.id}, name='{self.name}')>"
```

### Step 4: 编写 API 配置模型

创建: `models/api_config.py`

```python
from sqlalchemy import String, Text, Boolean, Integer, Enum as SQLEnum, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional
from enum import Enum
from .base import Base, TimestampMixin

class AuthType(str, Enum):
    """认证类型"""
    NONE = "none"
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"

class HttpMethod(str, Enum):
    """HTTP 方法"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"

class ApiConfig(Base, TimestampMixin):
    """API 配置模型"""
    __tablename__ = "api_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    method: Mapped[HttpMethod] = mapped_column(SQLEnum(HttpMethod), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)

    auth_type: Mapped[AuthType] = mapped_column(
        SQLEnum(AuthType), default=AuthType.NONE, nullable=False
    )
    auth_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    headers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    body_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 关联关系
    project: Mapped["Project"] = relationship("Project", back_populates="api_configs")
    call_logs: Mapped[list["ApiCallLog"]] = relationship(
        "ApiCallLog", back_populates="api_config", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ApiConfig(id={self.id}, name='{self.name}', method='{self.method}')>"
```

### Step 5: 编写调用日志模型

创建: `models/api_log.py`

```python
from sqlalchemy import String, Text, Integer, ForeignKey, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import Optional
from datetime import datetime
from .base import Base

class ApiCallLog(Base):
    """API 调用日志模型"""
    __tablename__ = "api_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_config_id: Mapped[int] = mapped_column(ForeignKey("api_configs.id"), nullable=False)

    mcp_call_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    request_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_time: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 毫秒

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 关联关系
    api_config: Mapped["ApiConfig"] = relationship("ApiConfig", back_populates="call_logs")

    # 索引
    __table_args__ = (
        Index("idx_api_config", "api_config_id"),
        Index("idx_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<ApiCallLog(id={self.id}, api_config_id={self.api_config_id}, code={self.response_code})>"
```

### Step 6: 更新 requirements.txt

修改: `requirements.txt`

```text
# 添加以下依赖
sqlalchemy==2.0.36
pymysql==1.1.1
cryptography==44.0.0
httpx==0.28.1
```

### Step 7: 提交模型层

```bash
git add models/
git add requirements.txt
git commit -m "feat(models): 添加数据库模型层

- 项目模型 (Project)
- API配置模型 (ApiConfig)
- 调用日志模型 (ApiCallLog)
- 支持多种认证方式和HTTP方法
```

---

## Task 2: 数据库连接和加密工具

**文件:**
- 创建: `utils/database.py`
- 创建: `utils/encryption.py`

### Step 1: 编写数据库连接工具

创建: `utils/database.py`

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from typing import Generator
from contextlib import contextmanager
from utils.config import config
from utils.logger import logger

# 数据库连接配置
DATABASE_URL = config.get(
    "database.url",
    "mysql+pymysql://user:password@localhost/intellibridge"
)

# 创建引擎
engine = create_engine(
    DATABASE_URL,
    echo=config.get("database.echo", False),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@contextmanager
def get_db() -> Generator[Session, None, None]:
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """初始化数据库表"""
    from models.base import Base
    from models.project import Project
    from models.api_config import ApiConfig
    from models.api_log import ApiCallLog

    Base.metadata.create_all(bind=engine)
    logger.info("数据库表初始化完成")
```

### Step 2: 编写加密工具

创建: `utils/encryption.py`

```python
from cryptography.fernet import Fernet
from typing import Optional
import base64
import hashlib
from utils.logger import logger

class EncryptionManager:
    """加密管理器"""

    def __init__(self, secret_key: Optional[str] = None):
        """
        初始化加密管理器

        Args:
            secret_key: 密钥，如果为None则从配置读取
        """
        if secret_key is None:
            from utils.config import config
            secret_key = config.get("encryption.secret_key", None)

        if secret_key is None:
            # 生成默认密钥（生产环境应该从配置读取）
            logger.warning("未配置加密密钥，使用临时密钥")
            secret_key = Fernet.generate_key()

        # 确保密钥是32字节，然后编码为base64
        if isinstance(secret_key, str):
            secret_key = secret_key.encode()
        if len(secret_key) != 32:
            # 使用SHA256哈希确保密钥长度
            secret_key = hashlib.sha256(secret_key).digest()

        # 生成Fernet密钥
        self.key = base64.urlsafe_b64encode(secret_key)
        self.cipher = Fernet(self.key)

    def encrypt(self, data: str) -> str:
        """
        加密字符串

        Args:
            data: 要加密的字符串

        Returns:
            加密后的字符串（base64编码）
        """
        if not data:
            return ""
        encrypted = self.cipher.encrypt(data.encode())
        return base64.b64encode(encrypted).decode()

    def decrypt(self, encrypted_data: str) -> str:
        """
        解密字符串

        Args:
            encrypted_data: 加密的字符串（base64编码）

        Returns:
            解密后的字符串
        """
        if not encrypted_data:
            return ""
        try:
            decoded = base64.b64decode(encrypted_data.encode())
            decrypted = self.cipher.decrypt(decoded)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"解密失败: {e}")
            return ""

# 全局实例
encryption = EncryptionManager()
```

### Step 3: 提交数据库和加密工具

```bash
git add utils/database.py utils/encryption.py
git commit -m "feat(utils): 添加数据库连接和加密工具

- 数据库会话管理
- 敏感信息加密/解密
```

---

## Task 3: Pydantic 请求/响应模型

**文件:**
- 创建: `schemas/__init__.py`
- 创建: `schemas/project.py`
- 创建: `schemas/api_config.py`
- 创建: `schemas/api_log.py`

### Step 1: 创建 schemas 包

```bash
mkdir -p schemas && touch schemas/__init__.py
```

### Step 2: 编写项目 Schema

创建: `schemas/project.py`

```python
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class ProjectBase(BaseModel):
    """项目基础模型"""
    name: str = Field(..., min_length=1, max_length=100, description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")

class ProjectCreate(ProjectBase):
    """创建项目请求"""
    pass

class ProjectUpdate(BaseModel):
    """更新项目请求"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None

class ProjectResponse(ProjectBase):
    """项目响应"""
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
```

### Step 3: 编写 API 配置 Schema

创建: `schemas/api_config.py`

```python
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

class AuthType(str, Enum):
    """认证类型"""
    NONE = "none"
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"

class HttpMethod(str, Enum):
    """HTTP 方法"""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"

class ApiConfigBase(BaseModel):
    """API 配置基础模型"""
    name: str = Field(..., min_length=1, max_length=100, description="接口名称")
    description: Optional[str] = Field(None, description="接口描述")
    method: HttpMethod = Field(..., description="HTTP 方法")
    url: str = Field(..., min_length=1, max_length=500, description="请求地址")

    auth_type: AuthType = Field(default=AuthType.NONE, description="认证类型")
    auth_config: Optional[Dict[str, Any]] = Field(None, description="认证配置")

    headers: Optional[Dict[str, str]] = Field(None, description="请求头")
    params: Optional[Dict[str, Any]] = Field(None, description="URL参数")
    body_schema: Optional[Dict[str, Any]] = Field(None, description="请求体结构")

    enabled: bool = Field(default=True, description="是否启用")

class ApiConfigCreate(ApiConfigBase):
    """创建 API 配置请求"""
    project_id: int = Field(..., description="所属项目ID")

class ApiConfigUpdate(BaseModel):
    """更新 API 配置请求"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    method: Optional[HttpMethod] = None
    url: Optional[str] = Field(None, min_length=1, max_length=500)
    auth_type: Optional[AuthType] = None
    auth_config: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, str]] = None
    params: Optional[Dict[str, Any]] = None
    body_schema: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None

class ApiConfigResponse(ApiConfigBase):
    """API 配置响应"""
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime

    # 不返回加密的认证配置
    auth_config: Optional[Dict[str, Any]] = Field(None, exclude=True)

    class Config:
        from_attributes = True

class ApiConfigTestRequest(BaseModel):
    """测试 API 请求"""
    params: Optional[Dict[str, Any]] = Field(None, description="请求参数")
    headers: Optional[Dict[str, str]] = Field(None, description="额外请求头")
```

### Step 4: 编写日志 Schema

创建: `schemas/api_log.py`

```python
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ApiCallLogResponse(BaseModel):
    """API 调用日志响应"""
    id: int
    api_config_id: int
    mcp_call_id: Optional[str]
    request_body: Optional[str]
    response_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]
    execution_time: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True

class ApiCallLogListResponse(BaseModel):
    """日志列表响应"""
    total: int
    items: list[ApiCallLogResponse]
```

### Step 5: 提交 Schema 模型

```bash
git add schemas/
git commit -m "feat(schemas): 添加 Pydantic 请求/响应模型

- 项目、配置、日志的 CRUD 模型
- 支持验证和序列化
```

---

## Task 4: API 调用服务

**文件:**
- 创建: `services/__init__.py`
- 创建: `services/api_caller.py`

### Step 1: 创建服务包

```bash
mkdir -p services && touch services/__init__.py
```

### Step 2: 编写 API 调用服务

创建: `services/api_caller.py`

```python
import httpx
import asyncio
from typing import Optional, Dict, Any
from models.api_config import ApiConfig, AuthType
from models.api_log import ApiCallLog
from utils.encryption import encryption
from utils.database import get_db
from utils.logger import logger
from datetime import datetime
import time
import json

class ApiCaller:
    """API 调用服务"""

    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端"""
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=30.0)
        return self.client

    async def close(self):
        """关闭客户端"""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def call(
        self,
        config: ApiConfig,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        mcp_call_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        调用配置的 API

        Args:
            config: API 配置
            params: 请求参数
            headers: 额外的请求头
            mcp_call_id: MCP 调用ID

        Returns:
            调用结果
        """
        start_time = time.time()
        request_body = None
        response_code = None
        response_body = None
        error_message = None

        try:
            # 1. 构建请求 URL 和参数
            url = self._build_url(config.url, params or {})

            # 2. 构建请求头
            request_headers = self._build_headers(config, headers or {})

            # 3. 构建请求体
            request_body_str = None
            request_params = None
            if config.method in ["POST", "PUT", "PATCH"]:
                # 合并配置的 body_schema 和传入的 params
                body_data = {**(config.body_schema or {}), **(params or {})}
                request_body_str = json.dumps(body_data) if body_data else None
            else:
                # GET 等方法使用 query params
                request_params = {**(config.params or {}), **(params or {})}

            # 4. 发送请求
            client = await self.get_client()
            response = await client.request(
                method=config.method.value,
                url=url,
                headers=request_headers,
                params=request_params,
                content=request_body_str
            )

            # 5. 处理响应
            response_code = response.status_code
            try:
                response_body = response.json()
            except:
                response_body = response.text

            execution_time = int((time.time() - start_time) * 1000)

            # 6. 记录日志
            await self._log_call(
                config.id,
                mcp_call_id,
                request_body_str or json.dumps(request_params) if request_params else None,
                response_code,
                json.dumps(response_body) if isinstance(response_body, dict) else response_body,
                None,
                execution_time
            )

            return {
                "success": response.status_code < 400,
                "status_code": response.status_code,
                "data": response_body,
                "execution_time": execution_time
            }

        except httpx.TimeoutException:
            error_message = "请求超时"
            logger.error(f"API 调用超时: {config.name}")
        except httpx.HTTPError as e:
            error_message = f"HTTP 错误: {str(e)}"
            logger.error(f"API 调用失败: {config.name}, {e}")
        except Exception as e:
            error_message = f"未知错误: {str(e)}"
            logger.error(f"API 调用异常: {config.name}, {e}")

        # 记录错误日志
        execution_time = int((time.time() - start_time) * 1000)
        await self._log_call(
            config.id,
            mcp_call_id,
            request_body,
            response_code,
            response_body,
            error_message,
            execution_time
        )

        return {
            "success": False,
            "error": error_message,
            "execution_time": execution_time
        }

    def _build_url(self, base_url: str, params: Dict[str, Any]) -> str:
        """构建请求 URL"""
        url = base_url
        # 替换路径参数（如 {id}）
        for key, value in params.items():
            url = url.replace(f"{{{key}}}", str(value))
        return url

    def _build_headers(self, config: ApiConfig, extra_headers: Dict[str, str]) -> Dict[str, str]:
        """构建请求头"""
        headers = {
            "Content-Type": "application/json",
            **(config.headers or {}),
            **extra_headers
        }

        # 添加认证信息
        if config.auth_type == AuthType.API_KEY and config.auth_config:
            auth_config = config.auth_config
            key_name = auth_config.get("key_name", "Authorization")
            key_value = encryption.decrypt(auth_config.get("key_value", ""))
            key_prefix = auth_config.get("key_prefix", "")

            if key_prefix:
                headers[key_name] = f"{key_prefix} {key_value}"
            else:
                headers[key_name] = key_value

        elif config.auth_type == AuthType.BASIC_AUTH and config.auth_config:
            auth_config = config.auth_config
            username = encryption.decrypt(auth_config.get("username", ""))
            password = encryption.decrypt(auth_config.get("password", ""))

            import base64
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"

        return headers

    async def _log_call(
        self,
        api_config_id: int,
        mcp_call_id: Optional[str],
        request_body: Optional[str],
        response_code: Optional[int],
        response_body: Optional[str],
        error_message: Optional[str],
        execution_time: int
    ):
        """记录调用日志"""
        try:
            with next(get_db()) as db:
                log = ApiCallLog(
                    api_config_id=api_config_id,
                    mcp_call_id=mcp_call_id,
                    request_body=request_body,
                    response_code=response_code,
                    response_body=response_body,
                    error_message=error_message,
                    execution_time=execution_time
                )
                db.add(log)
                db.commit()
        except Exception as e:
            logger.error(f"记录日志失败: {e}")

# 全局实例
api_caller = ApiCaller()
```

### Step 3: 提交 API 调用服务

```bash
git add services/api_caller.py
git commit -m "feat(services): 添加 API 调用服务

- 支持 GET/POST/PUT/DELETE/PATCH
- 支持 API Key 和 Basic Auth 认证
- 自动记录调用日志
- 超时和错误处理
```

---

## Task 5: MCP 动态注册服务

**文件:**
- 创建: `services/mcp_registry.py`

### Step 1: 编写 MCP 注册服务

创建: `services/mcp_registry.py`

```python
from typing import Dict, List, Optional
from fastmcp import FastMCP
from models.api_config import ApiConfig
from utils.logger import logger
import asyncio

class MCPRegistry:
    """MCP 工具动态注册服务"""

    def __init__(self, mcp: FastMCP):
        self.mcp = mcp
        self.registered_tools: Dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def register_tool(self, config: ApiConfig) -> bool:
        """
        动态注册 MCP 工具

        Args:
            config: API 配置

        Returns:
            是否注册成功
        """
        async with self._lock:
            try:
                tool_name = f"api_{config.id}_{config.name.lower().replace(' ', '_')}"

                # 检查是否已注册
                if config.id in self.registered_tools:
                    logger.info(f"工具 {tool_name} 已注册，跳过")
                    return True

                # 动态创建工具函数
                async def api_tool(**kwargs):
                    """动态生成的 API 工具"""
                    from services.api_caller import api_caller
                    return await api_caller.call(config, kwargs)

                # 设置工具元数据
                api_tool.__name__ = tool_name
                api_tool.__doc__ = f"""
调用 {config.name} API

{config.description or ''}

接口地址: {config.url}
请求方法: {config.method.value}

参数:
{self._format_params(config)}
                """.strip()

                # 注册工具
                self.mcp.tool()(api_tool)

                self.registered_tools[config.id] = tool_name
                logger.info(f"成功注册 MCP 工具: {tool_name}")
                return True

            except Exception as e:
                logger.error(f"注册 MCP 工具失败: {config.name}, {e}")
                return False

    async def unregister_tool(self, config_id: int):
        """
        注销 MCP 工具

        Args:
            config_id: API 配置 ID
        """
        async with self._lock:
            if config_id in self.registered_tools:
                tool_name = self.registered_tools[config_id]
                # 注意: FastMCP 可能不支持直接注销
                # 这里只是从记录中移除
                del self.registered_tools[config_id]
                logger.info(f"已注销 MCP 工具: {tool_name}")

    async def reload_tools(self, configs: List[ApiConfig]):
        """
        重新加载所有工具

        Args:
            configs: 所有启用的配置列表
        """
        async with self._lock:
            # 清除所有已注册的工具
            self.registered_tools.clear()

            # 重新注册
            for config in configs:
                if config.enabled:
                    await self.register_tool(config)

            logger.info(f"已重新加载 {len(self.registered_tools)} 个 MCP 工具")

    def _format_params(self, config: ApiConfig) -> str:
        """格式化参数说明"""
        parts = []

        if config.params:
            for key, value in config.params.items():
                parts.append(f"  - {key}: (URL参数) {value}")

        if config.body_schema:
            for key, value in config.body_schema.items():
                parts.append(f"  - {key}: (请求体) {value}")

        return "\n".join(parts) if parts else "  无参数"

# 全局实例（在 main.py 中初始化）
mcp_registry: Optional[MCPRegistry] = None
```

### Step 2: 更新 utils/mcp.py 导出

修改: `utils/mcp.py`

```python
from fastmcp import FastMCP

# 创建统一的 MCP 服务器实例
mcp = FastMCP("IntelliBridge")
```

### Step 3: 提交 MCP 注册服务

```bash
git add services/mcp_registry.py
git commit -m "feat(services): 添加 MCP 动态注册服务

- 动态注册 API 配置为 MCP 工具
- 支持并发安全
- 自动生成工具文档
```

---

## Task 6: 配置管理服务

**文件:**
- 创建: `services/config_service.py`

### Step 1: 编写配置管理服务

创建: `services/config_service.py`

```python
from typing import List, Optional
from sqlalchemy.orm import Session
from models.project import Project
from models.api_config import ApiConfig
from schemas.project import ProjectCreate, ProjectUpdate
from schemas.api_config import ApiConfigCreate, ApiConfigUpdate
from utils.encryption import encryption
from utils.logger import logger

class ConfigService:
    """配置管理服务"""

    def __init__(self, db: Session):
        self.db = db

    # 项目管理
    def create_project(self, data: ProjectCreate) -> Project:
        """创建项目"""
        project = Project(**data.model_dump())
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        logger.info(f"创建项目: {project.name}")
        return project

    def get_project(self, project_id: int) -> Optional[Project]:
        """获取项目"""
        return self.db.query(Project).filter(Project.id == project_id).first()

    def list_projects(self) -> List[Project]:
        """列出所有项目"""
        return self.db.query(Project).order_by(Project.created_at.desc()).all()

    def update_project(self, project_id: int, data: ProjectUpdate) -> Optional[Project]:
        """更新项目"""
        project = self.get_project(project_id)
        if not project:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(project, key, value)

        self.db.commit()
        self.db.refresh(project)
        logger.info(f"更新项目: {project.name}")
        return project

    def delete_project(self, project_id: int) -> bool:
        """删除项目"""
        project = self.get_project(project_id)
        if not project:
            return False

        self.db.delete(project)
        self.db.commit()
        logger.info(f"删除项目: {project.name}")
        return True

    # API 配置管理
    def create_api_config(self, data: ApiConfigCreate) -> ApiConfig:
        """创建 API 配置"""
        # 加密敏感信息
        create_data = data.model_dump()
        if create_data.get("auth_config"):
            create_data["auth_config"] = self._encrypt_auth_config(create_data["auth_config"])

        config = ApiConfig(**create_data)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info(f"创建 API 配置: {config.name}")
        return config

    def get_api_config(self, config_id: int) -> Optional[ApiConfig]:
        """获取 API 配置"""
        return self.db.query(ApiConfig).filter(ApiConfig.id == config_id).first()

    def list_api_configs(self, project_id: Optional[int] = None, enabled_only: bool = False) -> List[ApiConfig]:
        """列出 API 配置"""
        query = self.db.query(ApiConfig)

        if project_id:
            query = query.filter(ApiConfig.project_id == project_id)

        if enabled_only:
            query = query.filter(ApiConfig.enabled == True)

        return query.order_by(ApiConfig.created_at.desc()).all()

    def update_api_config(self, config_id: int, data: ApiConfigUpdate) -> Optional[ApiConfig]:
        """更新 API 配置"""
        config = self.get_api_config(config_id)
        if not config:
            return None

        update_data = data.model_dump(exclude_unset=True)

        # 加密敏感信息
        if "auth_config" in update_data and update_data["auth_config"]:
            update_data["auth_config"] = self._encrypt_auth_config(update_data["auth_config"])

        for key, value in update_data.items():
            setattr(config, key, value)

        self.db.commit()
        self.db.refresh(config)
        logger.info(f"更新 API 配置: {config.name}")
        return config

    def delete_api_config(self, config_id: int) -> bool:
        """删除 API 配置"""
        config = self.get_api_config(config_id)
        if not config:
            return False

        self.db.delete(config)
        self.db.commit()
        logger.info(f"删除 API 配置: {config.name}")
        return True

    def _encrypt_auth_config(self, auth_config: dict) -> dict:
        """加密认证配置中的敏感信息"""
        encrypted = auth_config.copy()

        if encrypted.get("key_value"):
            encrypted["key_value"] = encryption.encrypt(encrypted["key_value"])

        if encrypted.get("username"):
            encrypted["username"] = encryption.encrypt(encrypted["username"])

        if encrypted.get("password"):
            encrypted["password"] = encryption.encrypt(encrypted["password"])

        return encrypted
```

### Step 2: 提交配置管理服务

```bash
git add services/config_service.py
git commit -m "feat(services): 添加配置管理服务

- 项目的 CRUD 操作
- API 配置的 CRUD 操作
- 敏感信息自动加密
```

---

## Task 7: REST API 路由

**文件:**
- 创建: `api/__init__.py`
- 创建: `api/projects.py`
- 创建: `api/configs.py`
- 创建: `api/logs.py`

### Step 1: 创建 API 包

```bash
mkdir -p api && touch api/__init__.py
```

### Step 2: 编写项目 API

创建: `api/projects.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from utils.database import get_db
from services.config_service import ConfigService
from schemas.project import ProjectCreate, ProjectUpdate, ProjectResponse

router = APIRouter(prefix="/api/projects", tags=["项目管理"])

@router.post("/", response_model=ProjectResponse)
async def create_project(
    data: ProjectCreate,
    db: Session = Depends(get_db)
):
    """创建项目"""
    service = ConfigService(db)
    try:
        project = service.create_project(data)
        return project
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/", response_model=List[ProjectResponse])
async def list_projects(db: Session = Depends(get_db)):
    """列出所有项目"""
    service = ConfigService(db)
    return service.list_projects()

@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    db: Session = Depends(get_db)
):
    """获取项目详情"""
    service = ConfigService(db)
    project = service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project

@router.put("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    data: ProjectUpdate,
    db: Session = Depends(get_db)
):
    """更新项目"""
    service = ConfigService(db)
    project = service.update_project(project_id, data)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project

@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    db: Session = Depends(get_db)
):
    """删除项目"""
    service = ConfigService(db)
    if not service.delete_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"message": "删除成功"}
```

### Step 3: 编写配置 API

创建: `api/configs.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from utils.database import get_db
from services.config_service import ConfigService
from services.api_caller import api_caller
from schemas.api_config import ApiConfigCreate, ApiConfigUpdate, ApiConfigResponse, ApiConfigTestRequest

router = APIRouter(prefix="/api/configs", tags=["接口配置"])

@router.post("/", response_model=ApiConfigResponse)
async def create_api_config(
    data: ApiConfigCreate,
    db: Session = Depends(get_db)
):
    """创建接口配置"""
    service = ConfigService(db)
    try:
        config = service.create_api_config(data)
        # 触发 MCP 工具重新加载
        await trigger_mcp_reload()
        return config
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/", response_model=List[ApiConfigResponse])
async def list_api_configs(
    project_id: Optional[int] = None,
    enabled_only: bool = False,
    db: Session = Depends(get_db)
):
    """列出接口配置"""
    service = ConfigService(db)
    return service.list_api_configs(project_id, enabled_only)

@router.get("/{config_id}", response_model=ApiConfigResponse)
async def get_api_config(
    config_id: int,
    db: Session = Depends(get_db)
):
    """获取接口配置详情"""
    service = ConfigService(db)
    config = service.get_api_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")
    return config

@router.put("/{config_id}", response_model=ApiConfigResponse)
async def update_api_config(
    config_id: int,
    data: ApiConfigUpdate,
    db: Session = Depends(get_db)
):
    """更新接口配置"""
    service = ConfigService(db)
    config = service.update_api_config(config_id, data)
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")
    # 触发 MCP 工具重新加载
    await trigger_mcp_reload()
    return config

@router.delete("/{config_id}")
async def delete_api_config(
    config_id: int,
    db: Session = Depends(get_db)
):
    """删除接口配置"""
    service = ConfigService(db)
    if not service.delete_api_config(config_id):
        raise HTTPException(status_code=404, detail="配置不存在")
    # 触发 MCP 工具重新加载
    await trigger_mcp_reload()
    return {"message": "删除成功"}

@router.post("/{config_id}/test")
async def test_api_config(
    config_id: int,
    data: ApiConfigTestRequest,
    db: Session = Depends(get_db)
):
    """测试接口配置"""
    service = ConfigService(db)
    config = service.get_api_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")

    result = await api_caller.call(
        config,
        data.params or {},
        data.headers or {}
    )
    return result

async def trigger_mcp_reload():
    """触发 MCP 工具重新加载"""
    # 这里需要通知 MCP 注册服务重新加载
    # 可以使用事件或消息队列实现
    pass
```

### Step 4: 编写日志 API

创建: `api/logs.py`

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from utils.database import get_db
from models.api_log import ApiCallLog
from schemas.api_log import ApiCallLogListResponse

router = APIRouter(prefix="/api/logs", tags=["调用日志"])

@router.get("/", response_model=ApiCallLogListResponse)
async def list_call_logs(
    config_id: Optional[int] = Query(None, description="接口配置ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    db: Session = Depends(get_db)
):
    """查询调用日志"""
    query = db.query(ApiCallLog)

    if config_id:
        query = query.filter(ApiCallLog.api_config_id == config_id)

    total = query.count()
    logs = query.order_by(ApiCallLog.created_at.desc()) \
             .offset((page - 1) * page_size) \
             .limit(page_size) \
             .all()

    return ApiCallLogListResponse(total=total, items=logs)
```

### Step 5: 更新 main.py 注册路由

修改: `main.py`

```python
from api.projects import router as projects_router
from api.configs import router as configs_router
from api.logs import router as logs_router

# ... 其他代码 ...

# 注册路由
app.include_router(hive_router)
app.include_router(agent_browser_router)
app.include_router(projects_router)
app.include_router(configs_router)
app.include_router(logs_router)
```

### Step 6: 提交 REST API

```bash
git add api/
git add main.py
git commit -m "feat(api): 添加 REST API 路由

- 项目管理 API
- 接口配置 CRUD API
- 调用日志查询 API
- 接口测试功能
```

---

## Task 8: Jinja2 模板 - 基础布局

**文件:**
- 创建: `templates/layout.html`
- 创建: `static/css/style.css`

### Step 1: 编写基础布局模板

创建: `templates/layout.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}IntelliBridge API 配置{% endblock %}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="/static/css/style.css">
    {% block head %}{% endblock %}
</head>
<body class="bg-slate-50 min-h-screen">
    <!-- 顶部导航 -->
    <nav class="bg-white border-b border-slate-200">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex items-center">
                    <h1 class="text-xl font-semibold text-slate-800">IntelliBridge</h1>
                </div>
                <div class="flex items-center space-x-4">
                    <a href="/configs" class="text-slate-600 hover:text-slate-900 text-sm">接口配置</a>
                    <a href="/logs" class="text-slate-600 hover:text-slate-900 text-sm">调用日志</a>
                </div>
            </div>
        </div>
    </nav>

    <!-- 主内容 -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {% block content %}{% endblock %}
    </main>

    {% block scripts %}{% endblock %}
</body>
</html>
```

### Step 2: 编写自定义样式

创建: `static/css/style.css`

```css
/* 颜色变量 */
:root {
    --primary: #64748b;
    --primary-hover: #475569;
    --border: #e2e8f0;
    --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
    --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);
}

/* 基础样式 */
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

/* 卡片样式 */
.card {
    background: white;
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: var(--shadow-sm);
}

/* 按钮样式 */
.btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 500;
    transition: all 0.15s ease;
    cursor: pointer;
    border: none;
}

.btn-primary {
    background: var(--primary);
    color: white;
}

.btn-primary:hover {
    background: var(--primary-hover);
}

.btn-secondary {
    background: white;
    color: #64748b;
    border: 1px solid var(--border);
}

.btn-secondary:hover {
    background: #f8fafc;
}

/* 表格样式 */
.table {
    width: 100%;
    border-collapse: collapse;
}

.table th,
.table td {
    padding: 12px 16px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}

.table th {
    background: #f8fafc;
    font-weight: 500;
    font-size: 14px;
    color: #64748b;
}

.table td {
    font-size: 14px;
}

.table tr:hover {
    background: #f8fafc;
}

/* 表单样式 */
.form-group {
    margin-bottom: 16px;
}

.form-label {
    display: block;
    margin-bottom: 6px;
    font-size: 14px;
    font-weight: 500;
    color: #334155;
}

.form-input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 14px;
    transition: border-color 0.15s ease;
}

.form-input:focus {
    outline: none;
    border-color: var(--primary);
}

/* 状态标签 */
.badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
}

.badge-success {
    background: #dcfce7;
    color: #166534;
}

.badge-danger {
    background: #fee2e2;
    color: #991b1b;
}
```

### Step 3: 提交基础模板

```bash
git add templates/ static/
git commit -m "feat(ui): 添加基础布局模板

- 简约的导航栏
- 克制的颜色方案
- 响应式布局
```

---

## Task 9: 配置列表页面

**文件:**
- 创建: `templates/configs/list.html`
- 修改: `main.py` 添加路由

### Step 1: 编写配置列表模板

创建: `templates/configs/list.html`

```html
{% extends "layout.html" %}

{% block title %}接口配置 - IntelliBridge{% endblock %}

{% block content %}
<div class="mb-6">
    <div class="flex justify-between items-center">
        <h2 class="text-2xl font-semibold text-slate-800">接口配置</h2>
        <a href="/configs/new" class="btn btn-primary">新建配置</a>
    </div>
</div>

<div class="card">
    <table class="table">
        <thead>
            <tr>
                <th>名称</th>
                <th>项目</th>
                <th>方法</th>
                <th>地址</th>
                <th>认证</th>
                <th>状态</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody>
            {% for config in configs %}
            <tr>
                <td>
                    <div class="font-medium text-slate-900">{{ config.name }}</div>
                    {% if config.description %}
                    <div class="text-sm text-slate-500">{{ config.description }}</div>
                    {% endif %}
                </td>
                <td>{{ config.project.name }}</td>
                <td>
                    <span class="badge" style="background: #e0f2fe; color: #0369a1;">
                        {{ config.method }}
                    </span>
                </td>
                <td class="font-mono text-sm">{{ config.url }}</td>
                <td>
                    {% if config.auth_type == 'none' %}
                    <span class="text-slate-500">无</span>
                    {% elif config.auth_type == 'api_key' %}
                    <span class="text-slate-700">API Key</span>
                    {% else %}
                    <span class="text-slate-700">Basic Auth</span>
                    {% endif %}
                </td>
                <td>
                    {% if config.enabled %}
                    <span class="badge badge-success">启用</span>
                    {% else %}
                    <span class="badge badge-danger">禁用</span>
                    {% endif %}
                </td>
                <td>
                    <div class="flex space-x-2">
                        <a href="/configs/{{ config.id }}/test" class="text-blue-600 hover:text-blue-800 text-sm">测试</a>
                        <a href="/configs/{{ config.id }}/edit" class="text-blue-600 hover:text-blue-800 text-sm">编辑</a>
                        <button onclick="deleteConfig({{ config.id }})" class="text-red-600 hover:text-red-800 text-sm">删除</button>
                    </div>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    {% if not configs %}
    <div class="text-center py-12">
        <p class="text-slate-500">暂无配置</p>
        <a href="/configs/new" class="btn btn-primary mt-4">创建第一个配置</a>
    </div>
    {% endif %}
</div>
{% endblock %}

{% block scripts %}
<script>
async function deleteConfig(id) {
    if (!confirm('确定要删除这个配置吗？')) {
        return;
    }

    try {
        const response = await fetch(`/api/configs/${id}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            window.location.reload();
        } else {
            alert('删除失败');
        }
    } catch (error) {
        alert('删除失败: ' + error);
    }
}
</script>
{% endblock %}
```

### Step 2: 添加路由到 main.py

修改: `main.py`

```python
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from utils.database import get_db
from services.config_service import ConfigService

# 配置 Jinja2
templates = Jinja2Templates(directory="templates")

@app.get("/configs", response_class=HTMLResponse)
async def list_configs(request: Request, db: Session = Depends(get_db)):
    """配置列表页面"""
    service = ConfigService(db)
    configs = service.list_api_configs()
    return templates.TemplateResponse(
        "configs/list.html",
        {"request": request, "configs": configs}
    )
```

### Step 3: 提交配置列表页面

```bash
git add templates/configs/ main.py
git commit -m "feat(ui): 添加配置列表页面

- 表格展示所有配置
- 状态标签
- 快速操作链接
```

---

## Task 10: 配置表单页面

**文件:**
- 创建: `templates/configs/form.html`

### Step 1: 编写配置表单模板

创建: `templates/configs/form.html`

```html
{% extends "layout.html" %}

{% block title %}{{ '新建' if is_new else '编辑' }}配置 - IntelliBridge{% endblock %}

{% block content %}
<div class="mb-6">
    <h2 class="text-2xl font-semibold text-slate-800">{{ '新建' if is_new else '编辑' }}接口配置</h2>
</div>

<div class="card max-w-3xl">
    <form id="configForm" class="p-6">
        <div class="grid grid-cols-2 gap-6">
            <!-- 基本信息 -->
            <div class="col-span-2">
                <h3 class="text-lg font-medium text-slate-800 mb-4">基本信息</h3>
            </div>

            <div class="form-group">
                <label class="form-label">接口名称 *</label>
                <input type="text" name="name" class="form-input" required value="{{ config.name if config else '' }}">
            </div>

            <div class="form-group">
                <label class="form-label">所属项目 *</label>
                <select name="project_id" class="form-input" required>
                    <option value="">请选择项目</option>
                    {% for project in projects %}
                    <option value="{{ project.id }}" {{ 'selected' if config and config.project_id == project.id else '' }}>
                        {{ project.name }}
                    </option>
                    {% endfor %}
                </select>
            </div>

            <div class="col-span-2 form-group">
                <label class="form-label">接口描述</label>
                <textarea name="description" class="form-input" rows="2">{{ config.description if config else '' }}</textarea>
            </div>

            <!-- 请求配置 -->
            <div class="col-span-2 mt-4">
                <h3 class="text-lg font-medium text-slate-800 mb-4">请求配置</h3>
            </div>

            <div class="form-group">
                <label class="form-label">请求方法 *</label>
                <select name="method" class="form-input" required>
                    <option value="GET" {{ 'selected' if config and config.method == 'GET' else '' }}>GET</option>
                    <option value="POST" {{ 'selected' if config and config.method == 'POST' else '' }}>POST</option>
                    <option value="PUT" {{ 'selected' if config and config.method == 'PUT' else '' }}>PUT</option>
                    <option value="DELETE" {{ 'selected' if config and config.method == 'DELETE' else '' }}>DELETE</option>
                    <option value="PATCH" {{ 'selected' if config and config.method == 'PATCH' else '' }}>PATCH</option>
                </select>
            </div>

            <div class="form-group">
                <label class="form-label">请求地址 *</label>
                <input type="text" name="url" class="form-input" required placeholder="https://api.example.com/users/{id}" value="{{ config.url if config else '' }}">
            </div>

            <!-- 认证配置 -->
            <div class="col-span-2 mt-4">
                <h3 class="text-lg font-medium text-slate-800 mb-4">认证配置</h3>
            </div>

            <div class="col-span-2 form-group">
                <label class="form-label">认证方式</label>
                <select name="auth_type" class="form-input" id="authTypeSelect">
                    <option value="none" {{ 'selected' if not config or config.auth_type == 'none' else '' }}>无认证</option>
                    <option value="api_key" {{ 'selected' if config and config.auth_type == 'api_key' else '' }}>API Key</option>
                    <option value="basic_auth" {{ 'selected' if config and config.auth_type == 'basic_auth' else '' }}>Basic Auth</option>
                </select>
            </div>

            <!-- API Key 配置 -->
            <div id="apiKeyConfig" style="display: none;">
                <div class="form-group">
                    <label class="form-label">Key 名称</label>
                    <input type="text" name="api_key_name" class="form-input" placeholder="Authorization">
                </div>
                <div class="form-group">
                    <label class="form-label">Key 值</label>
                    <input type="password" name="api_key_value" class="form-input" placeholder="your-api-key">
                </div>
                <div class="form-group">
                    <label class="form-label">Key 前缀</label>
                    <input type="text" name="api_key_prefix" class="form-input" placeholder="Bearer">
                </div>
            </div>

            <!-- Basic Auth 配置 -->
            <div id="basicAuthConfig" style="display: none;" class="col-span-2">
                <div class="grid grid-cols-2 gap-4">
                    <div class="form-group">
                        <label class="form-label">用户名</label>
                        <input type="text" name="basic_username" class="form-input">
                    </div>
                    <div class="form-group">
                        <label class="form-label">密码</label>
                        <input type="password" name="basic_password" class="form-input">
                    </div>
                </div>
            </div>

            <!-- 状态 -->
            <div class="col-span-2 mt-4 form-group">
                <label class="flex items-center">
                    <input type="checkbox" name="enabled" {{ 'checked' if not config or config.enabled else '' }}>
                    <span class="ml-2 text-slate-700">启用此配置</span>
                </label>
            </div>
        </div>

        <div class="flex justify-end space-x-3 mt-6 pt-6 border-t border-slate-200">
            <a href="/configs" class="btn btn-secondary">取消</a>
            <button type="submit" class="btn btn-primary">保存</button>
        </div>
    </form>
</div>
{% endblock %}

{% block scripts %}
<script>
// 认证类型切换
const authTypeSelect = document.getElementById('authTypeSelect');
const apiKeyConfig = document.getElementById('apiKeyConfig');
const basicAuthConfig = document.getElementById('basicAuthConfig');

authTypeSelect.addEventListener('change', function() {
    apiKeyConfig.style.display = this.value === 'api_key' ? 'block' : 'none';
    basicAuthConfig.style.display = this.value === 'basic_auth' ? 'block' : 'none';
});

// 表单提交
document.getElementById('configForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const formData = new FormData(this);
    const data = {
        name: formData.get('name'),
        project_id: parseInt(formData.get('project_id')),
        description: formData.get('description') || null,
        method: formData.get('method'),
        url: formData.get('url'),
        auth_type: formData.get('auth_type'),
        enabled: formData.get('enabled') === 'on'
    };

    // 认证配置
    if (data.auth_type === 'api_key') {
        data.auth_config = {
            key_name: formData.get('api_key_name'),
            key_value: formData.get('api_key_value'),
            key_prefix: formData.get('api_key_prefix')
        };
    } else if (data.auth_type === 'basic_auth') {
        data.auth_config = {
            username: formData.get('basic_username'),
            password: formData.get('basic_password')
        };
    }

    try {
        const url = {{ 'null' if is_new else config.id }};
        const method = 'POST';
        const fullUrl = url ? `/api/configs/${url}` : '/api/configs';

        const response = await fetch(fullUrl, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            window.location.href = '/configs';
        } else {
            const error = await response.json();
            alert('保存失败: ' + (error.detail || '未知错误'));
        }
    } catch (error) {
        alert('保存失败: ' + error);
    }
});

// 初始化认证配置显示
authTypeSelect.dispatchEvent(new Event('change'));
</script>
{% endblock %}
```

### Step 2: 添加路由到 main.py

修改: `main.py` 添加以下路由

```python
@app.get("/configs/new", response_class=HTMLResponse)
async def new_config_form(request: Request, db: Session = Depends(get_db)):
    """新建配置表单"""
    service = ConfigService(db)
    projects = service.list_projects()
    return templates.TemplateResponse(
        "configs/form.html",
        {"request": request, "projects": projects, "config": None, "is_new": True}
    )

@app.get("/configs/{config_id}/edit", response_class=HTMLResponse)
async def edit_config_form(config_id: int, request: Request, db: Session = Depends(get_db)):
    """编辑配置表单"""
    service = ConfigService(db)
    config = service.get_api_config(config_id)
    projects = service.list_projects()
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")
    return templates.TemplateResponse(
        "configs/form.html",
        {"request": request, "projects": projects, "config": config, "is_new": False}
    )
```

### Step 3: 提交配置表单页面

```bash
git add templates/configs/form.html main.py
git commit -m "feat(ui): 添加配置表单页面

- 新建/编辑配置
- 动态认证配置表单
- 表单验证
```

---

## Task 11: 测试页面

**文件:**
- 创建: `templates/configs/test.html`

### Step 1: 编写测试页面模板

创建: `templates/configs/test.html`

```html
{% extends "layout.html" %}

{% block title %}测试接口 - {{ config.name }}{% endblock %}

{% block content %}
<div class="mb-6">
    <div class="flex justify-between items-center">
        <div>
            <h2 class="text-2xl font-semibold text-slate-800">测试接口</h2>
            <p class="text-slate-600 mt-1">{{ config.name }} - {{ config.method }} {{ config.url }}</p>
        </div>
        <a href="/configs" class="btn btn-secondary">返回</a>
    </div>
</div>

<div class="grid grid-cols-2 gap-6">
    <!-- 请求参数 -->
    <div class="card">
        <div class="p-6">
            <h3 class="text-lg font-medium text-slate-800 mb-4">请求参数</h3>

            {% if config.params %}
            <div class="mb-4">
                <p class="text-sm text-slate-600 mb-2">URL 参数:</p>
                {% for key, value in config.params.items() %}
                <div class="form-group">
                    <label class="form-label">{{ key }}</label>
                    <input type="text" class="form-input param-input" data-type="url" data-name="{{ key }}" value="">
                </div>
                {% endfor %}
            </div>
            {% endif %}

            {% if config.body_schema %}
            <div class="mb-4">
                <p class="text-sm text-slate-600 mb-2">请求体参数:</p>
                {% for key, value in config.body_schema.items() %}
                <div class="form-group">
                    <label class="form-label">{{ key }}</label>
                    <input type="text" class="form-input param-input" data-type="body" data-name="{{ key }}" value="">
                </div>
                {% endfor %}
            </div>
            {% endif %}

            <button id="sendBtn" class="btn btn-primary w-full">发送请求</button>
        </div>
    </div>

    <!-- 响应结果 -->
    <div class="card">
        <div class="p-6">
            <h3 class="text-lg font-medium text-slate-800 mb-4">响应结果</h3>

            <div id="responseInfo" class="mb-4" style="display: none;">
                <div class="flex items-center space-x-4 text-sm">
                    <span>状态码: <strong id="statusCode"></strong></span>
                    <span>耗时: <strong id="executionTime"></strong>ms</span>
                </div>
            </div>

            <div id="responseBody" class="bg-slate-50 rounded p-4 min-h-64 font-mono text-sm">
                <p class="text-slate-400">等待发送请求...</p>
            </div>

            <div id="errorMessage" class="mt-4 p-4 bg-red-50 text-red-700 rounded" style="display: none;"></div>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
document.getElementById('sendBtn').addEventListener('click', async function() {
    const btn = this;
    btn.disabled = true;
    btn.textContent = '发送中...';

    try {
        // 收集参数
        const params = {};
        document.querySelectorAll('.param-input').forEach(input => {
            if (input.value) {
                params[input.dataset.name] = input.value;
            }
        });

        const response = await fetch(`/api/configs/{{ config.id }}/test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ params: params })
        });

        const result = await response.json();

        // 显示响应信息
        document.getElementById('responseInfo').style.display = 'block';
        document.getElementById('statusCode').textContent = result.status_code || 'N/A';
        document.getElementById('executionTime').textContent = result.execution_time || 'N/A';

        // 显示响应体
        const responseBody = document.getElementById('responseBody');
        const errorMessage = document.getElementById('errorMessage');

        if (result.success) {
            responseBody.textContent = JSON.stringify(result.data, null, 2);
            errorMessage.style.display = 'none';
        } else {
            responseBody.textContent = '';
            errorMessage.textContent = result.error || '请求失败';
            errorMessage.style.display = 'block';
        }

    } catch (error) {
        alert('请求失败: ' + error);
    } finally {
        btn.disabled = false;
        btn.textContent = '发送请求';
    }
});
</script>
{% endblock %}
```

### Step 2: 添加测试路由

修改: `main.py`

```python
@app.get("/configs/{config_id}/test", response_class=HTMLResponse)
async def test_config_page(config_id: int, request: Request, db: Session = Depends(get_db)):
    """测试接口页面"""
    service = ConfigService(db)
    config = service.get_api_config(config_id)
    if not config:
        raise HTTPException(status_code=404, detail="配置不存在")
    return templates.TemplateResponse(
        "configs/test.html",
        {"request": request, "config": config}
    )
```

### Step 3: 提交测试页面

```bash
git add templates/configs/test.html main.py
git commit -m "feat(ui): 添加接口测试页面

- 动态生成参数表单
- 实时显示响应结果
- 错误处理
```

---

## Task 12: 日志查询页面

**文件:**
- 创建: `templates/logs/list.html`

### Step 1: 编写日志列表模板

创建: `templates/logs/list.html`

```html
{% extends "layout.html" %}

{% block title %}调用日志 - IntelliBridge{% endblock %}

{% block content %}
<div class="mb-6">
    <h2 class="text-2xl font-semibold text-slate-800">调用日志</h2>
</div>

<div class="card mb-6">
    <div class="p-4">
        <form id="filterForm" class="flex items-center space-x-4">
            <div>
                <label class="form-label">接口配置</label>
                <select name="config_id" class="form-input">
                    <option value="">全部</option>
                    {% for config in configs %}
                    <option value="{{ config.id }}">{{ config.name }}</option>
                    {% endfor %}
                </select>
            </div>
            <button type="submit" class="btn btn-primary self-end">查询</button>
        </form>
    </div>
</div>

<div class="card">
    <table class="table">
        <thead>
            <tr>
                <th>时间</th>
                <th>接口</th>
                <th>状态码</th>
                <th>耗时</th>
                <th>错误</th>
            </tr>
        </thead>
        <tbody>
            {% for log in logs.items %}
            <tr>
                <td class="text-sm">{{ log.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</td>
                <td>
                    <div class="font-medium text-slate-900">
                        {{ log.api_config.name }}
                    </div>
                    <div class="text-sm text-slate-500">
                        {{ log.api_config.method }} {{ log.api_config.url }}
                    </div>
                </td>
                <td>
                    {% if log.response_code %}
                    {% if log.response_code < 400 %}
                    <span class="badge badge-success">{{ log.response_code }}</span>
                    {% else %}
                    <span class="badge badge-danger">{{ log.response_code }}</span>
                    {% endif %}
                    {% else %}
                    <span class="text-slate-400">-</span>
                    {% endif %}
                </td>
                <td>
                    {% if log.execution_time %}
                    <span class="{% if log.execution_time > 3000 %}text-red-600{% elif log.execution_time > 1000 %}text-yellow-600{% else %}text-slate-700{% endif %}">
                        {{ log.execution_time }}ms
                    </span>
                    {% else %}
                    <span class="text-slate-400">-</span>
                    {% endif %}
                </td>
                <td>
                    {% if log.error_message %}
                    <span class="text-red-600 text-sm">{{ log.error_message[:50] }}...</span>
                    {% else %}
                    <span class="text-slate-400">-</span>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    {% if not logs.items %}
    <div class="text-center py-12">
        <p class="text-slate-500">暂无日志</p>
    </div>
    {% endif %}
</div>

<!-- 分页 -->
{% if logs.total > 0 %}
<div class="flex justify-between items-center mt-6">
    <p class="text-sm text-slate-600">
        共 {{ logs.total }} 条记录
    </p>
    <div class="flex space-x-2">
        {% if page > 1 %}
        <a href="?page={{ page - 1 }}" class="btn btn-secondary">上一页</a>
        {% endif %}
        {% if logs.total > page * page_size %}
        <a href="?page={{ page + 1 }}" class="btn btn-secondary">下一页</a>
        {% endif %}
    </div>
</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
document.getElementById('filterForm').addEventListener('submit', function(e) {
    e.preventDefault();
    const formData = new FormData(this);
    const params = new URLSearchParams(formData);
    window.location.href = '?' + params.toString();
});
</script>
{% endblock %}
```

### Step 2: 添加日志路由

修改: `main.py`

```python
@app.get("/logs", response_class=HTMLResponse)
async def list_logs(
    request: Request,
    config_id: Optional[int] = None,
    page: int = 1,
    db: Session = Depends(get_db)
):
    """调用日志页面"""
    service = ConfigService(db)
    configs = service.list_api_configs()

    from api.logs import router as logs_router
    # 复用 API 逻辑
    page_size = 20
    query = db.query(ApiCallLog)

    if config_id:
        query = query.filter(ApiCallLog.api_config_id == config_id)

    total = query.count()
    logs = query.order_by(ApiCallLog.created_at.desc()) \
             .offset((page - 1) * page_size) \
             .limit(page_size) \
             .all()

    return templates.TemplateResponse(
        "logs/list.html",
        {
            "request": request,
            "configs": configs,
            "logs": {"items": logs, "total": total},
            "page": page,
            "page_size": page_size
        }
    )
```

### Step 3: 提交日志页面

```bash
git add templates/logs/ main.py
git commit -m "feat(ui): 添加调用日志页面

- 日志列表展示
- 按接口筛选
- 分页功能
```

---

## Task 13: MCP 工具自动加载和重载

**文件:**
- 修改: `main.py`

### Step 1: 更新 main.py 集成 MCP 注册

修改: `main.py`

```python
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from tools.hive_query import router as hive_router
from tools.agent_browser import router as agent_browser_router
from utils.mcp import mcp
from utils.logger import logger
from utils.database import init_db, get_db
from services.mcp_registry import MCPRegistry
from services.config_service import ConfigService

# MCP 注册表
mcp_registry: MCPRegistry = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化
    logger.info("初始化数据库...")
    init_db()

    logger.info("初始化 MCP 注册服务...")
    global mcp_registry
    mcp_registry = MCPRegistry(mcp)

    # 加载所有启用的配置
    with next(get_db()) as db:
        service = ConfigService(db)
        configs = service.list_api_configs(enabled_only=True)
        await mcp_registry.reload_tools(configs)
        logger.info(f"已加载 {len(configs)} 个 API 配置")

    yield

    # 关闭时清理
    logger.info("关闭服务...")

app = FastAPI(
    title="IntelliBridge API",
    description="IntelliBridge Backend Service",
    version="1.0.0",
    lifespan=lifespan
)

# ... 其他代码保持不变 ...
```

### Step 2: 添加配置变更触发重载的机制

修改: `api/configs.py`

```python
async def trigger_mcp_reload():
    """触发 MCP 工具重新加载"""
    from main import mcp_registry
    from utils.database import get_db
    from services.config_service import ConfigService

    if mcp_registry:
        with next(get_db()) as db:
            service = ConfigService(db)
            configs = service.list_api_configs(enabled_only=True)
            await mcp_registry.reload_tools(configs)
```

### Step 3: 提交 MCP 集成

```bash
git add main.py api/configs.py
git commit -m "feat(mcp): 集成 MCP 动态注册

- 应用启动时自动加载配置
- 配置变更时自动重载
- 生命周期管理
```

---

## Task 14: 首页项目列表

**文件:**
- 创建: `templates/index.html`
- 修改: `main.py`

### Step 1: 编写首页模板

创建: `templates/index.html`

```html
{% extends "layout.html" %}

{% block title %}IntelliBridge{% endblock %}

{% block content %}
<div class="mb-6">
    <div class="flex justify-between items-center">
        <h2 class="text-2xl font-semibold text-slate-800">项目列表</h2>
        <button onclick="showNewProjectModal()" class="btn btn-primary">新建项目</button>
    </div>
</div>

<div class="grid grid-cols-3 gap-6">
    {% for project in projects %}
    <div class="card">
        <div class="p-6">
            <h3 class="text-lg font-medium text-slate-800 mb-2">{{ project.name }}</h3>
            {% if project.description %}
            <p class="text-slate-600 text-sm mb-4">{{ project.description }}</p>
            {% endif %}

            <div class="flex items-center justify-between text-sm text-slate-500 mb-4">
                <span>{{ project.api_configs|length }} 个接口</span>
            </div>

            <div class="flex space-x-2">
                <a href="/configs?project_id={{ project.id }}" class="text-blue-600 hover:text-blue-800 text-sm">查看接口</a>
                <button onclick="editProject({{ project.id }}, '{{ project.name }}', '{{ project.description or '' }}')" class="text-slate-600 hover:text-slate-800 text-sm">编辑</button>
                <button onclick="deleteProject({{ project.id }})" class="text-red-600 hover:text-red-800 text-sm">删除</button>
            </div>
        </div>
    </div>
    {% endfor %}

    <!-- 新建项目卡片 -->
    <div onclick="showNewProjectModal()" class="card border-dashed cursor-pointer hover:border-slate-400 transition-colors">
        <div class="p-6 flex items-center justify-center h-full min-h-32">
            <div class="text-center text-slate-400">
                <svg class="mx-auto h-12 w-12" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path>
                </svg>
                <p class="mt-2">新建项目</p>
            </div>
        </div>
    </div>
</div>

<!-- 新建/编辑项目模态框 -->
<div id="projectModal" class="fixed inset-0 bg-black bg-opacity-50 hidden items-center justify-center">
    <div class="bg-white rounded-lg shadow-xl max-w-md w-full mx-4">
        <div class="p-6">
            <h3 id="modalTitle" class="text-lg font-medium text-slate-800 mb-4">新建项目</h3>

            <form id="projectForm">
                <input type="hidden" id="projectId">

                <div class="form-group">
                    <label class="form-label">项目名称 *</label>
                    <input type="text" id="projectName" class="form-input" required>
                </div>

                <div class="form-group">
                    <label class="form-label">项目描述</label>
                    <textarea id="projectDescription" class="form-input" rows="3"></textarea>
                </div>

                <div class="flex justify-end space-x-3 mt-6">
                    <button type="button" onclick="hideProjectModal()" class="btn btn-secondary">取消</button>
                    <button type="submit" class="btn btn-primary">保存</button>
                </div>
            </form>
        </div>
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
function showNewProjectModal() {
    document.getElementById('modalTitle').textContent = '新建项目';
    document.getElementById('projectId').value = '';
    document.getElementById('projectName').value = '';
    document.getElementById('projectDescription').value = '';
    document.getElementById('projectModal').classList.remove('hidden');
    document.getElementById('projectModal').classList.add('flex');
}

function hideProjectModal() {
    document.getElementById('projectModal').classList.add('hidden');
    document.getElementById('projectModal').classList.remove('flex');
}

function editProject(id, name, description) {
    document.getElementById('modalTitle').textContent = '编辑项目';
    document.getElementById('projectId').value = id;
    document.getElementById('projectName').value = name;
    document.getElementById('projectDescription').value = description;
    document.getElementById('projectModal').classList.remove('hidden');
    document.getElementById('projectModal').classList.add('flex');
}

async function deleteProject(id) {
    if (!confirm('确定要删除这个项目吗？项目下的所有接口配置也会被删除。')) {
        return;
    }

    try {
        const response = await fetch(`/api/projects/${id}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            window.location.reload();
        } else {
            alert('删除失败');
        }
    } catch (error) {
        alert('删除失败: ' + error);
    }
}

document.getElementById('projectForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const id = document.getElementById('projectId').value;
    const data = {
        name: document.getElementById('projectName').value,
        description: document.getElementById('projectDescription').value || null
    };

    try {
        const url = id ? `/api/projects/${id}` : '/api/projects';
        const method = id ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            window.location.reload();
        } else {
            const error = await response.json();
            alert('保存失败: ' + (error.detail || '未知错误'));
        }
    } catch (error) {
        alert('保存失败: ' + error);
    }
});
</script>
{% endblock %}
```

### Step 2: 添加首页路由

修改: `main.py`

```python
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    """首页 - 项目列表"""
    service = ConfigService(db)
    projects = service.list_projects()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "projects": projects}
    )
```

### Step 3: 提交首页

```bash
git add templates/index.html main.py
git commit -m "feat(ui): 添加首页项目列表

- 项目卡片展示
- 新建/编辑项目模态框
- 项目管理功能
```

---

## Task 15: 更新配置和文档

**文件:**
- 更新: `config/config.yaml`
- 创建: `docs/OPENAPI_CONFIG_GUIDE.md`

### Step 1: 更新配置文件

修改: `config/config.yaml`

```yaml
# 数据库配置
database:
  url: "mysql+pymysql://user:password@localhost/intellibridge"
  echo: false

# 加密配置
encryption:
  secret_key: "your-32-byte-secret-key-here-change-me"

# API 配置
api:
  cors_origins:
    - "http://localhost:8000"
    - "http://localhost:49000"
```

### Step 2: 编写使用文档

创建: `docs/OPENAPI_CONFIG_GUIDE.md`

```markdown
# OpenAPI 配置系统使用指南

## 功能概述

IntelliBridge OpenAPI 配置系统允许您通过 Web 界面动态配置 REST API 接口，并自动注册为 MCP 工具，实现即时生效的 API 调用能力。

## 快速开始

### 1. 访问界面

启动服务后，访问 http://localhost:49000 即可看到项目列表页面。

### 2. 创建项目

1. 点击"新建项目"按钮
2. 填写项目名称和描述
3. 保存

### 3. 配置接口

1. 进入项目后，点击"新建配置"
2. 填写接口信息：
   - **基本信息**: 接口名称、所属项目、描述
   - **请求配置**: HTTP 方法、请求地址
   - **认证配置**: 无认证 / API Key / Basic Auth
3. 保存后，接口自动注册为 MCP 工具

### 4. 测试接口

1. 在配置列表中点击"测试"
2. 填写请求参数
3. 点击"发送请求"查看结果

### 5. 查看日志

访问"调用日志"页面，可以查看所有接口的调用历史。

## MCP 工具使用

配置保存后，接口会自动注册为 MCP 工具，工具名称格式为：

```
api_<config_id>_<config_name>
```

AI 可以直接调用这些工具来执行实际的 HTTP 请求。

## 认证配置

### API Key

- **Key 名称**: 请求头字段名（如 `Authorization`）
- **Key 值**: API 密钥（加密存储）
- **Key 前缀**: 密钥前缀（如 `Bearer`）

示例：配置后请求头会包含 `Authorization: Bearer your-api-key`

### Basic Auth

- **用户名**: 认证用户名（加密存储）
- **密码**: 认证密码（加密存储）

## 注意事项

1. **安全**: 敏感信息（API 密钥、密码）会加密存储
2. **即时生效**: 配置保存后无需重启服务
3. **日志记录**: 所有接口调用都会记录日志
4. **超时设置**: 接口调用超时时间为 30 秒

## 技术架构

- **后端**: FastAPI + SQLAlchemy
- **前端**: Jinja2 + TailwindCSS
- **MCP**: FastMCP 动态工具注册
- **数据库**: MySQL
- **加密**: cryptography (Fernet)
```

### Step 3: 更新 requirements.txt

修改: `requirements.txt`

```text
fastapi==0.115.0
uvicorn[standard]==0.35.0
pydantic==2.11.7
python-multipart==0.0.12
loguru==0.7.2
pyyaml==6.0.2
diskcache==5.6.3
pyhive[hive_pure_sasl]==0.7.0
sqlglot==25.27.0
fastmcp==3.1.0
sqlalchemy==2.0.36
pymysql==1.1.1
cryptography==44.0.0
httpx==0.28.1
jinja2==3.1.6
```

### Step 4: 提交配置和文档

```bash
git add config/ docs/ requirements.txt
git commit -m "docs: 添加配置和使用文档

- 数据库和加密配置
- 完整的使用指南
- 更新依赖列表
```

---

## Task 16: 创建数据库表

**文件:**
- 创建: `scripts/init_db.sql`

### Step 1: 编写数据库初始化脚本

创建: `scripts/init_db.sql`

```sql
-- 创建数据库
CREATE DATABASE IF NOT EXISTS intellibridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE intellibridge;

-- 项目表
CREATE TABLE IF NOT EXISTS projects (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 接口配置表
CREATE TABLE IF NOT EXISTS api_configs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    project_id INT NOT NULL,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    method ENUM('GET', 'POST', 'PUT', 'DELETE', 'PATCH') NOT NULL,
    url VARCHAR(500) NOT NULL,
    auth_type ENUM('none', 'api_key', 'basic_auth') DEFAULT 'none',
    auth_config JSON,
    headers JSON,
    params JSON,
    body_schema JSON,
    enabled BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    INDEX idx_project (project_id),
    INDEX idx_enabled (enabled),
    INDEX idx_method (method)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 调用日志表
CREATE TABLE IF NOT EXISTS api_call_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    api_config_id INT NOT NULL,
    mcp_call_id VARCHAR(100),
    request_body TEXT,
    response_code INT,
    response_body TEXT,
    error_message TEXT,
    execution_time INT COMMENT '执行时间(毫秒)',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (api_config_id) REFERENCES api_configs(id) ON DELETE CASCADE,
    INDEX idx_api_config (api_config_id),
    INDEX idx_created_at (created_at),
    INDEX idx_response_code (response_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### Step 2: 提交初始化脚本

```bash
mkdir -p scripts
git add scripts/init_db.sql
git commit -m "feat(db): 添加数据库初始化脚本

- 创建所有必要的表
- 添加索引和外键约束
- 使用 utf8mb4 字符集
```

---

## 完成

### 测试清单

1. **数据库连接**: 确保 MySQL 配置正确，表已创建
2. **项目创建**: 测试创建、编辑、删除项目
3. **配置创建**: 测试创建各种认证类型的配置
4. **接口测试**: 测试实际的 API 调用
5. **MCP 工具**: 验证配置自动注册为 MCP 工具
6. **日志查询**: 验证调用日志正确记录
7. **即时生效**: 修改配置后验证无需重启即可生效

### 部署注意事项

1. **密钥配置**: 修改 `config/config.yaml` 中的加密密钥
2. **数据库**: 创建数据库并运行初始化脚本
3. **环境变量**: 可通过环境变量覆盖配置
4. **HTTPS**: 生产环境建议使用 HTTPS

---

**计划完成日期**: 2026-03-05
**预计工作量**: 8-10 天
**优先级**: 高
