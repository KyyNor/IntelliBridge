import asyncio
import json
import datetime
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from models.mcp_service_model import McpService, ServiceStatus
from models.mcp_service_capability_model import McpServiceCapabilityModel, CapabilityType
from utils.logger import log

class McpServiceTester:
    def __init__(self, mcp_service_model, capability_model):
        self.mcp_service_model = mcp_service_model
        self.capability_model = capability_model
    
    async def test_capability(self, service_id, capability_id, params=None):
        """
        测试指定服务的指定能力
        
        参数:
            service_id: 服务ID
            capability_id: 能力ID
            params: 测试参数，JSON格式字符串或字典
        
        返回:
            测试结果字典
        """
        # 获取服务和能力信息
        service = self.mcp_service_model.get_service_by_id(service_id)
        if not service:
            return {"error": "Service not found"}
            
        capability = self.capability_model.get_capability_by_id(capability_id)
        if not capability:
            return {"error": "Capability not found"}
        
        # 确保服务在线
        if service.status != ServiceStatus.ONLINE:
            return {"error": "Service is offline"}
        
        # 解析参数
        if params and isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return {"error": "Invalid parameters format"}
        
        # 执行测试
        try:
            if capability.cap_type == CapabilityType.TOOL:
                result = await self._test_tool(service, capability, params)
            elif capability.cap_type == CapabilityType.RESOURCE:
                result = await self._test_resource(service, capability)
            else:
                return {"error": "Unsupported capability type"}
                
            return {
                "success": True,
                "capability": {
                    "id": capability.id,
                    "name": capability.name,
                    "type": capability.cap_type
                },
                "result": result
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "capability": {
                    "id": capability.id,
                    "name": capability.name,
                    "type": capability.cap_type
                }
            }
    
    async def _test_tool(self, service, tool, params=None):
        """测试工具能力"""
        async with sse_client(f"http://{service.ip}:{service.port}/sse") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # 调用工具
                result = await session.call_tool(tool.name, params or {})
                return result
    
    async def _test_resource(self, service, resource):
        """测试资源能力"""
        async with sse_client(f"http://{service.ip}:{service.port}/sse") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # 读取资源
                result = await session.read_resource(resource.name)
                return result