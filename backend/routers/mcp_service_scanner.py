from fastapi import APIRouter, HTTPException, Depends
from models.mcp_service_model import McpServiceModel
from services.mcp_service_scan_tasker import McpServiceScanTasker
from utils.db import DatabaseManager

router = APIRouter(prefix="/api/mcp_service_scanner", tags=["MCP Service Scanner"])

def get_db_manager():
    return DatabaseManager()

def get_mcp_service_model(db_manager: DatabaseManager = Depends(get_db_manager)):
    return McpServiceModel(db_manager)

def get_service_scan_tasker(mcp_service_model: McpServiceModel = Depends(get_mcp_service_model)):
    return McpServiceScanTasker(mcp_service_model)

@router.post("/scan/{service_id}")
async def scan_service(
    service_id: str,
    scan_tasker: McpServiceScanTasker = Depends(get_service_scan_tasker)
):
    """
    手动扫描指定服务
    """
    service = scan_tasker.mcp_service_model.get_service_by_id(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    await scan_tasker.scan_service(service)
    return {"message": f"Service {service_id} scanned successfully"} 