"""
Mem0 记忆模块 - 提供基于 Mem0 的情境记忆功能

支持三层分层存储：
- user_id: 对应小组/团队（如 "ai-innovation-team"）
- agent_id: 对应应用/助手（如 "credit-bot", "claude-code"）
- run_id: 对应会话（如 "session-abc123"）
"""

from typing import Optional, List, Dict, Any
import json
from mem0 import Memory
from mem0.configs.base import MemoryConfig
from mem0.configs.base import VectorStoreConfig
from mem0.embeddings.configs import EmbedderConfig
from mem0.llms.configs import LlmConfig
from mem0.configs.base import RerankerConfig

from prompts.memory_prompts import MEMORY_FACT_EXTRACTION_PROMPT
from utils.mcp import mcp
from utils.decorators import log_function_info
from utils.config import config
from utils.logger import logger


class Mem0Memory:
    """
    Mem0 记忆客户端封装类
    """

    def __init__(self):
        """初始化 Mem0 记忆客户端"""
        self._memory = None
        self._initialized = False

    def _get_config(self) -> Dict[str, Any]:
        """获取 memory 配置"""
        return config.get("memory", {})

    def initialize(self) -> bool:
        """
        初始化 Mem0 客户端

        Returns:
            是否初始化成功
        """
        if self._initialized:
            return True

        try:
            memory_config = self._get_config()
            qdrant_config = memory_config.get("qdrant", {})
            embedding_config = memory_config.get("embedding", {})
            llm_config = memory_config.get("llm", {})
            reranker_config = memory_config.get("reranker", {})

            # 构建 Embedder 配置 (使用 OpenAI 兼容格式)
            embedder_cfg = EmbedderConfig(
                provider="openai",
                config={
                    "model": embedding_config.get("model", "text-embedding-3-small"),
                    "api_key": embedding_config.get("api_key", ""),
                    "openai_base_url": embedding_config.get("base_url", "http://localhost:8000/v1"),
                    "embedding_dims": embedding_config.get("dimension", 1024)
                }
            )

            # 构建 Qdrant 向量存储配置
            vector_store_cfg = VectorStoreConfig(
                provider="qdrant",
                config={
                    "host" : qdrant_config.get("host", "localhost"),
                    "port" : qdrant_config.get("port", 6333),
                    "api_key" : qdrant_config.get("api_key", ""),
                    "collection_name" : qdrant_config.get("collection_name", "intellibridge_memory"),
                    "embedding_model_dims" : embedding_config.get("dimension", 1024)
                }
            )

            # 构建 LLM 配置 (用于 add 操作时的记忆提取)
            llm_cfg = None
            if llm_config:
                llm_cfg = LlmConfig(
                    provider=llm_config.get("provider", "openai"),
                    config={
                        "model": llm_config.get("model", "qwen2.5"),
                        "api_key": llm_config.get("api_key", ""),
                        "openai_base_url": llm_config.get("base_url", "http://localhost:8000/v1")
                    }
                )

            # 构建 Reranker 配置 (用于搜索结果排序)
            reranker_cfg = None
            if reranker_config:
                reranker_cfg = RerankerConfig(
                    provider=reranker_config.get("provider", "llm_reranker"),
                    config={
                        "provider": llm_config.get("provider", "openai") if llm_config else "openai",
                        "model": llm_config.get("model", "qwen2.5") if llm_config else "qwen2.5",
                        "api_key": llm_config.get("api_key", "") if llm_config else "",
                        "openai_base_url": llm_config.get("base_url", "http://localhost:8000/v1") if llm_config else "http://localhost:8000/v1",
                        "top_k": reranker_config.get("top_k", 10)
                    } if reranker_config.get("provider") == "llm_reranker" else None
                )

            # 构建 MemoryConfig
            mem_cfg = MemoryConfig(
                embedder=embedder_cfg,
                vector_store=vector_store_cfg,
                llm=llm_cfg,
                reranker=reranker_cfg,
                custom_fact_extraction_prompt=MEMORY_FACT_EXTRACTION_PROMPT,
            )

            # 创建 Mem0 实例
            self._memory = Memory(config=mem_cfg)

            self._initialized = True
            logger.info("Mem0 记忆客户端初始化成功")
            return True

        except Exception as e:
            logger.error(f"Mem0 初始化失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def add(
        self,
        user_id: str,
        content: str
    ) -> Dict[str, Any]:
        """
        添加记忆

        Args:
            user_id: 用户/小组 ID
            agent_id: 应用/助手 ID
            run_id: 会话 ID
            content: 记忆内容

        Returns:
            添加结果
        """
        if not self._initialized:
            self.initialize()

        try:
            result = self._memory.add(
                messages=content,
                user_id=user_id,
            )
            return {"success": True, "data": result}
        except Exception as e:
            logger.error(f"添加记忆失败: {e}")
            return {"success": False, "error": str(e)}

    # 最大返回条数
    MAX_SEARCH_LIMIT = 100

    def search(
        self,
        user_id: str,
        query: str,
        limit: int = 5
    ) -> Dict[str, Any]:
        """
        搜索记忆

        Args:
            user_id: 用户/小组 ID
            agent_id: 应用/助手 ID
            run_id: 会话 ID
            query: 查询内容
            limit: 返回结果数量（最大 100）

        Returns:
            搜索结果
        """
        if not self._initialized:
            self.initialize()

        try:
            # 限制最大返回条数
            limit = max(1, min(limit, self.MAX_SEARCH_LIMIT))

            results = self._memory.search(
                query=query,
                user_id=user_id,
                limit=limit
            )
            return {"success": True, "data": results}
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return {"success": False, "error": str(e)}


    def delete(self, memory_id: str) -> Dict[str, Any]:
        """
        删除指定记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            删除结果
        """
        if not self._initialized:
            self.initialize()

        try:
            result = self._memory.delete(memory_id=memory_id)
            return {"success": True, "data": result}
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return {"success": False, "error": str(e)}

    def update(self, memory_id: str, content: str) -> Dict[str, Any]:
        """
        更新指定记忆

        Args:
            memory_id: 记忆 ID
            content: 新的记忆内容

        Returns:
            更新结果
        """
        if not self._initialized:
            self.initialize()

        try:
            result = self._memory.update(memory_id=memory_id, data={"memory": content})
            return {"success": True, "data": result}
        except Exception as e:
            logger.error(f"更新记忆失败: {e}")
            return {"success": False, "error": str(e)}


# 全局实例
mem = Mem0Memory()


# ==================== MCP 工具函数 ====================

@mcp.tool()
@log_function_info
def memory_add(
    user_id: str,
    content: str
) -> str:
    """
    添加记忆

    Args:
        content: 记忆内容

    Returns:
        JSON 格式的添加结果
    """
    result = mem.add(user_id, content)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@log_function_info
def memory_search(
    user_id: str,
    query: str,
    limit: int = 5
) -> str:
    """
    搜索记忆

    Args:
        user_id: 当前用户名称
        query: 查询内容
        limit: 返回结果数量（默认 5）

    Returns:
        JSON 格式的搜索结果
    """
    result = mem.search(user_id, query, limit)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@log_function_info
def memory_delete(memory_id: str) -> str:
    """
    删除指定记忆

    Args:
        memory_id: 记忆 ID

    Returns:
        JSON 格式的删除结果
    """
    result = mem.delete(memory_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@log_function_info
def memory_update(memory_id: str, content: str) -> str:
    """
    更新指定记忆

    Args:
        memory_id: 记忆 ID
        content: 新的记忆内容

    Returns:
        JSON 格式的更新结果
    """
    result = mem.update(memory_id, content)
    return json.dumps(result, ensure_ascii=False, indent=2)

