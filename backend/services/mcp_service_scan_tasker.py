import httpx
import asyncio
import datetime
import json
from typing import List, Dict
from mcp.client.sse import sse_client
from mcp import ClientSession, types
from models.mcp_service_model import McpService, ServiceStatus
from models.mcp_service_capability_model import McpServiceCapabilityModel, CapabilityType
from utils.config_manager import ConfigManager
from utils.db import DatabaseManager
from utils.logger import log

class McpServiceScanTasker:
    def __init__(self, mcp_service_model):
        self.mcp_service_model = mcp_service_model
        self.config = ConfigManager()
        self.db_manager = DatabaseManager()
        self.capability_model = McpServiceCapabilityModel(self.db_manager)
        log.info("初始化MCP服务扫描任务器")

    async def check_service_status(self, service: McpService) -> ServiceStatus:
        """检查单个服务的状态"""
        if not service.endpoint:
            log.warning(f"服务 {service.name} 没有配置端点")
            return ServiceStatus.OFFLINE

        try:
            async with sse_client(f"http://{service.ip}:{service.port}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    # Initialize the connection
                    await session.initialize()
                    prompts = await session.list_prompts()
                    # List available resources
                    resources = await session.list_resources()
                    # List available tools
                    tools = await session.list_tools()
                    log.debug(f"服务 {service.name} 可用提示词: {prompts}")
                    log.debug(f"服务 {service.name} 可用资源: {resources}")
                    log.debug(f"服务 {service.name} 可用工具: {tools}")
                    
                    # 更新服务能力信息到数据库
                    self._update_service_capabilities(service.id, tools, resources)

                    return ServiceStatus.ONLINE
        except Exception as e:
            log.error(f"服务 {service.name} 状态检查失败: {str(e)}")
            return ServiceStatus.OFFLINE

    async def scan_service(self, service: McpService) -> None:
        """扫描单个服务并更新状态"""
        log.debug(f"开始扫描服务: {service.name}")
        current_status = await self.check_service_status(service)
        current_time = datetime.datetime.now()
        
        service.status = current_status
        service.status_check_tm = current_time
        service.updated_tm = current_time
        
        self.mcp_service_model.update_service_status(service)
        log.info(f"服务 {service.name} 状态已更新为: {current_status}")

    async def scan_all_services(self) -> None:
        """扫描所有服务"""
        log.info("开始扫描所有服务")
        services = self.mcp_service_model.list_mcp_services()
        tasks = [self.scan_service(service) for service in services]
        await asyncio.gather(*tasks)
        log.info("所有服务扫描完成")

    def _update_service_capabilities(self, service_id: int, tools_result, resources_result) -> None:
        """
        更新服务的能力信息（工具和资源）到数据库
        
        参数:
            service_id: 服务ID
            tools_result: ListToolsResult类型，包含tools属性(list[Tool])
            resources_result: 资源列表
        """
        try:
            capabilities = []
            
            # 处理工具数据 - 从ListToolsResult中提取tools列表
            if tools_result:
                # 检查是否有tools属性
                tools_list = getattr(tools_result, 'tools', None)
                if tools_list and hasattr(tools_list, '__iter__'):
                    for tool in tools_list:
                        if hasattr(tool, 'name') and hasattr(tool, 'description'):
                            # 将工具的inputSchema转换为JSON字符串
                            parameters = json.dumps(tool.inputSchema) if hasattr(tool, 'inputSchema') else None
                            
                            capabilities.append({
                                'name': tool.name,
                                'description': tool.description,
                                'cap_type': CapabilityType.TOOL,
                                'parameters': parameters
                            })
            
            # 处理资源数据 - Resource对象有uri, name, description等属性
            if resources_result and hasattr(resources_result, '__iter__'):
                for resource in resources_result:
                    # 检查Resource对象的必要属性
                    if hasattr(resource, 'name'):
                        description = getattr(resource, 'description', None)
                        uri = getattr(resource, 'uri', None)
                        
                        # 构建资源描述信息
                        resource_desc = description or f"Resource at {uri}" if uri else "Unknown resource"
                        
                        capabilities.append({
                            'name': resource.name,
                            'description': resource_desc,
                            'cap_type': CapabilityType.RESOURCE,
                            'parameters': None
                        })
            
            # 更新数据库
            if capabilities:
                log.info(f"更新服务ID {service_id} 的能力信息，共 {len(capabilities)} 项")
                self.capability_model.update_service_capabilities(service_id, capabilities)
            else:
                log.warning(f"服务ID {service_id} 没有可用的工具或资源")
                
        except Exception as e:
            log.error(f"更新服务能力信息失败: {str(e)}")
    
    def should_scan_service(self, service: McpService) -> bool:
        """判断服务是否需要扫描"""
        if not service.status_check_tm:
            log.debug(f"服务 {service.name} 从未被扫描过")
            return True

        current_time = datetime.datetime.now()
        time_diff = current_time - service.status_check_tm

        if service.status == ServiceStatus.ONLINE:
            should_scan = time_diff.total_seconds() >= self.config._config["scheduler"]["valid_service_interval"]
        else:
            should_scan = time_diff.total_seconds() >= self.config._config["scheduler"]["invalid_service_interval"]
            
        if should_scan:
            log.debug(f"服务 {service.name} 需要扫描，距离上次扫描已过去 {time_diff.total_seconds():.1f} 秒")
        return should_scan