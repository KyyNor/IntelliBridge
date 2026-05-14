"""
Hindsight 记忆模块 - 提供基于 Hindsight 的知识管理与记忆召回功能

知识层面：
- bank_id: 对应一个记忆仓库（对应 user_id，由请求头 x-user-id 或配置派生）
- mental_model: 知识页面（围绕特定主题持续合成的记忆摘要）
- recall: 跨对话和文档的事实检索
- retain: 上传原始文本作为底仓记忆
"""

import json
import os
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from utils.config import config
from utils.logger import logger
from utils.decorators import log_function_info

from fastmcp import FastMCP

memory_mcp = FastMCP("IntelliBridge Hindsight Memory")


# ==================== Hindsight 客户端（原 lib.client）====================

class HindsightClient:
    """
    Hindsight API 客户端，封装所有后端交互
    """

    def __init__(self, api_url: str, api_token: Optional[str] = None):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
        timeout: int = 15,
    ) -> Any:
        """
        通用 HTTP 请求封装

        Args:
            method: HTTP 方法 (GET/POST/PATCH/DELETE)
            path: API 路径
            body: 请求体（JSON serializable dict）
            timeout: 超时秒数

        Returns:
            解包后的响应数据（dict/list），出错时返回带 "error" 的 dict
        """
        url = f"{self.api_url}{path}"
        logger.debug(f"[HindsightClient] {method} {url}")

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in self._build_headers().items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                result = json.loads(raw)
                return result.get("data", result)
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
                msg = err_body.get("error", err_body.get("message", str(e)))
            except Exception:
                msg = str(e)
            logger.debug(f"[HindsightClient] HTTP {e.code} error: {msg}")
            return {"error": msg, "status_code": e.code}
        except Exception as e:
            logger.debug(f"[HindsightClient] Request exception: {e}")
            return {"error": str(e)}

    def recall(
        self,
        bank_id: str,
        query: str,
        max_tokens: int = 512,
        budget: str = "mid",
        timeout: int = 15,
    ) -> Any:
        """
        事实检索：在所有对话记忆和底仓文档中搜索答案

        对应 Mem0 的 search，但语义更强——不只查单条记忆，
        而是跨对话合成结果与文档 chunks 综合召回。
        """
        encoded_bank = urllib.parse.quote(bank_id, safe="")
        path = f"/v1/default/banks/{encoded_bank}/recall"
        return self.request(
            "POST",
            path,
            body={"query": query, "budget": budget, "max_tokens": max_tokens},
            timeout=timeout,
        )

    def retain(
        self,
        bank_id: str,
        content: str,
        document_id: str,
        timeout: int = 15,
    ) -> Any:
        """
        上传原文到底仓，后续通过 recall 引用
        与 Mem0 的 add 不同：retain 保全文原不做摘要；
        add 则会自动抽取事实存入向量库，两者互补。
        """
        encoded_bank = urllib.parse.quote(bank_id, safe="")
        path = f"/v1/default/banks/{encoded_bank}/retain"
        return self.request(
            "POST",
            path,
            body={"document_id": document_id, "content": content},
            timeout=timeout,
        )


# ==================== 常量 ====================

DEFAULT_BANK_ID = "shared"


# ==================== MCP 服务类 ====================

