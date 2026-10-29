"""工具执行分派层：把本地同步工具与后续的 MCP 异步工具统一成异步调用。

Agent 只依赖 ``execute_tool_async`` 与 ``format_observation``，
不需要知道某个工具是本地文件 IO 还是跨进程 MCP 调用。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel

from config import MAX_OBSERVATION_CHARS
from knowledge_backend import get_backend
from tools import LOCAL_TOOL_REGISTRY, MCP_TOOL_SPECS, available_tools, execute_tool

#: 截断时保留的证据条数与预览长度
_EVIDENCE_KEEP = 2
_PREVIEW_KEEP = 80
#: 逐级收缩策略：(answer 保留长度, evidence 保留条数)
_SHRINK_LEVELS: tuple[tuple[int, int], ...] = ((800, _EVIDENCE_KEEP), (400, 0), (120, 0))
#: 最后兜底时保留的标量字段
_SKELETON_KEYS = ("status", "route", "run_id", "failure_type", "citations", "message")
_CITATIONS_KEEP = 6
_MESSAGE_KEEP = 60

#: 非 success 状态的运行时硬约束。写进下一轮的历史消息，而不是只依赖系统提示词——
#: 提示词是概率性的，而"证据门控不被绕过"必须是确定性的。
_STATUS_CONSTRAINTS: dict[str, str] = {
    "abstain": (
        "知识库返回 abstain（证据不足）。不得基于常识或推测补充答案；"
        "请如实说明当前知识库没有足够证据，并给出可执行的下一步"
        "（补充文档、缩小问题范围或改用匹配的知识库）。"
    ),
    "clarify": ("知识库返回 clarify（问题存在歧义）。先向用户确认歧义点，不要自行假设后作答。"),
    "verification_failed": (
        "知识库返回 verification_failed（引用校验未通过）。"
        "不得把该结论当作事实陈述，需说明结论未通过校验。"
    ),
    "disabled": ("知识库工具未启用。不得假装已查询过知识库，需告知用户该能力当前不可用。"),
    "error": ("知识库调用返回 error（配置或索引问题）。如实说明原因，不要用常识替代证据。"),
}


def constraint_for(payload: Any) -> str:
    """按知识库返回状态生成运行时约束，不需要约束时返回空串。

    Args:
        payload: 工具返回值；非字典或 status 为 success 时返回空串

    Returns:
        带 ``[系统约束]`` 前缀的约束文本
    """
    if not isinstance(payload, dict):
        return ""
    template = _STATUS_CONSTRAINTS.get(str(payload.get("status")))
    if not template:
        return ""
    run_id = payload.get("run_id")
    suffix = f"（run_id={run_id}）" if run_id else ""
    return f"[系统约束] {template}{suffix}"


async def _run_knowledge_search(validated_input: Any) -> dict[str, Any]:
    """调用知识库后端；后端未启用时返回 disabled，由 Agent 如实告知。"""
    return await get_backend().search(validated_input.question, validated_input.knowledge_base)


#: MCP 工具名 -> 执行协程。新增 MCP 工具时只需在这里加一行。
_MCP_HANDLERS: dict[str, Any] = {
    "knowledge_search": _run_knowledge_search,
}


async def execute_tool_async(tool_name: str, validated_input: BaseModel) -> str | dict[str, Any]:
    """执行任意已注册工具。

    本地工具是可阻塞的文件 IO，放进线程池执行；MCP 工具直接 await 异步客户端。
    调用方只看到统一的异步接口，不需要区分工具来源。

    Args:
        tool_name: 工具名称
        validated_input: 已通过 Pydantic 校验的入参实例

    Returns:
        本地工具返回 ``str``；MCP 工具返结构化 ``dict``（由 ``format_observation`` 序列化）

    Raises:
        ValueError: 工具未注册，或注册了但没接执行器
    """
    if tool_name in LOCAL_TOOL_REGISTRY:
        return await asyncio.to_thread(execute_tool, tool_name, validated_input)
    handler = _MCP_HANDLERS.get(tool_name)
    if handler is not None:
        return await handler(validated_input)
    if tool_name in MCP_TOOL_SPECS:
        raise ValueError(f"MCP 工具 {tool_name} 尚未接入执行器")
    raise ValueError(f"未知工具：{tool_name}；可用工具：{available_tools()}")


def _compact(
    payload: dict[str, Any],
    answer_keep: int,
    evidence_keep: int,
) -> dict[str, Any]:
    """按给定预算压缩结构化 observation，保留可解析性。"""
    compact = dict(payload)
    answer = compact.get("answer")
    if isinstance(answer, str) and len(answer) > answer_keep:
        compact["answer"] = answer[:answer_keep] + "…" if answer_keep else ""
    evidence = compact.get("evidence")
    if isinstance(evidence, list):
        trimmed: list[Any] = []
        for item in evidence[:evidence_keep]:
            if isinstance(item, dict):
                item = dict(item)
                preview = item.get("preview")
                if isinstance(preview, str) and len(preview) > _PREVIEW_KEEP:
                    item["preview"] = preview[:_PREVIEW_KEEP] + "…"
            trimmed.append(item)
        compact["evidence"] = trimmed
    compact["truncated"] = True
    return compact


def _skeleton(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """只保留关键标量字段，并给 answer 分配剩余预算。"""
    skeleton: dict[str, Any] = {
        key: payload[key] for key in _SKELETON_KEYS if payload.get(key) is not None
    }
    citations = skeleton.get("citations")
    if isinstance(citations, list):
        skeleton["citations"] = citations[:_CITATIONS_KEEP]
    message = skeleton.get("message")
    if isinstance(message, str) and len(message) > _MESSAGE_KEEP:
        skeleton["message"] = message[:_MESSAGE_KEEP] + "…"

    overhead = len(json.dumps(skeleton, ensure_ascii=False)) + len('"answer": "",')
    answer = payload.get("answer")
    budget = max(limit - overhead, 0)
    if isinstance(answer, str) and answer and budget:
        skeleton["answer"] = answer[:budget] + ("…" if len(answer) > budget else "")
    skeleton["truncated"] = True
    return skeleton


def _fit_skeleton(payload: dict[str, Any], limit: int) -> str:
    """收缩到骨架，必要时按超出量回退裁剪 answer，直到放得下或无可再删。"""
    skeleton = _skeleton(payload, limit)
    body = json.dumps(skeleton, ensure_ascii=False)
    for _ in range(3):
        if len(body) <= limit:
            break
        answer = skeleton.get("answer")
        if not isinstance(answer, str) or not answer:
            break
        overshoot = len(body) - limit + 1
        keep = max(len(answer) - overshoot, 0)
        skeleton["answer"] = answer[:keep] + ("…" if keep else "")
        body = json.dumps(skeleton, ensure_ascii=False)
    return body


def format_observation(
    payload: Any,
    *,
    limit: int | None = None,
) -> str:
    """把工具返回值格式化成写进对话历史的 observation 文本。

    结构化结果先序列化为 JSON：Python repr 会转义中文、把嵌套结构压成难解析的一行。
    超长时逐级收缩（裁剪 evidence → 清空 evidence → 只留标量字段），**始终保证合法 JSON**；
    硬截断会得到无法解析的 JSON，比信息变少更糟。只有连骨架都放不下时才退化为纯文本截断。

    Args:
        payload: 工具返回值，字符串或可 JSON 序列化的字典
        limit: 字符上限，默认取 ``MAX_OBSERVATION_CHARS``

    Returns:
        带前缀的 observation 文本
    """
    limit = MAX_OBSERVATION_CHARS if limit is None else limit
    if isinstance(payload, dict):
        body = json.dumps(payload, ensure_ascii=False)
        for answer_keep, evidence_keep in _SHRINK_LEVELS:
            if len(body) <= limit:
                break
            body = json.dumps(_compact(payload, answer_keep, evidence_keep), ensure_ascii=False)
        if len(body) > limit:
            body = _fit_skeleton(payload, limit)
        if len(body) > limit:
            body = body[:limit] + "…(已截断)"
        return f"工具输出(JSON): {body}"

    text = str(payload)
    if len(text) > limit:
        text = text[:limit] + "\n…(已截断)"
    return f"工具输出: {text}"
