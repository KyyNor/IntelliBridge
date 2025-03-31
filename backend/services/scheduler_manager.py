import asyncio
from datetime import datetime
from services.mcp_service_scan_tasker import McpServiceScanTasker
from models.mcp_service_model import McpServiceModel
from utils.config_manager import ConfigManager

class SchedulerManager:
    def __init__(self, mcp_service_model: McpServiceModel):
        self.mcp_service_model = mcp_service_model
        self.scan_tasker = McpServiceScanTasker(mcp_service_model)
        self._running = False
        self._task = None
        self.config = ConfigManager()

    async def start(self):
        """启动定时任务"""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_scheduler())

    async def stop(self):
        """停止定时任务"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_scheduler(self):
        """运行定时任务"""
        while self._running:
            try:
                services = self.mcp_service_model.list_mcp_services()
                tasks = []
                
                for service in services:
                    if self.scan_tasker.should_scan_service(service):
                        tasks.append(self.scan_tasker.scan_service(service))
                
                if tasks:
                    await asyncio.gather(*tasks)
                
                # 等待最短的扫描间隔
                await asyncio.sleep(min(
                    self.config._config["scheduler"]["invalid_service_interval"],
                    self.config._config["scheduler"]["valid_service_interval"]
                ))
                
            except Exception as e:
                print(f"Error in scheduler: {str(e)}")
                await asyncio.sleep(60)  # 发生错误时等待1分钟后重试 