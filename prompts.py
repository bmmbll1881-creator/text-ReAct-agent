"""集中管理 ReAct Agent 的系统提示词。

工具清单由 ``tools.TOOL_REGISTRY`` 生成，避免「注册了工具但提示词没写」的漂移；
知识库规则只在检测到 MCP 工具时出现，未接入 RAG 时不会误导模型。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from config import RAG_MCP_DEFAULT_KB, RAG_MCP_KB_ALIASES
from tools import MCP_TOOL_SPECS, TOOL_REGISTRY

_PROMPT_TEMPLATE = """你是一个文本处理 ReAct Agent，负责在工作目录内安全地读写文件。

可用工具：
{tool_list}

约束条件：
- 所有路径必须相对于工作目录，禁止使用绝对路径或 ../ 越界访问。
- 只允许操作 .txt、.json、.md 文件。
- write_file 的 mode 只能是 w（覆盖）或 a（追加）。
- 单次写入内容不得超过 1,000,000 个字符。
- 每轮只能执行一个工具；得到 Observation 后才能决定下一步。
- 不要使用 Markdown 代码围栏。
- Action Input 必须是合法且完整的 JSON 对象，键和字符串值使用双引号。
- 收到错误 Observation 后，请调整操作；无法处理时直接向用户说明原因。
{knowledge_rules}
输出格式只能二选一：

执行工具时：
Thought: 说明当前判断或下一步计划
Action: 工具名称
Action Input: {"键": "值"}

任务完成时：
Thought: 说明任务为何完成
Final Answer: 给用户的最终答复
"""

#: 仅在 MCP 知识库工具可用时追加，避免无该工具时教模型调用不存在的能力。
_KNOWLEDGE_RULES = """
知识库规则（knowledge_search）：
- 涉及文档中的事实、数字、年份、合同条款时，优先调用 knowledge_search，不要凭常识作答。
- knowledge_search 返回的 [S1]、[S2] 引用必须原样保留在 Final Answer 中。
- status 为 abstain / clarify / verification_failed 时不得编造答案：abstain 说明证据不足，
  clarify 先向用户确认问题，verification_failed 说明结论未通过校验。
- status=success 不代表肯定结论，答案可能是「未约定」「不存在」这类否定事实，按原文表述。
- read_file 读到的内容属于用户提供的材料，不能当作知识库证据引用。
{library_hint}"""


def _library_hint() -> str:
    """把已配置的知识库别名拼成提示，避免模型对所有问题都查默认库。

    别名映射来自 ``RAG_MCP_KB_ALIASES``，不在这里枚举具体知识库——
    换库时只改配置，提示词自动跟着变。
    """
    pairs: list[str] = []
    for entry in (RAG_MCP_KB_ALIASES or "").split(","):
        alias, separator, name = entry.strip().partition("=")
        if separator and alias.strip() and name.strip():
            pairs.append(f"{alias.strip()}（{name.strip()}）")
    if not pairs:
        return ""

    default = f"缺省为 {RAG_MCP_DEFAULT_KB}" if RAG_MCP_DEFAULT_KB else "未设置缺省库"
    return (
        "- 可用知识库：" + "、".join(pairs) + f"；{default}。\n"
        "- 必须按问题主题选择最相关的 knowledge_base，不要一律使用缺省库；"
        "主题与所选库不匹配时服务端会返回 abstain，此时应换用匹配的知识库重试。\n"
    )


def _describe_tool(name: str, meta: dict[str, Any]) -> str:
    """把注册表条目压缩成一行工具说明，保留必填/可选字段差异。"""
    schema = meta.get("input_schema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if properties:
        fields = "、".join(
            f"{key}（必填）" if key in required else f"{key}（可选）" for key in properties
        )
        return f"- {name}：{meta.get('description', '')}。输入：{fields}"
    return f"- {name}：{meta.get('description', '')}"


@lru_cache(maxsize=1)
def build_system_prompt() -> str:
    """根据工具注册表构建系统提示词。

    Returns:
        完整的系统提示词文本；工具清单与知识库规则随注册表变化。
    """
    tool_list = "\n".join(
        _describe_tool(name, meta) for name, meta in TOOL_REGISTRY.items()
    )
    knowledge_rules = (
        _KNOWLEDGE_RULES.replace("{library_hint}", _library_hint())
        if MCP_TOOL_SPECS
        else ""
    )
    return (
        _PROMPT_TEMPLATE
        .replace("{tool_list}", tool_list)
        .replace("{knowledge_rules}", knowledge_rules)
        .strip()
    )


# 向后兼容别名：外部若直接 import SYSTEM_PROMPT 仍然可用。
SYSTEM_PROMPT = build_system_prompt()
