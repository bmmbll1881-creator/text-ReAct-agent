from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

def _required_url(name: str) -> str:
    """获取必需的 URL 配置项。
    参数：
        name: 环境变量名称
    返回：
        去除首尾空格后的 URL 字符串
    异常：
        ValueError: 当环境变量不存在或为空时抛出
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} 是必需的配置项")
    return value

def _int_env(name: str, default: int, *, minimum: int | None = None, positive: bool = False) -> int:
    """从环境变量读取整数配置。
    参数：
        name: 环境变量名称
        default: 默认值
        minimum: 最小值限制（可选）
        positive: 是否必须为正数
    返回：
        解析后的整数值
    异常：
        ValueError: 当值不是整数、小于最小值或不为正数时抛出
    """
    raw = os.getenv(name)
    try:
        # 如果环境变量为空或未设置，使用默认值
        value = default if raw is None or not raw.strip() else int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc

    # 验证正数要求
    if positive and value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    # 验证最小值要求
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 必须至少为 {minimum}")
    return value


def _float_env(name: str, default: float, *, positive: bool = False) -> float:
    """从环境变量读取浮点数配置。
    参数：
        name: 环境变量名称
        default: 默认值
        positive: 是否必须为正数
    返回：
        解析后的浮点数值
    异常：
        ValueError: 当值不是数字或不为正数时抛出
    """
    raw = os.getenv(name)
    try:
        # 如果环境变量为空或未设置，使用默认值
        value = default if raw is None or not raw.strip() else float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc

    # 验证正数要求
    if positive and value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _workspace() -> Path:
    """获取工作空间路径。
    WORKSPACE 是标准写法；WORK_SPACE 保持支持。
    返回：
        解析后的绝对路径
    异常：
        ValueError: 当路径为空时抛出
    """
    raw = os.getenv("WORKSPACE") or os.getenv("WORK_SPACE") or "./workspace"
    if not raw.strip():
        raise ValueError("WORKSPACE 不能为空")
    # 展开用户目录（~）并解析为绝对路径
    return Path(raw).expanduser().resolve()


def _suffixes() -> frozenset[str]:
    """获取允许的文件后缀集合。
    从 ALLOWED_SUFFIXES 环境变量解析，用逗号分隔。
    返回：
        不可变的文件后缀集合（如 {'.txt', '.md', '.py'}）
    异常：
        ValueError: 当没有有效后缀或后缀不以 '.' 开头时抛出
    """
    raw = os.getenv(
        "ALLOWED_SUFFIXES",
        ".txt,.md,.py,.json,.yml,.yaml,.env.example",
    )
    # 分割字符串，去除空格，过滤空项，转为不可变集合
    values = frozenset(item.strip() for item in raw.split(",") if item.strip())

    if not values:
        raise ValueError("ALLOWED_SUFFIXES 必须包含至少一个后缀")
    if any(not item.startswith(".") for item in values):
        raise ValueError("ALLOWED_SUFFIXES 的每个条目必须以 '.' 开头")
    return values


@dataclass(frozen=True, slots=True)
class Config:
    """已验证的运行时设置。
    ``CHAT_URL`` 是唯一必需的设置。这样既保持本地开发简单，
    又能在未配置 LLM 端点时快速失败。
    """

    max_steps: int  # 最大执行步骤数
    chat_url: str  # 聊天服务 URL
    llm_model: str  # LLM 模型名称
    workspace: Path  # 工作空间路径
    redis_url: str  # Redis 连接 URL
    session_ttl_seconds: int  # 会话过期时间（秒）
    tool_timeout: float  # 工具调用超时（秒）
    llm_timeout: float  # LLM 调用超时（秒）
    context_token_limit: int  # 上下文 token 限制
    context_keep_recent_messages: int  # 保留的最近消息数量
    max_read_chars: int  # 读取文件最大字符数
    max_write_chars: int  # 写入文件最大字符数
    allowed_suffixes: frozenset[str]  # 允许的文件后缀集合

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量创建 Config 实例。
        返回：
            包含所有配置的 Config 对象
        """
        return cls(
            # 最大步骤数，默认为 10，必须为正数
            max_steps=_int_env("MAX_STEPS", 10, positive=True),
            # 必需的聊天服务 URL
            chat_url=_required_url("CHAT_URL"),
            # LLM 模型，默认为 gpt-3.5-turbo
            llm_model=(os.getenv("LLM_MODEL") or "deepseek-chat").strip(),
            # 工作空间路径
            workspace=_workspace(),
            # Redis URL，默认为本地
            redis_url=(os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip(),
            # 会话过期时间，默认 3600 秒（1小时）
            session_ttl_seconds=_int_env("SESSION_TTL_SECONDS", 3600, positive=True),
            # 工具超时，默认 30 秒
            tool_timeout=_float_env("TOOL_TIMEOUT", 30.0, positive=True),
            # LLM 超时，默认 120 秒
            llm_timeout=_float_env("LLM_TIMEOUT", 120.0, positive=True),
            # 上下文 token 限制，默认 4000
            context_token_limit=_int_env("CONTEXT_TOKEN_LIMIT", 4000, positive=True),
            # 保留最近消息数，默认 4，最小为 0
            context_keep_recent_messages=_int_env(
                "CONTEXT_KEEP_RECENT_MESSAGES", 4, minimum=0
            ),
            # 读取文件最大字符数，默认 100000
            max_read_chars=_int_env("MAX_READ_CHARS", 100_000, positive=True),
            # 写入文件最大字符数，默认 100000
            max_write_chars=_int_env("MAX_WRITE_CHARS", 100_000, positive=True),
            # 允许的文件后缀
            allowed_suffixes=_suffixes(),
        )


# 在导入时快速失败，同时保持简单的模块级访问
config = Config.from_env()

# 模块级常量，方便直接导入使用
MAX_STEPS = config.max_steps  # 最大步骤数
CHAT_URL = config.chat_url  # 聊天服务 URL
LLM_MODEL = config.llm_model  # LLM 模型
WORKSPACE = config.workspace  # 工作空间
WORK_SPACE = WORKSPACE  # 向后兼容的别名
REDIS_URL = config.redis_url  # Redis URL
SESSION_TTL_SECONDS = config.session_ttl_seconds  # 会话过期时间
TOOL_TIMEOUT = config.tool_timeout  # 工具超时
LLM_TIMEOUT = config.llm_timeout  # LLM 超时
CONTEXT_TOKEN_LIMIT = config.context_token_limit  # 上下文 token 限制
CONTEXT_KEEP_RECENT_MESSAGES = config.context_keep_recent_messages  # 保留消息数
MAX_READ_CHARS = config.max_read_chars  # 读取最大字符数
MAX_WRITE_CHARS = config.max_write_chars  # 写入最大字符数
ALLOWED_SUFFIXES = config.allowed_suffixes  # 允许的文件后缀