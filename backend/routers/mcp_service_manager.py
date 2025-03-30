from fastapi import APIRouter, HTTPException, Depends
from typing import Dict
from models.service_model import Service, ServiceModel
from utils.db import DatabaseManager

router = APIRouter(prefix="/api/mcp_service_manager", tags=["MCP Services"])

def get_db_manager():
    # 这里可以根据需要配置SQLite或MySQL
    # 示例使用SQLite内存数据库
    return DatabaseManager()

def get_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return ServiceModel(db_manager)

@router.post("/register")
async def register_service(service_info: dict, service_model: ServiceModel = Depends(get_service_model)):
    """
    注册MCP服务
    """
    service_name = service_info.get("name")
    if not service_name:
        raise HTTPException(status_code=400, detail="Service name is required")
    
    service = Service(
        name=service_name,
        description=service_info.get("description"),
        endpoint=service_info.get("endpoint", "")
    )
    
    service_model.register_service(service)
    return {"message": f"Service {service_name} registered successfully"}

@router.get("/list")
async def list_services(service_model: ServiceModel = Depends(get_service_model)):
    """
    获取所有已注册服务
    """
    services = service_model.list_services()
    return {"services": services}