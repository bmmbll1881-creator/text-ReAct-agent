"""工作目录内受限的文件读写工具。"""

import os
from pathlib import Path

MAX_READ_CHS = 20_000
MAX_WRITE_CHS = 1_000_000
WORK_SPACE = Path(os.getenv("WORK_SPACE", Path.cwd())).resolve()
ALLOWED_SUFFIXES = {".txt", ".json", ".md"}


def safe_path(relative_path: str) -> Path:
    """解析相对路径，并拒绝工作目录外的访问。"""
    path = (WORK_SPACE / relative_path).resolve()
    if not path.is_relative_to(WORK_SPACE):
        raise ValueError("路径越界：只能操作工作目录中的文件")
    return path


def read_file(inp: dict) -> str:
    """读取白名单文件；内容过长时仅返回前 20,000 个字符。"""
    if not isinstance(inp, dict) or not isinstance(inp.get("path"), str):
        raise ValueError("path 必须是字符串")
    path = safe_path(inp["path"])
    if not path.exists():
        raise FileNotFoundError(f"未找到文件：{inp['path']}")
    if not path.is_file():
        raise ValueError("目标路径不是文件")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("不支持该文件类型")
    content = path.read_text(encoding="utf-8")
    if len(content) > MAX_READ_CHS:
        return content[:MAX_READ_CHS] + f"\n文件内容过长，仅返回前 {MAX_READ_CHS} 个字符"
    return content


def write_file(inp: dict) -> str:
    """以覆盖或追加模式写入白名单文件。"""
    if not isinstance(inp, dict) or not isinstance(inp.get("path"), str):
        raise ValueError("path 必须是字符串")
    path = safe_path(inp["path"])
    content = inp.get("content", "")
    mode = inp.get("mode", "w")
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")
    if mode not in {"w", "a"}:
        raise ValueError("mode 只能是 w 或 a")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("不支持该文件类型")
    if path.exists() and path.is_dir():
        raise ValueError("不能向目录写入内容")
    if len(content) > MAX_WRITE_CHS:
        raise ValueError("写入内容过长")

    # 自动创建父目录，保证嵌套相对路径可以直接使用。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode=mode, encoding="utf-8") as file:
        file.write(content)
    return f"已向 {path.relative_to(WORK_SPACE)} 写入 {len(content)} 个字符"


TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件内容",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对文件路径"}},
            "required": ["path"],
        },
        "run": read_file,
    },
    {
        "name": "write_file",
        "description": "写入文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
                "mode": {"type": "string", "enum": ["w", "a"], "description": "w 覆盖，a 追加"},
            },
            "required": ["path", "content"],
        },
        "run": write_file,
    },
]
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def execute_tool(tool_name: str, tool_inp: dict) -> str:
    """按工具名称调用注册工具。"""
    if tool_name not in TOOLS_BY_NAME:
        available = ", ".join(TOOLS_BY_NAME)
        raise ValueError(f"未知工具：{tool_name}；可用工具：{available}")
    return TOOLS_BY_NAME[tool_name]["run"](tool_inp)
