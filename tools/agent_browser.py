import subprocess
import re
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from utils.config import config
from utils.logger import logger

# 创建路由（单接口直接挂在 /api 下）
router = APIRouter(tags=["Agent"])


# 请求模型
class AgentBrowserRequest(BaseModel):
    session_name: str
    command: str


class AgentBrowser:
    """Agent Browser 工具类"""

    def __init__(self):
        """初始化 Agent Browser 工具"""
        self.cdp_port = config.get("agent_browser.cdp_port", 9222)

    def execute(self, session_name: str, command: str) -> str:
        """
        执行 agent-browser 命令

        Args:
            session_name: 会话名称
            command: 命令，例如 "agent-browser open www.baidu.com" 或 "open www.baidu.com"

        Returns:
            执行结果
        """
        try:
            # 参数校验
            if not session_name or not session_name.strip():
                return "错误: session_name 不能为空"

            command = command.strip()
            if not command:
                return "错误: 命令不能为空"

            # 检查命令中是否已有 session 或 session-name 参数
            has_session_param = self._check_session_params(command)

            # 解析命令为列表
            command_parts = self._parse_command(command)

            # 判断命令是否以 agent-browser 开头
            if command_parts and command_parts[0] == "agent-browser":
                # 命令已包含 agent-browser，在其后插入参数
                base_cmd = command_parts[:1]  # ["agent-browser"]
                rest_cmd = command_parts[1:]  # 其余部分
            else:
                # 命令不包含 agent-browser，添加它
                base_cmd = ["agent-browser"]
                rest_cmd = command_parts

            # 如果命令中没有 session 参数，添加它
            if not has_session_param.get("session"):
                base_cmd.extend(["--session", session_name])

            # 如果命令中没有 session-name 参数，添加它
            if not has_session_param.get("session_name"):
                base_cmd.extend(["--session-name", session_name])

            # 添加 CDP 端口
            base_cmd.extend(["--cdp", str(self.cdp_port)])

            # 组合完整命令
            full_cmd = base_cmd + rest_cmd

            logger.info(f"执行命令: {' '.join(full_cmd)}")

            # 执行命令
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5分钟超时
            )

            # 处理执行结果
            output = []
            if result.stdout:
                output.append(result.stdout)
            if result.stderr:
                output.append(f"标准错误输出:\n{result.stderr}")

            # 添加返回码信息
            if result.returncode != 0:
                output.append(f"命令返回码: {result.returncode}")

            logger.info(f"命令执行完成，返回码: {result.returncode}")

            return "\n".join(output) if output else "命令执行完成，无输出"

        except subprocess.TimeoutExpired:
            error_msg = "错误: 命令执行超时（5分钟）"
            logger.error(error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"命令执行失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def _check_session_params(self, command: str) -> dict:
        """
        检查命令中是否已有 session 或 session-name 参数

        Args:
            command: 命令字符串

        Returns:
            字典，包含 session 和 session_name 的布尔值
        """
        result = {
            "session": False,
            "session_name": False
        }

        # 检查 --session 参数
        if re.search(r"--session\s+\S+", command):
            result["session"] = True

        # 检查 --session-name 参数（支持 --session-name 和 --session_name 两种格式）
        if re.search(r"--session[-_]name\s+\S+", command):
            result["session_name"] = True

        return result

    def _parse_command(self, command: str) -> list:
        """
        解析命令字符串为参数列表

        Args:
            command: 命令字符串

        Returns:
            参数列表
        """
        # 简单的分词，处理引号包裹的参数
        parts = []
        current = ""
        in_quotes = False
        quote_char = None

        for char in command:
            if char in ['"', "'"] and not in_quotes:
                in_quotes = True
                quote_char = char
            elif char == quote_char and in_quotes:
                in_quotes = False
                quote_char = None
            elif char.isspace() and not in_quotes:
                if current:
                    parts.append(current)
                    current = ""
            else:
                current += char

        if current:
            parts.append(current)

        return parts


# 默认实例
agent_browser = AgentBrowser()


# API 路由
@router.post("/api/agent-browser")
async def execute_agent_browser(request: AgentBrowserRequest):
    """执行 Agent Browser 命令"""
    result = agent_browser.execute(request.session_name, request.command)
    return {"data": result}
