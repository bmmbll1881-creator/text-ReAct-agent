# logger.py 日志
import hashlib
from datetime import datetime, timezone
import json

# 配置开关：默认关闭敏感信息调试模式
SENSITIVE_DEBUG = False

def set_sensitive_debug(flag: bool):
    """开启或关闭敏感信息调试模式（开启后会记录原始敏感信息）"""
    global SENSITIVE_DEBUG
    SENSITIVE_DEBUG = flag

def _redact_sensitive(key, value):
    """
    根据键名对敏感信息进行脱敏处理。
    - API_KEY 类：不记录值，替换为 "[REDACTED]"
    - 文件内容类：默认只记录长度和 sha256 前 8 位，调试模式下记录原文
    """
    key_lower = key.lower()

    # 屏蔽 敏感字段
    if any(marker in key_lower for marker in ("api_key", "apikey", "token", "secret", "password", "authorization")):
        return '[REDACTED]'

    # 处理文件内容
    if 'content' in key_lower or 'file' in key_lower:
        if SENSITIVE_DEBUG:
            return value
        # 计算长度和哈希摘要
        if isinstance(value, str):
            content = value.encode('utf-8')
        elif isinstance(value, bytes):
            content = value
        else:
            # 非字符串/字节类型，返回其字符串表示的长度
            return f"<len={len(str(value))}>"
        sha256 = hashlib.sha256(content).hexdigest()[:8]
        return f"<len={len(content)} sha256={sha256}>"

    # 其他情况原样返回
    return value

def log_event(event_type: str, session_id: str, step: str | None = None, **extra):
    """
    输出单行 JSON 日志到 stdout。

    参数：
        event_type: 事件类型
        session_id: 会话 ID
        step: 可选，步骤名
        **extra: 额外键值对，将经过敏感信息脱敏后包含在日志中
    """
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "session_id": session_id
    }

    if step is not None:
        log_entry["step"] = step

    # 处理额外字段，过滤并脱敏
    for key, value in extra.items():
        if key in ("timestamp", "event", "session_id", "step"):
            # 避免覆盖基础字段
            continue
        log_entry[key] = _redact_sensitive(key, value)

    # 输出单行 JSON，default=str 处理不可序列化对象
    print(json.dumps(log_entry, default=str), flush=True)
