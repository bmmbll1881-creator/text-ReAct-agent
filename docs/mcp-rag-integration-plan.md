# MCP 增强型证据约束 ReAct Agent —— 修订版实施方案（v2）

> 适用范围：`E:\PycharmProjects\text-ReAct-agent`（下称 Agent 侧）+ `E:\PycharmProjects\evidence-reflective-trace-rag`（下称 RAG 侧）
> 修订依据：对两侧代码的实际核对 + 对现有 MCP stdio server 的实测（2026-10-28）

---

## 0. 结论摘要

原规划方向正确，可以直接作为骨架：**保留进程边界、只暴露 `knowledge_search` 一个稳定工具、改造工具执行链支持异步、用 abstain 约束防止绕过证据门控**，这四点判断都成立，无需推翻。

但原规划存在 **6 处会导致实现失败或结论错误的问题**，必须在动手前修正：

| 编号 | 问题 | 严重度 | 影响 |
|---|---|---|---|
| C1 | `Run` 模型没有 `citations` / `failure_type` 字段，规划中的返回契约无法直接产出 | P0 | 契约写不出来，实现时随意编造 |
| C2 | `route` 是 4 值枚举（success/clarify/abstain/verification_failed），规划只处理了 2 值 | P0 | clarify 被当成成功、verification_failed 被当成可信答案 |
| C3 | 异步改造方案在现有 `_execute_action` 上会静默失败（`to_thread` 返回 coroutine 被 f-string 序列化） | P0 | observation 变成 `<coroutine object ...>`，Agent 空转 |
| C4 | `TOOL_REGISTRY` 条目必须含 `func`，只加 `model` 会让 `execute_tool` 抛 KeyError | P0 | 工具注册即崩 |
| C5 | 演示场景三选错了问题：自动续约在实测中是 `route=success`，答案文本为"合同未约定自动续约期限" | P0 | Demo 与简历描述被实测打脸 |
| C6 | 未处理单 session 并发、FastAPI 无 lifespan、observation 未截断（`CONTEXT_TOKEN_LIMIT=4000`） | P1 | 并发串号、启动泄漏、上下文被挤爆触发压缩 |

另有 5 处改进（B1–B5），见第 3 节。

---

## 1. 实测事实（用于校准规划）

已在本机用真实 stdio MCP 客户端调用现有 `mcp_server.py`（`RAG_DATA_DIR` 指向临时种子目录）：

```
TOOLS: ['rag_query', 'rag_retrieve', 'rag_run_detail']

Q: 2025年第三季度营业收入和净利润是多少？（financial_report）
   route=success  answer='2025年第三季度营业收入为100万元，净利润为20万元。 [S1]'

Q: 提前终止时需要承担违约责任吗？（contract）
   route=success  answer='任一方提前终止合同，应按照合同约定承担违约责任。 [S1]\n合同未约定提前终止后的具体赔偿金额。 [S2]\n合同未约定自动续约期限。 [S3]'

Q: 合同中是否规定了自动续约期限？（contract）
   route=success  ← 注意：不是 abstain！
   answer='合同未约定自动续约期限。 [S1]\n合同未约定提前终止后的具体赔偿金额。 [S2]\n...'

Q: 合同的违约金比例是多少？（contract）
   route=abstain  answer=None
   stages=['query_received','retrieval','evidence_gate','reflection','retrieval','evidence_gate','failure_attribution']
   failure_type=insufficient_coverage
```

由此得到 4 个必须写进规划的事实：

1. **现有 MCP Server 可用**，无需重写；`rag_query` 已能返回 `[S1]` 引用，`rag_run_detail` 能返回完整 trace 与 `failure_report.failure_type`。
2. **abstain 的判定标准是"证据门控拒绝"**，不是"答案是否定式的"。"合同未约定 X" 属于有证据的成功回答（`route=success`）。规划中的场景三必须换成"违约金比例"这类真正无证据的问题。
3. **抽取式答案会把 EvidenceSet 里所有 hit 的首句全部拼接**（`ExtractiveAnswerClient`），因此 answer 中会出现与问题无关的 S2/S3。这既是噪音源，也是 Agent 幻觉的温床 —— 必须在 observation 层做裁剪。
4. **答案中的引用格式是 `[S1]`，且前面有一个空格**，正则为 `\[S(\d+)\]`。

---

## 2. 逐条修正

### C1 `citations` / `failure_type` 的来源

`Run{id, knowledge_base_id, question, status, route, answer, created_at, completed_at}` —— 没有引用与失败字段。可用来源：

- `citations`：对 `run.answer` 跑 `re.findall(r"\[S(\d+)\]")` 得到 `["S1","S3"]`；再用 `repository.list_traces(run_id)` 中 `answer_verification` 的 `verified_citation_ids` 做交叉校验（不一致时以 trace 为准并在 `message` 里标注）。
- `failure_type`：`repository.get_failure_report(run_id).failure_type.value`（仅 abstain / verification_failed 有值）。
- `chunk_id` 映射：`retrieval` trace 的 `hits` 顺序即 `S1..Sn`（`ExtractiveAnswerClient` 用 `enumerate(evidence.hits, 1)`），可据此给出 `citation_id → chunk_id`。

