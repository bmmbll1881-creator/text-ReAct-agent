import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import knowledge_backend
from agent import SessionBusyError, run_agent
from config import (
    EVENT_QUEUE_PUT_TIMEOUT,
    MAX_CONCURRENT_RUNS,
    MAX_QUEUE_SIZE,
    MAX_SESSION_ID_LENGTH,
    MAX_TASK_LENGTH,
    RAG_MCP_ENABLED,
    SSE_KEEPALIVE_SECONDS,
)
from config import (
    config as app_config,
)
from knowledge_backend import McpKnowledgeBackend
from logger import log_event
from mcp_client import client_from_config


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=MAX_TASK_LENGTH)
    session_id: str = Field(min_length=1, max_length=MAX_SESSION_ID_LENGTH)


_global_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时拉起 RAG MCP 子进程，退出时关闭。

    MCP 启动失败不阻断服务：文件工具仍可用，``/health`` 会报告 error。
    """
    app.state.mcp_status = "disabled"
    client = None
    if RAG_MCP_ENABLED:
        try:
            client = client_from_config(app_config)
            await client.start()
        except Exception as exc:
            client = None
            app.state.mcp_status = f"error: {exc}"
            log_event("rag_mcp_startup_error", "system", error=str(exc))
        else:
            knowledge_backend.set_backend(McpKnowledgeBackend(client))
            app.state.mcp_status = "ready"
    try:
        yield
    finally:
        knowledge_backend.set_backend(None)
        if client is not None:
            await client.close()


app = FastAPI(title="Text ReAct Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    """存活与 MCP 依赖状态；``mcp`` 取值 ready / disabled / error: ...。"""
    return {"status": "ok", "mcp": getattr(app.state, "mcp_status", "disabled")}



@app.post("/invoke",
    responses={
        409: {
            "description": "Session busy: the session is currently occupied by another request",
            "content": {"application/json": {"example": {"detail": "session busy"}}},
        },
        500: {
            "description": "Server error: unexpected failure while running the agent",
            "content": {"application/json": {"example": {"detail": "server error"}}},
        },
    },)
async def invoke(task_request: TaskRequest):
    try:
        async with _global_semaphore:
            result = await run_agent(task_request.task, task_request.session_id)
        return {"session_id": task_request.session_id, "result": result}
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail="session busy") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="server error") from exc

def _format_sse(event: str, data: dict[str, Any]) -> str:
    """格式化 SSE 消息 / Format an SSE message."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _send_final_result(agent_task: asyncio.Task, format_sse) -> AsyncIterator[str]:
    """发送 Agent 任务的最终结果或错误 / Send the final result or error."""
    try:
        result = agent_task.result()
        yield format_sse("done", {"result": result})
    except asyncio.CancelledError:
        # 任务被取消，直接抛出让上层处理 / Propagate cancellation to the caller
        raise
    except Exception as exc:
        # 捕获普通异常（如 MaxStepsExceeded 等） / Report ordinary agent errors
        yield format_sse("error", {"detail": str(exc)})


async def _event_generator(
    queue: asyncio.Queue,
    agent_task: asyncio.Task,
    format_sse,
) -> AsyncIterator[str]:
    """从队列读取事件并转换为 SSE / Read queued events and format SSE output."""
    try:
        async for sse in _sse_event_generator(agent_task, format_sse, queue):
            yield sse

        # 清空队列中剩余的所有事件 / Drain remaining queued events
        while not queue.empty():
            event, data = queue.get_nowait()
            yield format_sse(event, data)

        # 发送最终结果（成功或错误） / Send the final result or error
        async for sse in _send_final_result(agent_task, format_sse):
            yield sse

    except asyncio.CancelledError:
        # 客户端断开连接，取消 Agent 任务并等待清理 / Cancel the agent after client disconnect
        if not agent_task.done():
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        raise
    finally:
        # 确保 Agent 任务被清理 / Always clean up the agent task
        if not agent_task.done():
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)


async def _sse_event_generator(agent_task: asyncio.Task, format_sse, queue: asyncio.Queue):
    while True:
            # 如果 Agent 任务已完成且队列为空，结束循环 / Stop when task and queue are complete
        if agent_task.done() and queue.empty():
            break

        try:
                # 等待新事件，15 秒超时用于发送 keep-alive / Wait for events and emit keep-alives
                event, data = await asyncio.wait_for(
                    queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                )
                yield format_sse(event, data)
        except TimeoutError:
                # 超时未获取到事件，检查是否需要退出 / Check completion after timeout
            if agent_task.done() and queue.empty():
                break
            # 发送 keep-alive 注释 / Send an SSE keep-alive comment
            yield ": keep-alive\n\n"


@app.post("/stream")
async def stream(task_request: TaskRequest):
    """流式返回 ReAct Agent 事件 / Stream ReAct Agent events as SSE."""
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)

    async def emit(event: str, data: dict[str, Any]) -> None:
        try:
            # 优先等待队列腾出空间，超时后降级丢弃 / Wait for queue space, then drop on timeout
            await asyncio.wait_for(queue.put((event, data)), timeout=EVENT_QUEUE_PUT_TIMEOUT)
        except TimeoutError:
            log_event(f"事件队列已满，丢弃事件: {event}", task_request.session_id)

    async def run_limited():
        """在全局信号量限制下运行 Agent。"""
        async with _global_semaphore:
            return await run_agent(
                task_request.task,
                task_request.session_id,
                on_event=emit,
            )

    agent_task = asyncio.create_task(run_limited())

    return StreamingResponse(
        _event_generator(queue, agent_task, _format_sse),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no-cache",
        },
    )