class HindsightService:
    """
    Hindsight 记忆客户端封装类，对外提供统一的 add/recall/retain 语义映射
    """

    def __init__(self):
        self._client: Optional[HindsightClient] = None
        self._initialized = False

    def _ensure_init(self) -> bool:
        if self._initialized:
            return True

        try:
            api_url = config.get("memory.base_url", "")
            api_token = config.get("memory.api_token", "")

            self._client = HindsightClient(api_url, api_token)
            self._initialized = True
            logger.info(f"Hindsight 客户端初始化完成，API: {api_url}")
            return True

        except Exception as e:
            logger.error(f"Hindsight 初始化失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def list_pages(self, bank_id: str) -> Dict[str, Any]:
        """列出所有知识页面（元数据维度，轻量化）"""
        self._ensure_init()
        try:
            resp = self._client.request(
                "GET",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/mental-models?detail=metadata",
                timeout=10,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"列出知识页面失败: {e}")
            return {"success": False, "error": str(e)}

    def get_page(self, page_id: str, bank_id: str) -> Dict[str, Any]:
        """读取指定知识页面的完整内容（含合成摘要）"""
        self._ensure_init()
        try:
            resp = self._client.request(
                "GET",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/mental-models/{urllib.parse.quote(page_id, safe='')}?detail=full",
                timeout=10,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"读取知识页面失败: {e}")
            return {"success": False, "error": str(e)}

    def create_page(self, page_id: str, name: str, source_query: str, bank_id: str, max_tokens: int = 4096) -> Dict[str, Any]:
        """创建新知识页面，系统会在每次合并后根据 source_query 重建该页"""
        self._ensure_init()
        try:
            resp = self._client.request(
                "POST",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/mental-models",
                body={
                    "id": page_id,
                    "name": name,
                    "source_query": source_query,
                    "max_tokens": max_tokens,
                    "trigger": {
                        "mode": "delta",
                        "refresh_after_consolidation": True,
                        "fact_types": ["observation"],
                        "exclude_mental_models": True,
                    },
                },
                timeout=15,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"创建知识页面失败: {e}")
            return {"success": False, "error": str(e)}

    def update_page(self, page_id: str, bank_id: str, name: str = "", source_query: str = "") -> Dict[str, Any]:
        """更新知识页面名称或重建查询，下一次合并时生效"""
        self._ensure_init()
        body = {}
        if name:
            body["name"] = name
        if source_query:
            body["source_query"] = source_query
        if not body:
            return {"success": False, "error": "请提供 name 或 source_query"}
        try:
            resp = self._client.request(
                "PATCH",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/mental-models/{urllib.parse.quote(page_id, safe='')}",
                body=body,
                timeout=10,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"更新知识页面失败: {e}")
            return {"success": False, "error": str(e)}

    def delete_page(self, page_id: str, bank_id: str) -> Dict[str, Any]:
        """永久删除知识页面"""
        self._ensure_init()
        try:
            resp = self._client.request(
                "DELETE",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/mental-models/{urllib.parse.quote(page_id, safe='')}",
                timeout=10,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"删除知识页面失败: {e}")
            return {"success": False, "error": str(e)}

    def recall(self, query: str, bank_id: str, max_results: int = 10) -> Dict[str, Any]:
        """
        跨对话与文档的事实检索，对应 Mem0 的 search 语义
        但底层综合了合成回忆与文档 chunks，更接近真实问答
        """
        self._ensure_init()
        try:
            resp = self._client.recall(
                bank_id=bank_id,
                query=query,
                max_tokens=int(max_results * 400),
                budget="mid",
                timeout=25,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"检索记忆失败: {e}")
            return {"success": False, "error": str(e)}

    def retain(self, doc_title: str, content: str, bank_id: str) -> Dict[str, Any]:
        """
        整篇上传原文到底仓，与 Mem0 的 add 对应但不裁剪摘要
        用于保留下游可引用的原始参考材料（如需求文档、技术设计等）
        """
        self._ensure_init()
        try:
            doc_id = doc_title.lower().replace(" ", "-")
            resp = self._client.retain(
                bank_id=bank_id,
                content=content,
                document_id=doc_id,
                timeout=15,
            )
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            return {"success": True, "data": resp}
        except Exception as e:
            logger.error(f"上传底仓文档失败: {e}")
            return {"success": False, "error": str(e)}


# ==================== 全局实例 ====================

hs_service = HindsightService()


# ==================== MCP 工具注册 ====================

PAGE_DEFAULTS = {
    "mode": "delta",
    "refresh_after_consolidation": True,
    "fact_types": ["observation"],
    "exclude_mental_models": True,
}

from fastmcp.dependencies import CurrentHeaders


def _resolve_bank_id(headers: Dict) -> str:
    """从请求头 x-user-id 解析 bank_id，空值回退到配置兜底"""
    uid = headers.get("x-user-id", "")
    if uid and uid != "anonymous":
        return uid
    fallback = config.get("hindsight.bank_id", DEFAULT_BANK_ID)
    return fallback


# -------------------- 知识页面管理 --------------------

@memory_mcp.tool(name="list_pages")
@log_function_info
def hindsight_list_pages(headers: dict = CurrentHeaders()) -> str:
    """
    列出当前银行下所有知识页面（轻量化元数据，不含正文）

    Returns:
        JSON 格式的页面列表
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.list_pages(bank_id=bank_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@memory_mcp.tool(name="get_page")
@log_function_info
def hindsight_get_page(page_id: str, headers: dict = CurrentHeaders()) -> str:
    """
    读取指定知识页面的完整内容

    Args:
        page_id: 知识页面 ID

    Returns:
        JSON 格式的页面完整数据
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.get_page(page_id=page_id, bank_id=bank_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@memory_mcp.tool(name="create_page")
@log_function_info
def hindsight_create_page(
    page_id: str,
    name: str,
    source_query: str,
    max_tokens: int = 4096,
    headers: dict = CurrentHeaders(),
) -> str:
    """
    创建新的知识页面

    系统会在每次对话合并后，根据 source_query 重建该页面（增量追加）。
    适合用作个人 Wiki、指标定义、架构决策记录等长期沉淀的场景。

    Args:
        page_id: 页面唯一标识，可含字母数字连字符
        name: 页面显示名
        source_query: 重建问题，回答将作为该页的主要内容
        max_tokens: 单次合成的最大 token 数，默认 4096

    Returns:
        JSON 格式的创建结果
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.create_page(
        page_id=page_id,
        name=name,
        source_query=source_query,
        bank_id=bank_id,
        max_tokens=max_tokens,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@memory_mcp.tool(name="update_page")
@log_function_info
def hindsight_update_page(
    page_id: str,
    name: str = "",
    source_query: str = "",
    headers: dict = CurrentHeaders(),
) -> str:
    """
    更新已有知识页面的名称或重建查询

    内容将在下一次自动合并时重新生成，无需手动干预。

    Args:
        page_id: 待更新的页面 ID
        name: 新显示名（非必填，留空则不变更）
        source_query: 新的重建问题（同上）

    Returns:
        JSON 格式的更新结果
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.update_page(page_id=page_id, bank_id=bank_id, name=name, source_query=source_query)
    return json.dumps(result, ensure_ascii=False, indent=2)


@memory_mcp.tool(name="delete_page")
@log_function_info
def hindsight_delete_page(page_id: str, headers: dict = CurrentHeaders()) -> str:
    """
    永久删除指定知识页面

    Args:
        page_id: 待删除的页面 ID

    Returns:
        JSON 格式的操作结果
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.delete_page(page_id=page_id, bank_id=bank_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


# -------------------- 底仓记忆检索 --------------------

@memory_mcp.tool(name="recall")
@log_function_info
def hindsight_recall(
    query: str,
    max_results: int = 10,
    headers: dict = CurrentHeaders(),
) -> str:
    """
    记忆召回，从记忆中查找相关内容。
    当你遇到任何不了解的事情的时候，都应当先做一次记忆召回。

    Args:
        query: 查询条件
        max_results: 隐式控制召回规模（token budget 分配），建议 5~20

    Returns:
        JSON 格式的检索结果，含合成回答与溯源片段
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.recall(query=query, bank_id=bank_id, max_results=max_results)
    return json.dumps(result, ensure_ascii=False, indent=2)


# -------------------- 底仓文档写入 --------------------

@memory_mcp.tool(name="ingest")
@log_function_info
def hindsight_ingest(
    title: str,
    content: str,
    headers: dict = CurrentHeaders(),
) -> str:
    """
    与 create_page 的区别：
    - ingest：将原文整块存入底仓，适合参考资料、技术文档等需逐字查阅的内容
    - create_page：建立主题页，系统持续提炼合成，等效于"活"的笔记

    同标题重复上传会覆盖原文，而非追加。

    Args:
        title: 文档标题（会自动转译为 document_id，小写下划线连写）
        content: 原始全文，越完整越好，切勿提前摘要

    Returns:
        JSON 格式的上传结果
    """
    bank_id = _resolve_bank_id(headers)
    result = hs_service.retain(doc_title=title, content=content, bank_id=bank_id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@memory_mcp.tool(name="ingest_file")
@log_function_info
def hindsight_ingest_file(file_path: str, headers: dict = CurrentHeaders()) -> str:
    """
    从磁盘读取文件，上传其全部内容到底仓记忆

    Args:
        file_path: 本地文件的绝对路径

    Returns:
        JSON 格式的上传结果
    """
    if not os.path.isfile(file_path):
        return json.dumps({"success": False, "error": f"文件不存在: {file_path}"}, ensure_ascii=False, indent=2)

    try:
        content = open(file_path, encoding="utf-8").read()
    except UnicodeDecodeError:
        return json.dumps({"success": False, "error": f"文件无法以 UTF-8 解码，请检查编码: {file_path}"}, ensure_ascii=False, indent=2)

    if not content.strip():
        return json.dumps({"success": False, "error": f"文件为空: {file_path}"}, ensure_ascii=False, indent=2)

    doc_id = os.path.basename(file_path).rsplit(".", 1)[0].lower().replace(" ", "-")
    bank_id = _resolve_bank_id(headers)

    result = hs_service.retain(doc_title=doc_id, content=content, bank_id=bank_id)
    if "error" in result:
        return json.dumps(result, ensure_ascii=False, indent=2)
    return json.dumps({
        "success": True,
        "data": result.get("data"),
        "meta": {
            "document_id": doc_id,
            "file_path": file_path,
            "size_bytes": len(content.encode("utf-8")),
        }
    }, ensure_ascii=False, indent=2)