结论：契约可以实现，但**必须在 `mcp_server.py` 内新增一个 `_shape_result()` 函数**统一装配，不能让客户端各自猜。

### C2 四值 route → 显式 status 映射

```
Route.SUCCESS             → status="success"              可引用作答
Route.CLARIFY             → status="clarify"              问题歧义，需追问，禁止作答
Route.ABSTAIN             → status="abstain"              证据不足，禁止作答
Route.VERIFICATION_FAILED → status="verification_failed"  有草稿答案但校验未通过，禁止当结论使用
（参数/配置错误）          → MCP isError=True，抛 ValueError
```

`status` 与 `route` 同时返回（`route` 保留原始枚举值，便于追溯）；Agent 只对 `success` 保留引用作答。

### C3 异步执行链的正确改法

现状（`agent.py:317`）：

```python
tool_result = await asyncio.wait_for(
    asyncio.to_thread(tool_executor, tool_name, validated_input), TOOL_TIMEOUT)
```

若 `tool_executor` 变成 async 函数，`to_thread` 拿到的是 coroutine 对象，随后 `f"工具输出: {tool_result}"` 会输出 `<coroutine object ...>` 并触发 `RuntimeWarning: never awaited`，Agent 不会报错但永远拿不到结果 —— 这是最隐蔽的坑。

正确做法（二选一，推荐 A）：

**A. 显式分流**

```python
ToolExecutor = Callable[[str, Any], str | Awaitable[str]]  # 类型放宽

async def _run_tool(tool_executor, tool_name, validated_input, timeout):
    if inspect.iscoroutinefunction(tool_executor):
        return await asyncio.wait_for(tool_executor(tool_name, validated_input), timeout)
    return await asyncio.wait_for(
        asyncio.to_thread(tool_executor, tool_name, validated_input), timeout)
```

优点：现有测试传 `tool_executor=lambda *_: "ok"` 无需改动，保持向后兼容。

**B. 统一异步 dispatcher**

`execute_tool_async()` 作为唯一入口，内部对本地工具 `await asyncio.to_thread(...)`、对 MCP 工具 `await backend.search(...)`；`run_agent` 的 `tool_executor` 默认值改为该 dispatcher。

最终建议：**A 用于 `agent.py` 的兼容层，B 作为默认 dispatcher**（两者不冲突，B 本身是 `async def`，走 A 的 coroutine 分支）。

超时也要区分：本地文件 IO 用 `TOOL_TIMEOUT`（30s），MCP 用独立的 `RAG_MCP_TOOL_TIMEOUT`（建议 60s，因为 RAG 内部可能有 2 轮检索 + 1 轮 LLM 反思）。

### C4 工具注册表结构

现状 `execute_tool()` 强依赖 `tool["func"]` 且校验 `isinstance(validated_input, tool["model"])`。因此：

- `TOOL_REGISTRY`（`agent.py` 用于查 `model` 做校验）必须是**合并视图**，包含 `knowledge_search` 的 `model`；
- `execute_tool()`（本地工具同步入口）只能服务含 `func` 的本地工具，遇到 `knowledge_search` 应抛 `ValueError("MCP 工具必须通过 execute_tool_async 调用")`；
- 建议拆成两份源数据，避免"注册了但调不通"的半成品状态：

```python
LOCAL_TOOL_REGISTRY = {"read_file": {...}, "write_file": {...}}   # 含 func
MCP_TOOL_SPECS      = {"knowledge_search": {"model": ..., "description": ...}}
TOOL_REGISTRY       = {**LOCAL_TOOL_REGISTRY, **MCP_TOOL_SPECS}   # 供校验与提示词使用
```

### C5 演示场景修正

| 场景 | 问题 | 期望 status | 备注 |
|---|---|---|---|
| 1 财务成功 | 2025年第三季度营业收入和净利润是多少？（financial_report） | success | 实测通过，含 [S1] |
| 2 合同成功 | 提前终止时需要承担违约责任吗？（contract） | success | 实测通过 |
| 3 真实拒答 | 合同的违约金比例是多少？（contract） | abstain / insufficient_coverage | 实测通过，替换原"自动续约" |
| 4 联合任务 | 读取 experiment/results.md 并结合合同知识库判断交付责任 | success | 需先 `write_file` 造样例文件 |
| 5（可选）陷阱演示 | 合同中是否规定了自动续约期限？ | success，答案文本为"未约定" | 用来演示"success ≠ 肯定回答"，写进 README 很有说服力 |

### C6 生命周期、并发与 observation 体积

1. **FastAPI 无 lifespan**（`api.py` 目前只有 `@app.get("/health")`）。必须新增 `lifespan`，在 `yield` 前 `await rag_client.start()`，`yield` 后 `await rag_client.stop()`。
2. **uvicorn `--workers > 1` 会起 N 个 MCP 子进程**。Demo 固定 `workers=1`，并在 README 标注。
3. **单 session 并发**：`MAX_CONCURRENT_RUNS=10`，`ClientSession` 可并发发请求，但 RAG 侧 `Database` 用单一 `threading.Lock` + SQLite，实际会串行化；同时 `RagGraphService` 在事件循环里做同步 SQLite 写入，会阻塞。Demo 可接受，但要在文档写明"并发上限 ≈ 1~2 有效并行"，并给 `McpKnowledgeBackend` 加 `asyncio.Semaphore(RAG_MCP_MAX_CONCURRENCY=2)` 兜底。
4. **observation 截断**：`CONTEXT_TOKEN_LIMIT=4000`，一旦超限会触发 `compress_conversation`（额外一次 LLM 调用 + 历史被改写，多步任务容易在中途丢上下文）。`knowledge_search` 的 observation 必须严格结构化并截断：

