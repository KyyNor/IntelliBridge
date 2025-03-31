import asyncio
from datetime import datetime
from services.mcp_service_scan_tasker import McpServiceScanTasker
from models.mcp_service_model import McpServiceModel
from utils.config_manager import ConfigManager
from utils.logger import log

class SchedulerManager:
    def __init__(self, mcp_service_model: McpServiceModel):
        self.mcp_service_model = mcp_service_model
        self.scan_tasker = McpServiceScanTasker(mcp_service_model)
        self._running = False
        self._task = None
        self.config = ConfigManager()
        log.info("初始化调度器管理器")

    async def start(self):
        """启动定时任务"""
        if self._running:
            log.warning("调度器已经在运行中")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_scheduler())
        log.info("调度器已启动")

    async def stop(self):
        """停止定时任务"""
        self._running = False
        if self._task:
            log.info("正在停止调度器...")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("调度器已停止")

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
                    log.debug(f"开始扫描 {len(tasks)} 个服务")
                    await asyncio.gather(*tasks)
                    log.debug("服务扫描完成")
                
                # 等待最短的扫描间隔
                interval = min(
                    self.config._config["scheduler"]["invalid_service_interval"],
                    self.config._config["scheduler"]["valid_service_interval"]
                )
                log.debug(f"等待 {interval} 秒后进行下一轮扫描")
                await asyncio.sleep(interval)
                
            except Exception as e:
                log.error(f"调度器运行错误: {str(e)}")
                await asyncio.sleep(60)  # 发生错误时等待1分钟后重试 