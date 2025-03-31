import aiohttp
import asyncio
from datetime import datetime, timedelta
from typing import List
from models.mcp_service_model import McpService, ServiceStatus
from utils.config_manager import ConfigManager
from utils.logger import log

class McpServiceScanTasker:
    def __init__(self, mcp_service_model):
        self.mcp_service_model = mcp_service_model
        self.config = ConfigManager()
        log.info("初始化MCP服务扫描任务器")

    async def check_service_status(self, service: McpService) -> ServiceStatus:
        """检查单个服务的状态"""
        if not service.endpoint:
            log.warning(f"服务 {service.name} 没有配置端点")
            return ServiceStatus.OFFLINE

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(service.endpoint, timeout=5) as response:
                    if response.status == 200:
                        log.debug(f"服务 {service.name} 状态检查成功")
                        return ServiceStatus.ONLINE
                    log.warning(f"服务 {service.name} 返回非200状态码: {response.status}")
                    return ServiceStatus.OFFLINE
        except Exception as e:
            log.error(f"服务 {service.name} 状态检查失败: {str(e)}")
            return ServiceStatus.OFFLINE

    async def scan_service(self, service: McpService) -> None:
        """扫描单个服务并更新状态"""
        log.debug(f"开始扫描服务: {service.name}")
        current_status = await self.check_service_status(service)
        current_time = datetime.now(datetime.UTC)
        
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

    def should_scan_service(self, service: McpService) -> bool:
        """判断服务是否需要扫描"""
        if not service.status_check_tm:
            log.debug(f"服务 {service.name} 从未被扫描过")
            return True

        current_time = datetime.now(datetime.UTC)
        time_diff = current_time - service.status_check_tm

        if service.status == ServiceStatus.ONLINE:
            should_scan = time_diff.total_seconds() >= self.config._config["scheduler"]["valid_service_interval"]
        else:
            should_scan = time_diff.total_seconds() >= self.config._config["scheduler"]["invalid_service_interval"]
            
        if should_scan:
            log.debug(f"服务 {service.name} 需要扫描，距离上次扫描已过去 {time_diff.total_seconds():.1f} 秒")
        return should_scan 