```python
def format_observation(payload: dict, limit: int = 4000) -> str:
    body = json.dumps(payload, ensure_ascii=False)
    if len(body) > limit:
        payload["evidence"] = payload.get("evidence", [])[:2]
        payload["truncated"] = True
        body = json.dumps(payload, ensure_ascii=False)[:limit] + "…(已截断)"
    return f"工具输出(JSON): {body}"
```

---

## 3. 在原规划基础上的 5 项改进

### B1 把 abstain/clarify 约束从"提示词"下沉为"代码"

原规划第 9 节把 `route=abstain 时不得编造` 写成系统提示词第 3 条。提示词约束是概率性的，而这条恰恰是整个项目最有价值的卖点（证据门控不被绕过）。改为**代码级硬约束**：

`_execute_action` 返回 observation 时，若 `status in {"abstain","clarify","verification_failed"}`，追加一条确定性的 user 消息（不是提示词，是历史里的真实指令）：

```
[系统约束] knowledge_search 返回 status=abstain（failure_type=insufficient_coverage）。
你不得基于常识或推测补充答案；若无法从知识库获得证据，必须明确说明证据不足，并给出可执行的下一步（如补充文档、缩小问题范围）。
```

这样即使模型想"帮忙"，看到的也是明确的运行时指令，且该约束在每一轮都存在于历史中，而非仅依赖 system prompt 的注意力。

### B2 工具清单由注册表生成，消除提示词漂移

`prompts.py` 现在硬编码了 2 个工具的说明。加入 `knowledge_search` 后，`TOOL_REGISTRY` 与提示词就成了两份需要手工同步的真相，必然漂移。改为：

```python
def build_system_prompt() -> str:
    lines = [f"{i}. {name}：{meta['description']}\n   输入 {json.dumps(meta['input_schema'], ensure_ascii=False)}"
             for i, (name, meta) in enumerate(TOOL_REGISTRY.items(), 1)]
    return SYSTEM_PROMPT_TEMPLATE.format(tool_list="\n".join(lines))
```

注意 `agent.py` 里有两处使用 `SYSTEM_PROMPT`（`_build_full_messages` 与 `HistoryManager.load`），改造成函数后两处都要走同一入口，避免"新会话有知识库工具、恢复会话没有"的错位。

### B3 RAG 侧启动预热与就绪校验

`get_container()` 是 `lru_cache` + 首次调用才建库建表。这意味着**第一个 `knowledge_search` 会承担全部初始化开销**（实测冷启动约 1~2s，含 embedding 客户端构建），在 `TOOL_TIMEOUT` 紧张时会把"慢"误判成"错"。改法：

- `main()` 在 `mcp.run()` 之前调用 `get_container()`，让配置错误与建表错误在握手前暴露；
- `knowledge_search` 增加 `_ensure_active_index(kb_id)`：`resolve_active_index(kb_id) is None` 时返回 `status="error", message="knowledge base has no active index"`（**这条很重要**：换一个空的 `RAG_DATA_DIR` 时，现有代码会静默返回 abstain，排查成本极高）；
- 可选新增 `rag_health()`：返回 `data_dir`、全部 kb 名称与 id、各自 active index 与 chunk 数。联调时先调它，能省掉大量猜测。

### B4 客户端抽象成 Protocol，测试不依赖子进程

原规划把测试写成"启动 MCP 子进程再断言"，会让单测变慢且易受环境影响（Windows 下子进程 + 事件循环在 pytest-asyncio 里尤其容易挂）。改为：

```python
class KnowledgeBackend(Protocol):
    async def search(self, question: str, knowledge_base: str | None) -> dict: ...

McpKnowledgeBackend   # 生产：持 ClientSession
StubKnowledgeBackend  # 测试：返回预置 payload（success/abstain/clarify/异常/超时）
NullKnowledgeBackend  # RAG_MCP_ENABLED=false 时：返回 status=disabled
```

7 个 bridge 测试全部用 `StubKnowledgeBackend`；只有 1 个 `@pytest.mark.integration` 的用例真正拉起子进程做冒烟。

### B5 SSE 事件要带知识库元信息

`api.py` 已有 `/stream`，`_execute_action` 已经会发 `tool_call` / `tool_output`。联调与展示需要的是"这次回答引用了哪份证据、能不能点进 trace"，建议在 `_execute_action` 里对 MCP 工具额外发一个事件：

```python
await emit("knowledge_result", {"run_id": ..., "route": ..., "status": ...,
                                "citations": [...], "failure_type": ...})
```

前端/演示脚本即可显示 `run_id` 并调用 `rag_run_detail` 展开完整 trace —— 这正是"可追溯"卖点的可视化落点，原规划把它推到了"最后再做"。

