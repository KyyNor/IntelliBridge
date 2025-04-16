from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Optional
from models.mcp_service_model import McpServiceModel
from models.mcp_service_capability_model import McpServiceCapabilityModel
from services.mcp_service_tester_service import McpServiceTester
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
    mcp_service_model: McpServiceModel = Depends(get_mcp_service_model),
    capability_model: McpServiceCapabilityModel = Depends(get_capability_model)
):
    return McpServiceTester(mcp_service_model, capability_model)

@router.post("/test_capability/{service_id}/{capability_id}")
async def test_capability(
    service_id: int,
    capability_id: int,
    params: Optional[Dict] = None,
    service_tester: McpServiceTester = Depends(get_service_tester)
):
    """
    测试指定服务的指定能力
    """
    result = await service_tester.test_capability(service_id, capability_id, params)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@router.get("/capabilities/{service_id}")
async def get_service_capabilities(
    service_id: int,
    capability_model: McpServiceCapabilityModel = Depends(get_capability_model),
    mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)
):
    """
    获取指定服务的所有能力
    """
    service = mcp_service_model.get_service_by_id(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
        
    capabilities = capability_model.get_capabilities_by_service_id(service_id)
    return {"capabilities": capabilities}