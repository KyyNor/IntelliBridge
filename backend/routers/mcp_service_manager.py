from fastapi import APIRouter, HTTPException, Depends
from typing import Dict
from models.service_model import McpService, McpServiceModel, ServiceStatus, CreationType
from utils.db import DatabaseManager
import datetime

router = APIRouter(prefix="/api/mcp_service_manager", tags=["MCP Services"])

def get_db_manager():
    # 这里可以根据需要配置SQLite或MySQL
    # 示例使用SQLite内存数据库
    return DatabaseManager()

def get_mcp_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceModel(db_manager)

@router.post("/register")
async def register_mcp_service(mcp_service_info: dict, mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    """
    注册或修改MCP服务
    """
    mcp_service_id = mcp_service_info.get("id")
    mcp_service_name = mcp_service_info.get("name")
    if not mcp_service_name:
        raise HTTPException(status_code=400, detail="Service name is required")
    
    current_time = datetime.datetime.now(datetime.UTC)
    
    if mcp_service_id:
        # 存在服务ID，执行修改操作
        existing_service = mcp_service_model.get_service_by_id(mcp_service_id)
        if not existing_service:
            raise HTTPException(status_code=404, detail="Service not found")
            
        mcp_service = McpService(
            id=mcp_service_id,
            name=mcp_service_name,
            description=mcp_service_info.get("description"),
            endpoint=mcp_service_info.get("endpoint", ""),
            ip=mcp_service_info.get("ip"),
            port=mcp_service_info.get("port"),
            version=mcp_service_info.get("version"),
            status=ServiceStatus.OFFLINE,  # 修改后服务状态设为离线
            creation_type=existing_service.creation_type,  # 保持原有的创建类型
            status_check_tm=existing_service.status_check_tm,  # 保持原有的状态检查时间
            created_tm=existing_service.created_tm,  # 保持原有的创建时间
            updated_tm=current_time  # 更新修改时间
        )
        mcp_service_model.register_mcp_service(mcp_service)
        return {"message": f"Service {mcp_service_name} updated successfully"}
    else:
        # 不存在服务ID，执行注册操作
        creation_type = mcp_service_info.get("creation_type", CreationType.MANUAL)
        mcp_service = McpService(
            id=mcp_service_id,
            name=mcp_service_name,
            description=mcp_service_info.get("description"),
            endpoint=mcp_service_info.get("endpoint", ""),
            ip=mcp_service_info.get("ip"),
            port=mcp_service_info.get("port"),
            version=mcp_service_info.get("version"),
            status=ServiceStatus.OFFLINE,  # 新注册的服务默认为离线状态
            creation_type=creation_type,  # 从参数获取创建类型
            status_check_tm=None,  # 状态检查时间默认为None
            created_tm=current_time,  # 设置创建时间为当前时间
            updated_tm=current_time  # 设置修改时间为当前时间
        )
        mcp_service_model.register_mcp_service(mcp_service)
        return {"message": f"Service {mcp_service_name} registered successfully"}

@router.get("/list")
async def list_services(mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    """
    获取所有已注册服务
    """
    mcp_services = mcp_service_model.list_mcp_services()
    return {"services": mcp_services}