"""工作目录内受限的文件读写工具。"""

import os
from pathlib import Path
from functools import lru_cache
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict
from config import WORKSPACE, ALLOWED_SUFFIXES, MAX_READ_CHARS, MAX_WRITE_CHARS
from logger import log_event
WORK_SPACE = WORKSPACE

class ReadFileInput(BaseModel):
    """读取文件内容的输入参数。"""
    path: str = Field(..., description="相对文件路径")
    model_config = ConfigDict(extra="forbid")

class WriteFileInput(BaseModel):
    """写入文件内容的输入参数。"""
    path: str = Field(..., description="相对文件路径")
    content: str = Field(..., description="要写入的内容")
    mode: Literal["w", "a"] = Field("w", description="w 覆盖，a 追加")
    model_config = ConfigDict(extra="forbid")

def safe_path(relative_path: str) -> Path:
    """解析相对路径，并拒绝工作目录外的访问。"""
    norm_path = os.path.normpath(relative_path)
    if os.path.isabs(norm_path):
        raise ValueError("不能使用绝对路径")

    path = (WORK_SPACE / norm_path).resolve()

    if not path.is_relative_to(WORK_SPACE):
        raise ValueError("路径越界：只能操作工作目录中的文件")
    if path.exists() and path.is_dir():
        raise ValueError("目标路径是目录，不允许操作")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"不支持该文件类型{path.suffix}")

    return path

@lru_cache(maxsize=32)
def _read_file_cached(resolved_path_str: str) -> str:
    return Path(resolved_path_str).read_text(encoding="utf-8")

def _read_file(inp: ReadFileInput) -> str:
    """读取白名单文件；内容过长时仅返回前 20,000 个字符。"""
    path = safe_path(inp.path)
    if not path.exists():
        raise FileNotFoundError(f"未找到文件：{inp.path}")

    content = _read_file_cached(str(path))
    if len(content) > MAX_READ_CHARS:
        log_event("tool_content_truncated", "tool", tool="read_file", path=inp.path,
                  original_length=len(content), limit=MAX_READ_CHARS)
        return content[:MAX_READ_CHARS] + f"\n文件内容过长，仅返回前 {MAX_READ_CHARS} 个字符"
    return content


def _write_file(inp: WriteFileInput) -> str:
    """以覆盖或追加模式写入白名单文件。"""
    path = safe_path(inp.path)
    content = inp.content
    mode = inp.mode

    if len(content) > MAX_WRITE_CHARS:
        log_event("tool_content_truncated", "tool", tool="write_file", path=inp.path,
                  original_length=len(content), limit=MAX_WRITE_CHARS)
        content = content[:MAX_WRITE_CHARS]

    # 自动创建父目录，保证嵌套相对路径可以直接使用。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode=mode, encoding="utf-8") as file:
        file.write(content)

    _read_file_cached.cache_clear() # 写操作后清空读缓存
    return f"已向 {path.relative_to(WORK_SPACE)} 写入 {len(content)} 个字符"


TOOL_REGISTRY = {
    "read_file": {
        "model": ReadFileInput,
        "func": _read_file,
        "description": "读取工作目录内的文本文件，返回文件内容（过长时截断）",
        "input_schema": ReadFileInput.model_json_schema(),
    },
    "write_file": {
        "model": WriteFileInput,
        "func": _write_file,
        "description": "将文本内容写入工作目录内的文件，支持覆盖(w)或追加(a)",
        "input_schema": WriteFileInput.model_json_schema(),
    },
}


def execute_tool(tool_name: str, validated_input: BaseModel) -> str:
    """按工具名称调用注册工具。"""
    if tool_name not in TOOL_REGISTRY:
        available = ", ".join(TOOL_REGISTRY.keys())
        raise ValueError(f"未知工具：{tool_name}；可用工具：{available}")
    tool = TOOL_REGISTRY[tool_name]
    if not isinstance(validated_input, tool["model"]):
        raise TypeError(f"输入类型错误：{type(validated_input)} 不是 {tool['model']}")
    return tool["func"](validated_input)

def execute_tool_from_dict(tool_name: str, raw: dict) -> str:
    if tool_name not in TOOL_REGISTRY:
        available = ", ".join(TOOL_REGISTRY.keys())
        raise ValueError(f"未知工具：{tool_name}；可用工具：{available}")
    model_cls = TOOL_REGISTRY[tool_name]["model"]
    validated = model_cls.model_validate(raw)   # Pydantic v2
    return execute_tool(tool_name, validated)

