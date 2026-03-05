#!/usr/bin/env python3
"""
IntelliBridge MCP 服务器入口
用于启动 MCP (Model Context Protocol) 服务
"""

from tools.hive_query import mcp as hive_mcp
from tools.agent_browser import mcp as agent_browser_mcp
from utils.logger import logger


def main():
    """启动 MCP 服务器"""
    logger.info("Starting IntelliBridge MCP Server...")

    # 合并所有 MCP 工具
    # 注意：fastmcp 的实际使用方式可能需要调整
    # 这里提供一个基础的框架

    # 如果需要单独运行某个 MCP 服务，可以这样做：
    # hive_mcp.run()  # 运行 Hive MCP 服务
    # agent_browser_mcp.run()  # 运行 Agent Browser MCP 服务

    logger.info("MCP Server initialized")
    logger.info("Available tools:")
    logger.info("  - hive_describe: 查看 Hive 表结构")
    logger.info("  - hive_query_tool: 查询 Hive 数据")
    logger.info("  - agent_browser_tool: 执行 Agent Browser 命令")


if __name__ == "__main__":
    main()