---

## 4. 目标架构（修订版）

```text
text-ReAct-agent
├── agent.py            ToolExecutor: str | Awaitable[str]；按工具分派超时
├── tools.py            LOCAL_TOOL_REGISTRY + MCP_TOOL_SPECS + TOOL_REGISTRY(合并视图)
├── tool_dispatch.py    新增｜execute_tool_async + format_observation
├── knowledge_backend.py 新增｜Protocol / Mcp / Stub / Null
├── mcp_client.py       新增｜RagMcpClient（AsyncExitStack 管 stdio + session）
├── prompts.py          build_system_prompt()，工具清单由 registry 生成
├── config.py           RAG_MCP_* 配置项（dataclass 字段 + 模块常量）
├── api.py              lifespan 启停 + /health 暴露 mcp 状态
└── demo/run_integrated_demo.py  新增（不新建 integrated_demo/ 目录）

        │  MCP stdio（stdout 仅协议，日志走 stderr）
        ▼
evidence-reflective-trace-rag
├── mcp_server.py       rag_query / rag_retrieve / rag_run_detail
│                       + knowledge_search /（可选）rag_health
│                       + _resolve_kb / _shape_result / _ensure_active_index
└── demo/seed_demo.py   保持不变，--data-dir 可重建
```

---

## 5. 接口契约（v1）

### 5.1 `knowledge_search`

```json
// 入参
{ "question": "合同的违约金比例是多少？", "knowledge_base": "contract" }

// 成功
{
  "status": "success",
  "knowledge_base": "contract",
  "knowledge_base_id": "kb_9d3f...",
  "answer": "任一方提前终止合同，应按照合同约定承担违约责任。 [S1]",
  "citations": ["S1", "S2"],
  "route": "success",
  "run_id": "run_c4fe...",
  "failure_type": null,
  "evidence": [
    {"citation_id": "S1", "chunk_id": "chunk_...", "score": 0.62, "preview": "任一方提前终止合同，应…"}
  ],
  "message": null
}

// 证据不足
{
  "status": "abstain",
  "knowledge_base": "contract",
  "knowledge_base_id": "kb_9d3f...",
  "answer": null,
  "citations": [],
  "route": "abstain",
  "run_id": "run_9647...",
  "failure_type": "insufficient_coverage",
  "evidence": [],
  "message": "当前知识库没有足够证据支持该问题"
}

// 配置类问题（不抛异常，便于 Agent 自愈）
{ "status": "error", "message": "knowledge base has no active index: contract", ... }
```

参数非法、kb 不存在 → `raise ValueError(...)`（MCP `isError=true`）。

### 5.2 知识库解析优先级

```
1. knowledge_base 显式传入且是 kb_id（前缀 kb_）→ 直接用
2. knowledge_base 命中别名（RAG_KB_ALIASES 或 demo manifest 的 key）→ manifest.knowledge_bases[key].knowledge_base_id
3. repository.find_knowledge_base_by_name(name)  → 直接用名称
4. 都失败 → 抛 ValueError，message 里列出可用 kb（名称 + 是否 ready）
```

不要在 Agent 侧写死 UUID，也不要在 Server 侧写死 `financial_report` 这个别名 —— 别名走配置，默认值走 `RAG_DEFAULT_KB`。

### 5.3 Agent 侧 observation 模板

```
工具输出(JSON): {"status":"success","route":"success","knowledge_base":"contract",
"answer":"... [S1]","citations":["S1"],"run_id":"run_xxx","evidence":[{"citation_id":"S1","preview":"..."}]}
```

规则（写入提示词，同时由代码保证）：
- `[S#]` 引用必须出现在 Final Answer 中；
- `status != success` 时不得给出结论性判断；
- 本地 `read_file` 内容属于"用户提供材料"，与知识库证据分开陈述。

---

## 6. 分阶段实施（每阶段可独立验收）

### 阶段 0｜RAG 侧补齐契约（0.5 天）✅ 已完成，见附录 A

改 `src/evidence_reflective_rag/mcp_server.py`：

1. 抽 `_run_query(kb_id, question)` 供 `rag_query` 与 `knowledge_search` 共用；
2. 新增 `_resolve_kb(knowledge_base: str | None) -> str`（按 5.2 优先级）；
3. 新增 `_shape_result(run, kb_alias) -> dict`（按 5.1 装配 status/citations/failure_type/evidence）；
4. 新增 `knowledge_search` 与（可选）`rag_health`；
5. `main()` 预热 `get_container()`；保持 stdout 干净（已确认 `src/` 下无 `print`）。

验收：

```powershell
cd E:\PycharmProjects\evidence-reflective-trace-rag
$env:PYTHONPATH="src"; $env:RAG_DATA_DIR="E:\PycharmProjects\evidence-reflective-trace-rag\demo\data"
.venv\Scripts\python.exe demo\seed_demo.py --data-dir demo\data
.venv\Scripts\python.exe -m evidence_reflective_rag.mcp_server    # 应只输出 MCP 协议，无报错
```

追加测试 `tests/test_mcp_server.py`：`test_search_success` / `test_search_abstain` / `test_search_clarify` / `test_search_unknown_kb` / `test_search_no_active_index` / `test_search_citations_aligned_with_trace`。

