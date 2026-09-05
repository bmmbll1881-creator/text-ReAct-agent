# context.py
"""上下文压缩工具：估算 token、判断是否需要压缩、执行压缩。"""

from typing import Callable, Awaitable, Optional
from config import config
from logger import log_event

# 模块级全局标志，防止压缩过程中再次触发压缩
# 正在压缩中的消息列表集合（按 id 区分会话），
# 仅阻止同一对话在压缩期间被再次触发压缩，不同会话可并发进行
_compressing: set[int] = set()

# LLM 调用类型别名
LLMCallable = Callable[[list[dict[str, str]], Optional[str], str, int | None], Awaitable[str]]

def estimate_tokens(messages: list[dict]) -> int:
    """粗略估算消息列表的 token 数：按字符数除以 4 估算。"""
    return len(str(messages)) // 4

def should_compress(messages: list[dict], limit: int) -> bool:
    """判断是否需要压缩：估算 token 超过限制且消息数大于 4。"""
    return estimate_tokens(messages) > limit and len(messages) > 4

async def compress_conversation(
        messages: list[dict],
        llm_call: LLMCallable,
        system_msg: str,
        initial_task: str,
        keep_recent: int = config.context_keep_recent_messages,
        session_id: str = "",
        step: int | None = None,
) -> list[dict]:
    """压缩对话历史，生成摘要并保留最近的消息。
    参数：
        messages: 当前完整消息列表（包含 system, user, assistant 等）
        system_msg: 原始系统提示词内容
        initial_task: 初始用户任务
        llm_call: 异步 LLM 调用函数，签名应为 async def(messages, system_prompt=None) -> str
        keep_recent: 保留最近的消息条数（默认为配置值）
    返回：
        压缩后的新消息列表。如果压缩失败，返回原始 messages。
    """

    key = id(messages)
    if key in _compressing:
        return messages

    _compressing.add(key)
    try:
        # 分离系统消息、用户消息
        system_msgs = [m for m in messages if m.get("role") == "system"]
        user_msgs = [m for m in messages if m.get("role") == "user"]

        # 提取初始任务：通常为第一条user
        if not initial_task:
            initial_task = user_msgs[0]["content"] if user_msgs else ""

        # 最近 keep_recent 条原样保留，窗口之外的其余消息将被压缩
        recent_messages = messages[-keep_recent:]
        to_condense = messages[:-keep_recent]

        # 整个对话都在保留窗口内，无需压缩
        if not to_condense:
            return messages

        # 较早消息
        early_messages = [
            msg for msg in to_condense
            if msg.get("role") != "system"
               and not (msg.get("role") == "user"
                        and msg.get("content") == initial_task)
        ]

        if not early_messages:
            return messages # 窗口外只有系统提示/初始任务，无可浓缩内容

        # 构造压缩提示
        conversation_text = ""
        for msg in early_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            conversation_text += f"{role}: {content}\n"

        summary_prompt = (
            "请对以下对话历史进行摘要，必须包含以下信息：\n"
            "1. 用户目标\n"
            "2. 已读文件与关键内容\n"
            "3. 已写文件与内容\n"
            "4. 失败原因（如有）\n"
            "5. 未完成工作\n\n"
            "对话历史：\n"
            f"{conversation_text}\n\n"
            "请输出简洁的结构化摘要。"
        )

        # 调用 LLM 生成摘要
        summary_skill_prompt = "你是助手，需要根据用户目标、已读文件、已写文件和对话历史，生成一个结构化的摘要。"
        summary_messages = [{"role": "user", "content": summary_prompt}]
        try:
            summary = await llm_call(summary_messages, summary_skill_prompt, session_id, step)
            # 截断摘要
            summary = summary[:2000]
        except Exception as error:
            # 摘要失败，记录日志并降级返回原消息
            log_event("context_compression_error", session_id="", step=None, error=str(error))
            return messages

        # 组织新消息列表
        new_messages = []
        if system_msg:
            new_messages.append({"role": "system", "content": system_msg})
        else :
            # 如果没有系统消息，则使用原始系统消息的第一条
            if system_msgs:
                new_messages.append(system_msgs[0])
        new_messages.append({"role": "user", "content": initial_task})
        new_messages.append({"role": "assistant", "content": f"Summary of earlier conversation:\n{summary}"})
        new_messages.extend(recent_messages)
        return new_messages

    finally:
        _compressing.discard(key)

