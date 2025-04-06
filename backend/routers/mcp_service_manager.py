from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, List
from models.mcp_service_model import McpService, McpServiceModel, ServiceStatus, CreationType
from models.mcp_service_capability_model import McpServiceCapabilityModel
from utils.db import DatabaseManager
from utils.logger import log
import datetime

router = APIRouter(prefix="/api/mcp_service_manager", tags=["MCP Services"])

def get_db_manager():
    # 这里可以根据需要配置SQLite或MySQL
    # 示例使用SQLite内存数据库
    return DatabaseManager()

def get_mcp_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceModel(db_manager)

def get_mcp_service_capability_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceCapabilityModel(db_manager)

@router.post("/register")
async def register_mcp_service(mcp_service_info: dict, mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    """
    注册或修改MCP服务
    """
    mcp_service_id = mcp_service_info.get("id")
    mcp_service_name = mcp_service_info.get("name")
    if not mcp_service_name:
        log.error("注册服务失败：缺少服务名称")
        raise HTTPException(status_code=400, detail="Service name is required")
    
    current_time = datetime.datetime.now()
    
    if mcp_service_id:
        # 存在服务ID，执行修改操作
        log.info(f"开始修改服务: {mcp_service_name}")
        existing_service = mcp_service_model.get_service_by_id(mcp_service_id)
        if not existing_service:
            log.error(f"修改服务失败：服务 {mcp_service_name} 不存在")
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
        log.info(f"服务 {mcp_service_name} 修改成功")
        return {"message": f"Service {mcp_service_name} updated successfully"}
    else:
        # 不存在服务ID，执行注册操作
        log.info(f"开始注册新服务: {mcp_service_name}")
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
        log.info(f"服务 {mcp_service_name} 注册成功")
        return {"message": f"Service {mcp_service_name} registered successfully"}

@router.get("/list")
async def list_services(mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    """
    获取所有已注册服务
    """
    log.debug("获取服务列表")
    mcp_services = mcp_service_model.list_mcp_services()
    log.debug(f"获取到 {len(mcp_services)} 个服务")
    return {"services": mcp_services}

@router.delete("/delete/{service_id}")
async def delete_service(service_id: int, mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    """
    删除指定ID的服务
    """
    log.info(f"开始删除服务: {service_id}")
    service = mcp_service_model.get_service_by_id(service_id)
    if not service:
        log.error(f"删除服务失败：服务 {service_id} 不存在")
        raise HTTPException(status_code=404, detail="Service not found")
        
    success = mcp_service_model.delete_service(service_id)
    if success:
        log.info(f"服务 {service_id} 删除成功")
        return {"message": f"Service {service_id} deleted successfully"}
    else:
        log.error(f"服务 {service_id} 删除失败")
        raise HTTPException(status_code=500, detail="Failed to delete service")

@router.get("/capabilities/{service_id}")
async def get_service_capabilities(
    service_id: int,
    capability_model: McpServiceCapabilityModel = Depends(get_mcp_service_capability_model),
    service_model: McpServiceModel = Depends(get_mcp_service_model)
):
    """
    获取指定服务的能力列表
    """
    log.debug(f"获取服务 {service_id} 的能力列表")
    
    # 检查服务是否存在
    service = service_model.get_service_by_id(service_id)
    if not service:
        log.error(f"获取服务能力失败：服务 {service_id} 不存在")
        raise HTTPException(status_code=404, detail="Service not found")
    
    # 获取服务的能力列表
    capabilities = capability_model.get_capabilities_by_service_id(service_id)
    
    # 将能力列表转换为字典列表
    result = []
    for cap in capabilities:
        result.append({
            "id": cap.id,
            "name": cap.name,
            "description": cap.description,
            "cap_type": cap.cap_type,
            "parameters": cap.parameters,
            "created_tm": cap.created_tm
        })
    
    log.debug(f"获取到服务 {service_id} 的 {len(result)} 个能力")
    return {"capabilities": result}