### 阶段 1｜Agent 侧异步工具链（1 天）

1. `agent.py`：`ToolExecutor` 类型放宽 + `_run_tool()` 分流 + 分工具超时；
2. `tools.py`：拆 registry（C4）；
3. `tool_dispatch.py`：`execute_tool_async` + `format_observation`；
4. `prompts.py`：`build_system_prompt()` 由 registry 生成（B2），并同步 `HistoryManager`；
5. 先只做本地工具的异步化，`TOOL_REGISTRY` 不含 MCP 工具，跑通现有测试。

验收：`pytest -q` 全绿（现有 10 个测试文件不改断言）。

### 阶段 2｜MCP Client + 最小闭环（1 天）

1. `mcp_client.py`：`RagMcpClient`（`AsyncExitStack` + `initialize` + `list_tools` 校验 `knowledge_search` 存在 + `stop()`）；
2. `knowledge_backend.py`：Protocol + Mcp/Stub/Null；
3. `config.py`：新增 7 项配置 + 启动期校验；
4. 注册 `knowledge_search` 到 `MCP_TOOL_SPECS`，接入 `execute_tool_async`；
5. `demo/run_integrated_demo.py`：起 client → 跑场景 1/3 → 打印事件与引用 → 关闭。

最小闭环验收（必须先于此顺序完成，再谈其他）：

```
ReAct → knowledge_search → MCP Client → knowledge_search(server) → RAG Graph
      → answer 含 [S1] → observation(JSON) → Final Answer 保留 [S1]
```

### 阶段 3｜约束强化与可观测（0.5 天）

1. abstain/clarify/verification_failed 的代码级 reminder（B1）；
2. `knowledge_result` 事件（B5）；
3. `api.py` lifespan + `/health` 返回 `{"mcp": "ready|disabled|error"}`；
4. 并发信号量（C6-3）+ observation 截断（C6-4）。

### 阶段 4｜Demo、测试与文档（1 天）

1. 5 个场景脚本化（含 C5 的陷阱场景）；
2. 7 个 bridge 单测（Stub）+ 2 个联合集成测试（其一真起子进程，标 `integration`）；
3. README：架构图、启动命令、契约、已知限制（抽取式答案噪音 / SQLite 串行 / workers=1）；
4. 简历描述按实测措辞收敛。

---

## 7. 配置项（Agent 侧 `.env`）

```env
RAG_MCP_ENABLED=true
RAG_MCP_PYTHON=E:\PycharmProjects\evidence-reflective-trace-rag\.venv\Scripts\python.exe
RAG_MCP_PROJECT=E:\PycharmProjects\evidence-reflective-trace-rag
RAG_MCP_DATA_DIR=E:\PycharmProjects\evidence-reflective-trace-rag\demo\data
RAG_MCP_STARTUP_TIMEOUT=15
RAG_MCP_TOOL_TIMEOUT=60
RAG_MCP_DEFAULT_KB=financial_report
RAG_MCP_MAX_CONCURRENCY=2
RAG_MCP_MAX_OBSERVATION_CHARS=4000
```

要点：
- 全部经 `config.py` 的 `Config` dataclass 读取并校验（当前是 frozen dataclass + 导入期校验，新增字段要同步加到 `from_env` 与模块常量两处）；
- `RAG_MCP_ENABLED=false` 时用 `NullKnowledgeBackend`，提示词里仍然出现 `knowledge_search` 但调用即返回 `status=disabled` —— 比"动态删工具"更利于测试；
- 路径不存在时**启动即失败**，不要懒加载到第一次工具调用才炸。

---

## 8. 测试矩阵

| 层 | 用例 | 手段 |
|---|---|---|
| RAG server | `test_search_success / abstain / clarify / unknown_kb / no_active_index / citations_aligned` | 直接调用底层函数（不启进程） |
| bridge | `test_knowledge_search_is_registered` | 断言 `TOOL_REGISTRY` 含 model 且无 func |
| bridge | `test_async_tool_result_is_serialized_as_json` | Stub + 断言 observation 前缀与可 json.loads |
| bridge | `test_abstain_injects_reminder` | Stub 返回 abstain，断言历史里出现约束消息 |
| bridge | `test_mcp_timeout_becomes_tool_error` | Stub 里 `asyncio.sleep` 超时 |
| bridge | `test_mcp_disabled_backend_returns_disabled` | NullKnowledgeBackend |
| bridge | `test_sync_tool_executor_still_works` | 传 lambda（回归现有 4 个测试） |
| integration | `test_react_calls_rag_and_keeps_citation` | 真子进程，断言 Final Answer 含 `[S1]` |
| integration | `test_react_abstain_does_not_fabricate` | 问"违约金比例"，断言不谈具体数字 |

---

## 9. 已知限制（写进 README，避免面试被问穿）

