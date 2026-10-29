"""知识库后端抽象：让工具层只依赖 ``search()``，实现可替换。

三种实现：
- ``McpKnowledgeBackend``：生产用，包装 ``RagMcpClient``，带并发上限；
- ``StubKnowledgeBackend``：测试用，返回预置结果或抛出预置异常；
- ``NullKnowledgeBackend``：未启用时的降级实现，返回 ``status="disabled"``。

用模块级单例（``set_backend`` / ``get_backend``）而不是层层传参：
工具分派层是纯函数式的，注入点只有应用启动（lifespan / demo 脚本）一处。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from config import RAG_MCP_MAX_CONCURRENCY, RAG_MCP_TOOL_TIMEOUT
from mcp_client import RagMcpClient


@runtime_checkable
class KnowledgeBackend(Protocol):
    """知识库查询协议。"""

    async def search(
        self,
        question: str,
        knowledge_base: str | None = None,
    ) -> dict[str, Any]:
        """查询知识库并返回 ``knowledge_search`` 契约字典。"""
        ...

    async def aclose(self) -> None:
        """释放底层资源。"""
        ...


class McpKnowledgeBackend:
    """通过 MCP 访问 RAG 服务的后端实现。

    Args:
        client: 已启动或待启动的 MCP 客户端
        max_concurrency: 同时进行的查询上限。RAG 侧 SQLite 单锁串行，
            留 2 个并发既能重叠网络/序列化开销，又不会把服务端压满
        timeout: 单次查询超时（秒）
    """

    def __init__(
        self,
        client: RagMcpClient,
        *,
        max_concurrency: int = RAG_MCP_MAX_CONCURRENCY,
        timeout: float = RAG_MCP_TOOL_TIMEOUT,
    ) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout

    @property
    def client(self) -> RagMcpClient:
        """底层客户端，供 demo 直接调用调试工具（rag_run_detail 等）。"""
        return self._client

    async def search(
        self,
        question: str,
        knowledge_base: str | None = None,
    ) -> dict[str, Any]:
        """调用 ``knowledge_search``。

        Args:
            question: 用户问题
            knowledge_base: 别名 / 名称 / kb_id，None 时用服务端默认库

        Returns:
            ``knowledge_search`` 契约字典

        Raises:
            McpStartupError: 客户端未启动
            McpToolError: 服务端返回 isError
            TimeoutError: 超过 timeout
        """
        arguments: dict[str, Any] = {"question": question}
        if knowledge_base:
            arguments["knowledge_base"] = knowledge_base
        async with self._semaphore:
            return await self._client.call_tool(
                "knowledge_search", arguments, timeout=self._timeout
            )

    async def aclose(self) -> None:
        """关闭底层 MCP 会话。"""
        await self._client.close()


class StubKnowledgeBackend:
    """测试替身：按顺序返回预置结果，或抛出预置异常。

    Args:
        responses: 单个字典 / 字典列表 / 异常实例；列表按调用顺序消费，
            用尽后重复最后一项
        delay: 每次调用前的模拟耗时（秒），用于超时测试
    """

    def __init__(
        self,
        responses: dict[str, Any] | list[dict[str, Any] | Exception] | Exception,
        *,
        delay: float = 0.0,
    ) -> None:
        if isinstance(responses, (dict, Exception)):
            responses = [responses]
        self._responses = list(responses)
        self._delay = delay
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    async def search(
        self,
        question: str,
        knowledge_base: str | None = None,
    ) -> dict[str, Any]:
        """记录调用并返回预置结果。"""
        self.calls.append((question, knowledge_base))
        if self._delay:
            await asyncio.sleep(self._delay)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        """标记已关闭。"""
        self.closed = True


class NullKnowledgeBackend:
    """未启用 MCP 时的降级实现：明确告知调用方能力不可用。"""

    async def search(
        self,
        question: str,
        knowledge_base: str | None = None,
    ) -> dict[str, Any]:
        """返回 disabled 状态，不伪造任何答案。"""
        return {
            "status": "disabled",
            "knowledge_base": knowledge_base,
            "knowledge_base_id": None,
            "answer": None,
            "citations": [],
            "route": None,
            "run_id": None,
            "failure_type": None,
            "evidence": [],
            "message": "知识库工具未启用（RAG_MCP_ENABLED=false）",
        }

    async def aclose(self) -> None:
        """无资源需要释放。"""
        return


_backend: KnowledgeBackend | None = None


def set_backend(backend: KnowledgeBackend | None) -> None:
    """注入后端实现；应用启动时调用一次。"""
    global _backend
    _backend = backend


def get_backend() -> KnowledgeBackend:
    """取当前后端；未注入时返回 Null 实现，保证工具永远有确定的返回。"""
    return _backend if _backend is not None else NullKnowledgeBackend()


__all__ = [
    "KnowledgeBackend",
    "McpKnowledgeBackend",
    "NullKnowledgeBackend",
    "StubKnowledgeBackend",
    "get_backend",
    "set_backend",
]
