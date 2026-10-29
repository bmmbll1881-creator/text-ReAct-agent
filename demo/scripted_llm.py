"""Deterministic ReAct replies for offline demos and evaluations."""

from __future__ import annotations

import json
import re
from typing import Any

# 文件路径：扩展名必须以字母开头，排除 URL 片段、1.2.3 之类版本号。
_PATH_RE = re.compile(
    r"(?<![\w:/])"
    r"(?:[\w.-]+/)*"
    r"[\w.-]*"
    r"\.[A-Za-z][A-Za-z0-9]{0,9}"
    r"(?![\w])"
)

_ACTION_LINE_RE = re.compile(r"^\s*Action\s*:\s*([\w.-]+)\s*$", re.MULTILINE)
_ACTION_INPUT_RE = re.compile(r"Action\s*Input\s*:\s*")

_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")

_OBSERVATION_ROLES = {"user", "tool", "function", "observation"}


class ScriptedLLM:
    """Return predictable ReAct turns based on the task and tool observations."""

    def __init__(self, task: str | None = None, replies: list[str] | None = None):
        self.task = task or ""
        self.replies = list(replies or [])

    async def __call__(
        self,
        messages: list[dict[str, str]],
        skill_prompt: str | None = None,
        session_id: str = "",
        step: int | None = None,
    ) -> str:
        del skill_prompt, session_id, step

        if self.replies:
            return self.replies.pop(0)

        task = self._resolve_task(messages)
        assistant_turns = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
        last = assistant_turns[-1] if assistant_turns else ""

        if "Final Answer:" in last:
            return "Final Answer: 任务已完成。"

        actions = self._extract_actions(assistant_turns)
        pairs = self._pair_actions_with_observations(messages)

        if "experiment/config.json" in task and "experiment/results.md" in task:
            return self._experiment_summary_reply(actions, pairs)

        path = self._extract_path(task)
        if "追加" in task or "append" in task.lower():
            content = self._extract_append_content(task)
            return self._maybe_finish(
                actions,
                "write_file",
                {"path": path, "content": content, "mode": "a"},
                "追加实验日志。",
            )
        if any(word in task for word in ("创建", "写入", "生成", "写", "create", "write")):
            content = self._extract_write_content(task)
            return self._maybe_finish(
                actions,
                "write_file",
                {"path": path, "content": content, "mode": "w"},
                "写入目标文件。",
            )
        return self._maybe_finish(
            actions,
            "read_file",
            {"path": path},
            "读取指定文件。",
        )

    # --- task / history parsing -------------------------------------------------

    def _resolve_task(self, messages: list[dict[str, str]]) -> str:
        if self.task:
            return self.task
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                first_line = content.splitlines()[0] if content else ""
                return first_line or content
        return ""

    @staticmethod
    def _extract_actions(assistant_turns: list[str]) -> list[tuple[str, dict[str, Any]]]:
        actions: list[tuple[str, dict[str, Any]]] = []
        for turn in assistant_turns:
            name_match = _ACTION_LINE_RE.search(turn)
            if not name_match:
                continue
            payload = _parse_action_input(turn) or {}
            actions.append((name_match.group(1), payload))
        return actions

    @staticmethod
    def _pair_actions_with_observations(
        messages: list[dict[str, str]],
    ) -> list[tuple[tuple[str, dict[str, Any]], str]]:
        pairs: list[tuple[tuple[str, dict[str, Any]], str]] = []
        pending: tuple[str, dict[str, Any]] | None = None
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "assistant":
                name_match = _ACTION_LINE_RE.search(content)
                if name_match:
                    pending = (name_match.group(1), _parse_action_input(content) or {})
                continue
            if pending is not None and role in _OBSERVATION_ROLES:
                pairs.append((pending, content))
                pending = None
        return pairs

    # --- branches ---------------------------------------------------------------

    def _experiment_summary_reply(
        self,
        actions: list[tuple[str, dict[str, Any]]],
        pairs: list[tuple[tuple[str, dict[str, Any]], str]],
    ) -> str:
        config_path = "experiment/config.json"
        results_path = "experiment/results.md"
        summary_path = "experiment/summary.md"

        if _has_action(actions, "write_file", path=summary_path):
            return "Final Answer: 任务已完成。"
        if not _has_action(actions, "read_file", path=config_path):
            return self._action("读取实验配置。", "read_file", {"path": config_path})
        if not _has_action(actions, "read_file", path=results_path):
            return self._action("读取实验结果。", "read_file", {"path": results_path})

        config_obs = _observation_for(pairs, "read_file", config_path) or ""
        results_obs = _observation_for(pairs, "read_file", results_path) or ""

        model = _extract_model(config_obs) or "实验模型"
        result = _extract_percent(results_obs) or "结果详见实验记录"
        content = f"实验模型：{model}\n关键结果：{result}\n"
        return self._action(
            "整理结果并写入摘要。",
            "write_file",
            {
                "path": summary_path,
                "content": content,
                "mode": "w",
            },
        )

    def _maybe_finish(
        self,
        actions: list[tuple[str, dict[str, Any]]],
        name: str,
        payload: dict[str, Any],
        thought: str,
    ) -> str:
        # 写动作按 path + mode 判定，读动作只按 path 判定，避免内容微差导致重复执行。
        keys: dict[str, Any] = {"path": payload.get("path")}
        if name == "write_file":
            keys["mode"] = payload.get("mode")
        if _has_action(actions, name, **keys):
            return "Final Answer: 任务已完成。"
        return self._action(thought, name, payload)

    # --- text extraction --------------------------------------------------------

    @staticmethod
    def _extract_path(task: str) -> str:
        for match in _PATH_RE.finditer(task):
            window = task[max(0, match.start() - 12) : match.start()]
            if "://" in window:
                continue
            return match.group(0)
        return "note.txt"

    @staticmethod
    def _extract_append_content(task: str) -> str:
        # “追加/append 之后的内容”，去掉尾部的 “到 <path>”
        m = re.search(r"(?:追加|append)\s*(?:内容\s*)?[：:]?\s*(?P<c>.+)", task, re.IGNORECASE)
        if m:
            content = m.group("c").strip()
            content = re.sub(r"\s*(?:到|至|into|to)\s+\S+\s*$", "", content, flags=re.IGNORECASE)
            content = content.strip(" 。.：:\"'“”")
            if content:
                return content
        # “把 X 追加/append 到 Y”
        m = re.search(r"(?:把|将)\s*(?P<c>.+?)\s*(?:追加|append)", task, re.IGNORECASE)
        if m:
            content = m.group("c").strip(" 把将。.：:")
            if content:
                return content
        return "运行完成"

    @staticmethod
    def _extract_write_content(task: str) -> str:
        # 1) 引号内容
        m = re.search(r"[“\"'](?P<c>[^”\"']+)[”\"']", task)
        if m:
            return m.group("c")
        # 2) “内容为/内容：”
        m = re.search(r"(?:内容(?:为|是)?|写入内容)\s*[：:]?\s*(?P<c>.+?)(?:[，。]|$)", task)
        if m:
            content = m.group("c").strip()
            if content:
                return content
        # 3) “写入 X”，但排除纯路径
        m = re.search(r"写入\s*[：:]?\s*(?P<c>.+?)(?:[，。]|$)", task)
        if m:
            content = m.group("c").strip()
            if content and not _PATH_RE.fullmatch(content):
                return content
        return "实验记录"

    @staticmethod
    def _action(thought: str, name: str, payload: dict[str, Any]) -> str:
        return f"Thought: {thought}\nAction: {name}\nAction Input: {json.dumps(payload, ensure_ascii=False)}"


# --- helpers -------------------------------------------------------------------


def _parse_action_input(turn: str) -> dict[str, Any] | None:
    match = _ACTION_INPUT_RE.search(turn)
    if not match:
        return None
    rest = turn[match.end() :].lstrip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _has_action(
    actions: list[tuple[str, dict[str, Any]]],
    name: str,
    **expected: Any,
) -> bool:
    for action_name, payload in actions:
        if action_name != name:
            continue
        if all(payload.get(key) == value for key, value in expected.items()):
            return True
    return False


def _observation_for(
    pairs: list[tuple[tuple[str, dict[str, Any]], str]],
    name: str,
    path: str,
) -> str | None:
    for (action_name, payload), observation in pairs:
        if action_name == name and payload.get("path") == path:
            return observation
    return None


def _extract_model(observation: str) -> str | None:
    match = _MODEL_RE.search(observation)
    return match.group(1) if match else None


def _extract_percent(observation: str) -> str | None:
    match = _PERCENT_RE.search(observation)
    return match.group(0) if match else None
