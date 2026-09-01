"""解析模型返回的 ReAct 文本协议。"""

import json
import re
from dataclasses import dataclass


@dataclass
class ParseResult:
    """一次协议解析的结果。"""

    thought: str = ""
    action: str | None = None
    action_input: dict | None = None
    final_answer: str | None = None
    error: str | None = None


def _strip_fences(text: str) -> str:
    """移除 Markdown 代码围栏，保留其中的协议内容。"""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()


def _extract_json_object(text: str, start: int) -> tuple[dict | None, int | None]:
    """从指定位置读取一个完整且独占后续内容的 JSON 对象。"""
    remainder = text[start:].lstrip()
    if not remainder.startswith("{"):
        return None, None
    brace_start = start + len(text[start:]) - len(remainder)
    end_pos = _find_matching_brace(text, brace_start)
    if end_pos is None or text[end_pos + 1:].strip():
        return None, None
    try:
        return json.loads(text[brace_start:end_pos + 1]), end_pos + 1
    except json.JSONDecodeError:
        return None, None


def _find_matching_brace(text: str, brace_start: int) -> int | None:
    """找到起始左花括号对应的右花括号，忽略字符串内的括号。"""
    if text[brace_start] != "{":
        return None
    in_string = False
    escape = False
    depth = 0
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def parse_response(text: str) -> ParseResult:
    """优先解析最终答复，否则解析一次工具调用。"""
    text = _strip_fences(text)
    thought_match = re.search(
        r"Thought\s*:\s*((?:(?!^\s*(?:Action|Final Answer)\s*:)[\s\S])+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    thought = thought_match.group(1).strip() if thought_match else ""

    final_match = re.search(
        r"Final Answer\s*:\s*((?:(?!^\s*(?:Action|Final Answer)\s*:)[\s\S])+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if final_match:
        return ParseResult(thought=thought, final_answer=final_match.group(1).strip())

    action_match = re.search(r"Action\s*:\s*(\w+)", text, re.IGNORECASE)
    if not action_match:
        return ParseResult(thought=thought, error="缺少 Action 或 Action Input")

    input_header = re.search(r"Action Input\s*:\s*", text, re.IGNORECASE)
    if not input_header:
        return ParseResult(thought=thought, action=action_match.group(1), error="缺少 Action Input")

    action_input, _ = _extract_json_object(text, input_header.end())
    if not isinstance(action_input, dict):
        return ParseResult(thought=thought, action=action_match.group(1), error="Action Input 必须是有效的 JSON 对象")
    return ParseResult(thought=thought, action=action_match.group(1), action_input=action_input)
