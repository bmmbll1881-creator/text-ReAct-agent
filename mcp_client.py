"""RAG MCP 客户端：把 evidence-reflective-rag 作为 stdio 子进程拉起并复用会话。

只暴露 ``start()`` / ``call_tool()`` / ``close()`` 三个动作，业务语义留给
``knowledge_backend``。生命周期约束：``start()`` 与 ``close()`` 必须在同一个任务里
调用（FastAPI lifespan 与 Demo 脚本都满足），否则 anyio 的 cancel scope 会跨任务退出报错。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Self

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

#: 必须在 RAG 服务端存在的工具，缺失说明版本不匹配
REQUIRED_TOOLS = ("knowledge_search",)


class McpStartupError(RuntimeError):
    """MCP 子进程启动或握手失败。"""


class McpToolError(RuntimeError):
    """MCP 工具返回 isError，携带服务端错误文本。"""


def _extract_text(result: Any) -> str:
    """从 CallToolResult 的 content 列表里拼接文本块。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _parse_payload(result: Any) -> dict[str, Any]:
    """把 CallToolResult 归一化为字典。

    优先 ``structuredContent``（FastMCP 对 dict 返回值会同时给出），其次把首个文本块
    当 JSON 解析，最后退化为 ``{"text": ...}``，保证调用方永远拿到 dict。
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    text = _extract_text(result)
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
    return {}


class RagMcpClient:
    """持有一个 stdio MCP 会话，供多次工具调用复用。

    Args:
        python: RAG 侧解释器路径
        project: RAG 项目根目录
        data_dir: RAG 数据目录（SQLite 与索引）
        kb_aliases: 透传给服务端的知识库别名映射
        default_kb: 透传给服务端的默认知识库
        startup_timeout: 启动握手超时（秒）
    """

    def __init__(
        self,
        *,
        python: Path,
        project: Path,
        data_dir: Path,
        kb_aliases: str = "",
        default_kb: str = "",
        startup_timeout: float = 15.0,
    ) -> None:
        self._python = python
        self._project = project
        self._data_dir = data_dir
        self._kb_aliases = kb_aliases
        self._default_kb = default_kb
        self._startup_timeout = startup_timeout

        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: tuple[str, ...] = ()

    @property
    def started(self) -> bool:
        """会话是否已经就绪。"""
        return self._session is not None

    @property
    def tools(self) -> tuple[str, ...]:
        """服务端暴露的工具名。"""
        return self._tools

    def _server_params(self) -> StdioServerParameters:
        """构造子进程参数。

        用 ``get_default_environment()`` 而不是 ``os.environ`` 全量透传：
        后者会把 API Key 等敏感变量带进子进程。
        """
        env = get_default_environment()
        env.update({
            "PYTHONPATH": str(self._project / "src"),
            "RAG_DATA_DIR": str(self._data_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        if self._kb_aliases:
            env["RAG_KB_ALIASES"] = self._kb_aliases
        if self._default_kb:
            env["RAG_DEFAULT_KB"] = self._default_kb
        return StdioServerParameters(
            command=str(self._python),
            args=["-m", "evidence_reflective_rag.mcp_server"],
            env=env,
            cwd=str(self._project),
        )

    async def start(self) -> None:
        """拉起子进程、完成握手并校验必需工具存在。

        Raises:
            McpStartupError: 子进程无法启动、握手超时或缺少必需工具
        """
        if self._session is not None:
            return

        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self._startup_timeout):
                read, write = await stack.enter_async_context(
                    stdio_client(self._server_params())
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
        except TimeoutError as error:
            await self._discard_stack(stack)
            raise McpStartupError(
                f"MCP 握手超时（{self._startup_timeout}s）：{self._project}"
            ) from error
        except Exception as error:
            await self._discard_stack(stack)
            raise McpStartupError(f"MCP 启动失败：{error}") from error

        names = tuple(tool.name for tool in listed.tools)
        missing = [name for name in REQUIRED_TOOLS if name not in names]
        if missing:
            await self._discard_stack(stack)
            raise McpStartupError(
                f"RAG MCP 服务缺少工具 {missing}，实际暴露：{list(names)}"
            )

        self._stack = stack
        self._session = session
        self._tools = names

    @staticmethod
    async def _discard_stack(stack: AsyncExitStack) -> None:
        """启动失败时清理半开的资源；清理本身的异常不能掩盖原始错误。"""
        with contextlib.suppress(Exception):
            await stack.aclose()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """调用工具并返回归一化字典。

        Args:
            name: 工具名
            arguments: 工具入参
            timeout: 单次调用超时（秒）

        Returns:
            服务端返回的字典；无法解析时退化为 ``{"text": ...}``

        Raises:
            McpStartupError: 尚未 start()
            McpToolError: 服务端返回 isError
            TimeoutError: 超过 timeout
        """
        if self._session is None:
            raise McpStartupError("MCP 客户端尚未启动，请先 await start()")
        result = await asyncio.wait_for(
            self._session.call_tool(name, arguments), timeout
        )
        if getattr(result, "isError", False):
            detail = _extract_text(result) or "unknown error"
            raise McpToolError(f"{name} 调用失败：{detail}")
        return _parse_payload(result)

    async def close(self) -> None:
        """关闭会话与子进程；必须在 start() 的同一任务中调用。"""
        stack, self._stack = self._stack, None
        self._session = None
        self._tools = ()
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def client_from_config(settings: Any) -> RagMcpClient:
    """按配置构造客户端；配置不完整时抛错而不是静默降级。

    Args:
        settings: ``config.Config`` 实例

    Returns:
        尚未启动的客户端

    Raises:
        ValueError: 缺少解释器/项目/数据目录配置
    """
    if settings.rag_mcp_python is None or settings.rag_mcp_project is None:
        raise ValueError("RAG_MCP_PYTHON / RAG_MCP_PROJECT 未配置")
    if settings.rag_mcp_data_dir is None:
        raise ValueError("RAG_MCP_DATA_DIR 未配置")
    return RagMcpClient(
        python=settings.rag_mcp_python,
        project=settings.rag_mcp_project,
        data_dir=settings.rag_mcp_data_dir,
        kb_aliases=settings.rag_mcp_kb_aliases,
        default_kb=settings.rag_mcp_default_kb,
        startup_timeout=settings.rag_mcp_startup_timeout,
    )


__all__ = [
    "REQUIRED_TOOLS",
    "McpStartupError",
    "McpToolError",
    "RagMcpClient",
    "client_from_config",
]