1. **抽取式答案有噪音**：`ExtractiveAnswerClient` 会把 EvidenceSet 全部 hit 的首句拼起来，answer 里可能存在与问题无关的句子。当前通过 observation 层 `evidence` 裁剪 + 提示词"只使用与问题相关的证据"缓解，根治需要改 RAG 生成器（不在本次范围）。
2. **`success` ≠ 肯定回答**："合同未约定自动续约期限" 也是 `route=success`。演示时要主动说明这是"有证据的否定回答"，而不是门控失效。
3. **SQLite 单锁串行**：并发 `knowledge_search` 实际串行，吞吐约 1~2 QPS 量级。
4. **单进程约束**：`uvicorn --workers=1`，否则多个 MCP 子进程 + 多份 SQLite 连接。
5. **无鉴权**：MCP stdio 走本机管道，不存在远程暴露问题；若后续改 HTTP transport 需补鉴权。

---

## 10. 对比：原规划 vs 本方案

| 维度 | 原规划 | 本方案 |
|---|---|---|
| 进程边界 | 保留 | 保留（不变） |
| 工具面 | 3 个（read/write/knowledge_search） | 3 个（不变），新增可选 `rag_health` |
| 返回契约 | success/abstain 两态，`citations` 凭空出现 | 4 态映射 + citations 从 answer/trace 推导 + `evidence` 映射 |
| 异步改造 | `to_thread(lambda: result)` 后再判 awaitable | `iscoroutinefunction` 分流，向后兼容现有测试 |
| abstain 约束 | 仅提示词 | 提示词 + 代码级 reminder + Stub 测试覆盖 |
| 工具清单 | 提示词手写 | 由 registry 生成 |
| 启动 | 首次调用懒初始化 | 预热 + 无活跃索引检测 + 健康工具 |
| 并发 | 未提 | 信号量 + 单进程约束说明 |
| observation | 未提 | JSON 结构化 + 4000 字符截断 |
| Demo 场景 | 自动续约当 abstain（错） | 违约金比例当 abstain，自动续约作"陷阱"正面演示 |
| 测试 | 全走子进程 | Protocol 注入 + 1 个真集成 |

---

## 11. 简历措辞（与实测一致）

> **基于 MCP 的证据约束 ReAct Agent**（Python / asyncio / FastAPI / MCP / LangGraph / SQLite / SSE）
>
> - 以 MCP stdio 将 Evidence Reflective RAG 封装为独立知识库服务，暴露带引用问答、证据检索与运行追踪三类工具；ReAct Agent 作为 MCP Client 复用单一会话（AsyncExitStack 管理子进程生命周期），实现进程解耦与工具协议标准化。
> - 改造 Agent 工具执行链，使同步文件工具与异步 MCP 工具统一接入：协程分流 + awaitable 兜底、分工具超时（本地 30s / MCP 60s）、异常与超时降级为可读 observation。
> - 将 RAG 四态路由（success / clarify / abstain / verification_failed）映射为 Agent 语义，**在运行时把"证据不足禁止补答"的约束写进对话历史**（非提示词约定），并用单测覆盖五态约束生成。
> - 结构化 observation 采用逐级收缩（裁证据 → 清证据 → 标量骨架）保证在 2000 字符预算内始终是合法 JSON；新增 `knowledge_result` 事件携带 run_id / route / citations，支持从 Agent 输出回溯至检索、门控、校验与失败归因。
> - 建财务报告与合同条款双知识库 Demo：ReAct 自主选库（7 条共享用例，21 项联调断言通过），覆盖带引用作答、证据不足拒答、否定事实陷阱与本地文件联合推理。
> - 工程化：两端测试数据统一到 `demo/cases.json`，RAG 132 项 + Agent 102 项测试通过，含真实子进程集成测试与 API lifespan 启停验证。

---

# 附录 A｜阶段 0 实施记录（已完成）

## A.1 改了哪些文件

| 文件 | 改动 |
| --- | --- |
| `src/evidence_reflective_rag/mcp_server.py` | 重写：新增 `knowledge_search` / `rag_health`；抽出 `_resolve_knowledge_base` / `_shape_result` / `_empty_result` / `_parse_aliases` / `_trace_evidence` / `_scores_by_chunk` / `_previews_by_citation`；`lru_cache` 改为可注入的 `get_container()` + `set_container()`；`main()` 增加配置校验与容器预热 |
| `src/evidence_reflective_rag/repositories.py` | 新增 `list_knowledge_bases()`（错误提示与 `rag_health` 需要） |
| `src/evidence_reflective_rag/config.py` | 新增 `kb_aliases`、`default_kb`（对应 `RAG_KB_ALIASES` / `RAG_DEFAULT_KB`） |
| `tests/test_mcp_server.py` | 新增 14 个契约测试（直接调用函数，不启子进程） |
| `README.md` | 新增「作为 MCP Server 使用」章节：启动命令、工具表、返回契约、解析优先级 |

## A.2 关键实现点

1. **状态映射覆盖全部 4 个 route**，非 `success` 一律把 `answer` / `citations` / `evidence` 置空，只留 `failure_type` 与 `message`：

```python
_STATUS_BY_ROUTE = {
    Route.SUCCESS: "success",
    Route.CLARIFY: "clarify",
    Route.ABSTAIN: "abstain",
    Route.VERIFICATION_FAILED: "verification_failed",
}
```

