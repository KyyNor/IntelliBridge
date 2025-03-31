import aiohttp
import asyncio
from datetime import datetime, timedelta
from typing import List
from models.mcp_service_model import McpService, ServiceStatus
from utils.config_manager import ConfigManager

class McpServiceScanTasker:
    def __init__(self, mcp_service_model):
        self.mcp_service_model = mcp_service_model
        self.config = ConfigManager()

    async def check_service_status(self, service: McpService) -> ServiceStatus:
        """检查单个服务的状态"""
        if not service.endpoint:
            return ServiceStatus.OFFLINE

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(service.endpoint, timeout=5) as response:
                    if response.status == 200:
                        return ServiceStatus.ONLINE
                    return ServiceStatus.OFFLINE
        except Exception:
            return ServiceStatus.OFFLINE

    async def scan_service(self, service: McpService) -> None:
        """扫描单个服务并更新状态"""
        current_status = await self.check_service_status(service)
        current_time = datetime.now(datetime.UTC)
        
        service.status = current_status
        service.status_check_tm = current_time
        service.updated_tm = current_time
        
        self.mcp_service_model.update_service_status(service)

    async def scan_all_services(self) -> None:
        """扫描所有服务"""
        services = self.mcp_service_model.list_mcp_services()
        tasks = [self.scan_service(service) for service in services]
        await asyncio.gather(*tasks)

    def should_scan_service(self, service: McpService) -> bool:
        """判断服务是否需要扫描"""
        if not service.status_check_tm:
            return True

        current_time = datetime.now(datetime.UTC)
        time_diff = current_time - service.status_check_tm

        if service.status == ServiceStatus.ONLINE:
            return time_diff.total_seconds() >= self.config._config["scheduler"]["valid_service_interval"]
        else:
            return time_diff.total_seconds() >= self.config._config["scheduler"]["invalid_service_interval"] 