2. **引用以 trace 为准**：先按 `\[S(\d+)\]` 从 answer 抽取，再与 `answer_verification.verified_citation_ids` 取交集；`S{n}` → `chunk_id` 通过 `answer_generation.evidence_chunk_ids[n-1]` 映射；`score` 取自 `retrieval` trace 的 `hits`；`preview` 复用答案行文本，**不额外查库**。
3. **错误分级**：参数非法 / 知识库不存在 → 抛 `ValueError`（MCP `isError=true`）；知识库存在但无活跃索引 → 返回 `status=error`（可自愈，不会静默退化成 abstain）。
4. **别名不写死**：`RAG_KB_ALIASES="financial_report=财务报告,contract=合同条款"`。Server 不认识 Demo，只认配置；解析顺序为 `kb_` 前缀 ID → 精确名称 → 别名 → `RAG_DEFAULT_KB`。

## A.3 验证结果

```
tests/test_mcp_server.py   14 passed
全量回归                    124 passed in 10.52s

# 真实 stdio 端到端（RAG_DATA_DIR=demo/data，别名经环境变量注入）
TOOLS: ['rag_query', 'rag_retrieve', 'rag_run_detail', 'knowledge_search', 'rag_health']

Q(financial_report) 2025年第三季度营业收入和净利润是多少？
  status=success  answer='2025年第三季度营业收入为100万元，净利润为20万元。 [S1]'
  citations=['S1']  evidence=[('S1', 0.529)]

Q(contract) 提前终止时需要承担违约责任吗？
  status=success  citations=['S1','S2','S3']  evidence=[('S1',0.376),('S2',0.120),('S3',0.034)]

Q(contract) 合同中是否规定了自动续约期限？
  status=success  answer='合同未约定自动续约期限。 [S1]...'   ← 陷阱场景，符合预期

Q(contract) 合同的违约金比例是多少？
  status=abstain  failure=insufficient_coverage  answer=None
```

> S2=0.120、S3=0.034 这类低分引用仍会出现在答案里（抽取式生成器的固有行为）。契约已把 `score` 暴露出来，阶段 1 的 observation 格式化器据此裁剪预览；**答案文本本身不改写**，保持与 Run 记录一致，避免「契约富化」变成「证据改写」。

## A.4 已知遗留

- `kb_aliases` 目前只在 Server 侧生效；Agent 侧需要把同样两个值写进自己的 `.env`（启动子进程时透传）。
- `rag_health` 未纳入 Agent 工具面，只在联调脚本中调用。

---

# 附录 B｜阶段 1 细化到函数的执行清单

按顺序执行，每步都能单独跑测试。

## B.1 `config.py`（先加配置）

```python
# Config dataclass 新增字段
rag_mcp_enabled: bool
rag_mcp_python: Path
rag_mcp_project: Path
rag_mcp_data_dir: Path
rag_mcp_startup_timeout: float
rag_mcp_tool_timeout: float
rag_mcp_default_kb: str
rag_mcp_kb_aliases: str
rag_mcp_max_concurrency: int
rag_mcp_max_observation_chars: int
```

导入期校验：`rag_mcp_enabled` 为真但 `python` / `project` 路径不存在时立刻 `raise ValueError`，不要懒加载到第一次工具调用才炸。

## B.2 `tools.py`（拆注册表，C4）

```python
LOCAL_TOOL_REGISTRY = {"read_file": {...含 func...}, "write_file": {...含 func...}}
MCP_TOOL_SPECS = {}          # 阶段 2 再填 knowledge_search
TOOL_REGISTRY = {**LOCAL_TOOL_REGISTRY, **MCP_TOOL_SPECS}   # 仅供校验与提示词

def execute_tool(tool_name, validated_input) -> str:
    tool = LOCAL_TOOL_REGISTRY.get(tool_name)
    if tool is None:
        if tool_name in MCP_TOOL_SPECS:
            raise ValueError(f"{tool_name} 是 MCP 工具，必须通过 execute_tool_async 调用")
        raise ValueError(f"未知工具：{tool_name}")
    ...
```

## B.3 `tool_dispatch.py`（新文件）

```python
async def execute_tool_async(tool_name, validated_input) -> str:
    if tool_name in LOCAL_TOOL_REGISTRY:
        return await asyncio.to_thread(execute_tool, tool_name, validated_input)
    raise ValueError(f"未知工具：{tool_name}")

def format_observation(payload: dict, limit: int = MAX_OBSERVATION_CHARS) -> str:
    ...
```

## B.4 `agent.py`（C3 的核心改动）

```python
ToolExecutor = Callable[[str, Any], str | Awaitable[str]]

async def _run_tool(tool_executor, tool_name, validated_input, timeout):
    if inspect.iscoroutinefunction(tool_executor):
        return await asyncio.wait_for(tool_executor(tool_name, validated_input), timeout)
    return await asyncio.wait_for(
        asyncio.to_thread(tool_executor, tool_name, validated_input), timeout)
```

同时把 `_execute_action` 的超时改成按工具取值：MCP 工具用 `RAG_MCP_TOOL_TIMEOUT`，本地工具用 `TOOL_TIMEOUT`。

## B.5 `prompts.py`（B2）

`SYSTEM_PROMPT_TEMPLATE` + `build_system_prompt()`，工具清单由 `TOOL_REGISTRY` 生成；`agent.py` 的 `_build_full_messages` 与 `HistoryManager.load` 都要改为调用它，否则恢复会话时模型不知道有新工具。

## B.6 阶段 1 验收

```powershell
pytest -q                 # 现有 10 个测试文件断言不变，全绿
python -m demo.run_demo   # 本地文件读写行为不变
```

## B.7 阶段 1 完成后即可打通最小闭环

接上 `MCP_TOOL_SPECS["knowledge_search"]` + `McpKnowledgeBackend` 后：

```
ReAct → knowledge_search → stdio → _shape_result → observation(JSON) → Final Answer 保留 [S1]
```

---

# 附录 C｜阶段 1 实施记录（已完成）

## C.1 备份

改动前把将触及的文件整份复制到 `.backup/2026-10-28-stage1/`（`agent.py`、`tools.py`、`prompts.py`、`config.py`、`api.py`、`requirements.txt`、`tests/*.py`），并把 `.backup/` 加进 `.gitignore`。回滚方式：把该目录内容复制回项目根即可。

## C.2 改动清单

| 文件 | 改动 |
| --- | --- |
| `config.py` | 新增 `max_observation_chars`（`MAX_OBSERVATION_CHARS`，默认 4000） |
| `tools.py` | 拆 `LOCAL_TOOL_REGISTRY` / `MCP_TOOL_SPECS` / `TOOL_REGISTRY`（合并视图）；`execute_tool` 只服务本地工具，遇到 MCP 工具时报「必须通过 execute_tool_async 调用」；新增 `available_tools()` |
| `tool_dispatch.py` | **新增**：`execute_tool_async`（本地工具走线程池）+ `format_observation`（JSON 序列化 + 逐级收缩） |
| `prompts.py` | 改为 `build_system_prompt()` 按注册表生成工具清单；知识库规则仅在 `MCP_TOOL_SPECS` 非空时出现；保留 `SYSTEM_PROMPT` 别名 |
| `agent.py` | `ToolExecutor` 放宽为 `str \| Awaitable[str]`；新增 `_run_tool()`（协程分流 + awaitable 兜底）与 `_tool_timeout()`；`_execute_action` 改用 `format_observation`；默认执行器改为 `execute_tool_async`；`SYSTEM_PROMPT` 的 4 处引用统一改为 `build_system_prompt()` |
| `tests/test_async_tools.py` | **新增** 16 个用例 |

## C.3 关键决策

1. **协程分流 + awaitable 兜底**：`inspect.iscoroutinefunction` 判断后直接 await；否则进线程池，若线程返回的仍是 awaitable 再 await 一次。后者专治 `partial` 包装、带 async `__call__` 的对象——它们不会被 `iscoroutinefunction` 识别，漏掉就会把 `<coroutine object ...>` 写进 observation。
2. **注册表拆三层**：`execute_tool` 强依赖 `func`，若把只有 model 的 MCP 工具塞进同一张表，注册即崩。合并视图 `TOOL_REGISTRY` 只用于「校验入参 + 生成提示词」，执行走分派层。
3. **observation 必须始终是合法 JSON**：最初版本「收缩后仍超限就直接截断」，实测会得到 `Unterminated string` —— 模型要么解析失败要么猜。改为三级收缩（裁 evidence → 清 evidence → 只留标量字段 + 按剩余预算裁 answer），只有连骨架都放不下才退化为纯文本。
4. **知识库规则按需出现**：未接 RAG 时提示词里没有 `knowledge_search`，避免模型调用不存在的能力；阶段 2 注册 `MCP_TOOL_SPECS` 后规则自动出现。
5. **超时留了单一入口 `_tool_timeout()`**：阶段 2 让 MCP 工具走 `RAG_MCP_TOOL_TIMEOUT` 只改这一个函数。

## C.4 踩到的两个坑

- **双花括号泄漏**：模板用 `.replace()` 而非 `.format()`，写 `{{"键": "值"}}` 会原样出现在提示词里。已修正并加回归测试 `test_system_prompt_keeps_single_braces_in_format_example`。
- **JSON 硬截断**：见 C.3-3，已由三级收缩替代。

## C.5 验证

```
pytest -q                     82 passed（原 66 + 新增 16）
ruff check agent.py tools.py prompts.py tool_dispatch.py config.py tests/...   All checks passed
python -m demo.run_demo       行为不变：read_file → read_file → write_file → Final Answer
                               observation 仍是「工具输出: ...」前缀，事件序列一致
```

> 仓库里 `ruff check .` 仍有 6 条历史告警，全部在 `demo/`（`PYI034`/`UP035`/`UP041`/`RUF100`），与本次改动无关，未顺手改以免扩大范围。

## C.6 阶段 2 的接入点（已就绪）

```python
# tools.py
MCP_TOOL_SPECS["knowledge_search"] = {
    "model": KnowledgeSearchInput,
    "description": "查询知识库并返回带引用的答案",
    "input_schema": KnowledgeSearchInput.model_json_schema(),
}
```
注册后自动生效：`TOOL_REGISTRY` 出现该工具 → 提示词生成知识库规则 → `execute_tool_async` 路由到 MCP 客户端 → `format_observation` 处理 JSON 返回。


