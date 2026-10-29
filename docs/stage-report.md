# MCP 增强型证据约束 ReAct Agent · 阶段汇报

> 涉及仓库
> - Agent 侧：`text-ReAct-agent`（GitHub: `bmmbll1881-creator/text-ReAct-agent`，master）
> - RAG 侧：`evidence-reflective-trace-rag`（GitHub: `bmmbll1881-creator/evidence-reflective-trace-rag`，master）
>
> 方案文档：`docs/mcp-rag-integration-plan.md`

---

## 一、项目目标

把已有两个项目合并为一个可演示、可讲解的完整系统：

```
ReAct Agent（任务规划 / 工具编排）
├── read_file / write_file          ← 本地工作区
└── knowledge_search                ← MCP stdio
        │
        ▼
Evidence Reflective RAG MCP Server
        │
        ▼
RAG Graph：retrieval → evidence gate → reflection → generation
           → verification → failure attribution
```

三条不可妥协的设计约束：

1. **进程边界**：RAG 独立进程，通过 MCP 协议暴露，不进程内耦合。
2. **证据门控不可绕过**：RAG 返回 `abstain` / `clarify` / `verification_failed` 时，Agent 不得凭常识补答案。
3. **引用可追溯**：答案保留 `[S1]` 引用，并保留 `run_id` 可回查完整 Trace 与失败归因。

---

## 二、阶段进度

| 阶段 | 内容 | 状态 | 验收证据 |
|---|---|---|---|
| 0 | RAG 侧 Agent 契约（`knowledge_search` / `rag_health`） | ✅ 完成 | RAG 124 passed；真实 stdio 四场景实测 |
| 1 | Agent 侧异步工具链（同步/异步工具混跑） | ✅ 完成 | Agent 82 passed；`demo.run_demo` 行为不变 |
| 2 | MCP Client + 最小闭环 | ✅ 完成 | 联合 Demo 13/13 通过（真实子进程 + 真实 LLM） |
| 3 | 约束强化与可观测（abstain 硬约束、SSE 事件、lifespan） | ✅ 完成 | 102 passed；真实 MCP 下 health=ready、可回查 run_id |
| 4 | 联合 Demo、集成测试、文档与简历 | ✅ 完成 | 21/21 联调断言；两端测试数据统一到 `demo/cases.json` |

---

## 三、已完成内容明细

### 3.1 阶段 0：RAG 侧 Agent 契约

改动文件：

| 文件 | 内容 |
|---|---|
| `src/evidence_reflective_rag/mcp_server.py` | 新增 `knowledge_search`（别名/名称/ID 解析 + 四态 status + citations/evidence 装配）、`rag_health`；容器改为可注入并支持启动预热 |
| `src/evidence_reflective_rag/repositories.py` | 新增 `list_knowledge_bases()` |
| `src/evidence_reflective_rag/config.py` | 新增 `kb_aliases` / `default_kb`（`RAG_KB_ALIASES` / `RAG_DEFAULT_KB`） |
| `tests/test_mcp_server.py` | 14 个契约测试（直接调用函数，不启子进程） |
| `README.md` | 新增「作为 MCP Server 使用」章节 |

返回契约（`status` 只有 `success` 携带 `answer` / `citations`）：

```json
{
  "status": "success | clarify | abstain | verification_failed | error",
  "knowledge_base": "contract",
  "knowledge_base_id": "kb_...",
  "answer": "任一方提前终止合同，应按照合同约定承担违约责任。 [S1]",
  "citations": ["S1"],
  "route": "success",
  "run_id": "run_...",
  "failure_type": null,
  "evidence": [{"citation_id": "S1", "chunk_id": "chunk_...", "score": 0.376, "preview": "..."}],
  "message": null
}
```

真实 stdio 实测（`RAG_DATA_DIR=demo/data`，别名经环境变量注入）：

| 问题 | status | 关键输出 |
|---|---|---|
| 2025Q3 营业收入和净利润（financial_report） | success | `...100万元，净利润为20万元。 [S1]` |
| 提前终止是否承担违约责任（contract） | success | `[S1] [S2] [S3]`，score 0.376 / 0.120 / 0.034 |
| 合同是否规定自动续约期限（contract） | success | `合同未约定自动续约期限。 [S1]` ← **陷阱场景，success ≠ 肯定回答** |
| 合同违约金比例是多少（contract） | abstain | `failure_type=insufficient_coverage`，answer=null |

### 3.2 阶段 1：Agent 侧异步工具链

| 文件 | 内容 |
|---|---|
| `config.py` | 新增 `MAX_OBSERVATION_CHARS` |
| `tools.py` | 拆 `LOCAL_TOOL_REGISTRY` / `MCP_TOOL_SPECS` / `TOOL_REGISTRY`；`execute_tool` 只服务本地工具 |
| `tool_dispatch.py`（新） | `execute_tool_async`（本地工具走线程池）+ `format_observation`（JSON 序列化 + 三级收缩） |
| `prompts.py` | `build_system_prompt()` 由注册表生成；知识库规则按需出现 |
| `agent.py` | `ToolExecutor = str \| Awaitable[str]`；`_run_tool()` 协程分流 + awaitable 兜底；`_tool_timeout()`；默认执行器改为异步 |
| `tests/test_async_tools.py`（新） | 16 个用例 |

关键修复（都是跑出来才发现的）：

1. **协程静默空转**：原 `to_thread(tool_executor)` 遇到 async 执行器会返回 coroutine，被 f-string 成 `<coroutine object ...>`，不报错也不退化。改为 `inspect.iscoroutinefunction` 分流，并在线程返回值上补 awaitable 兜底（覆盖 `partial` 与 async `__call__`）。
2. **提示词双花括号泄漏**：模板用 `.replace()` 占位，`{{"键": "值"}}` 会原样输出给模型。已修正并加回归测试。
3. **JSON 硬截断产生非法 JSON**：截断落在字符串中间会得到 `Unterminated string`。改为三级收缩（裁 evidence → 清 evidence → 标量骨架），任何一级都保证合法 JSON。

---

## 四、待完成事项

### P0 · 打通最小闭环

| # | 事项 | 状态 |
|---|---|---|
| 1 | 安装 `mcp` 依赖，补 `RAG_MCP_*` 配置（观测上限 2000、MCP 超时 60s） | ✅ |
| 2 | `mcp_client.py`：`RagMcpClient`（`AsyncExitStack` 管 stdio + session） | ✅ |
| 3 | `knowledge_backend.py`：Protocol + Mcp / Stub / Null | ✅ |
| 4 | 注册 `knowledge_search` 到 `MCP_TOOL_SPECS`，分派层路由 + 分工具超时 | ✅ |
| 5 | `demo/run_integrated_demo.py` 联合 Demo 与联调 | ✅ 13/13 |

### P1 · 质量与约束

| # | 事项 | 状态 |
|---|---|---|
| 6 | `abstain` / `clarify` / `verification_failed` 的**代码级** reminder | ✅ |
| 7 | `knowledge_result` SSE 事件（run_id / route / citations） | ✅ |
| 8 | `api.py` lifespan 启停 + `/health` 返回 mcp 状态 | ✅ |
| 9 | 并发信号量 + `workers=1` 约束写入文档 | ✅ |
| 10 | bridge 单测（Stub，14 例）+ 集成测试（1 例真起子进程） | ✅ |

### P2 · 收尾

| # | 事项 | 状态 |
|---|---|---|
| 11 | 5 个演示场景脚本化（含"自动续约"陷阱场景） | ✅ |
| 12 | README：架构图、事件/约束流程图、已知限制 5 条 | ✅ |
| 13 | `.env.example` 补 `RAG_MCP_*` 与 `RAG_KB_ALIASES` | ✅（A2 完成） |
| 14 | 简历措辞按实测数据核对 | ✅ |
| 15 | 两端测试数据统一到 `demo/cases.json` | ✅ |

### 已知限制（待写入 README）

- 抽取式答案会把所有命中 chunk 的首句拼接，answer 中存在低分噪音（已通过 `score` 暴露供上层裁剪）。
- `route=success` 不代表肯定结论（"合同未约定…" 也是 success）。
- RAG 侧 SQLite 单锁串行，`knowledge_search` 有效并发约 1~2。
- `uvicorn` 必须 `--workers=1`，否则会拉起多个 MCP 子进程。
- MCP stdio 走本机管道，无鉴权；若改 HTTP transport 需补鉴权。

---

## 五、变更记录

> 每完成一部分在此追加一条，最新在最上方。

### 2026-10-29 · A1 提交并推送既有改动

- **RAG 仓库**
  - `0cfdf38 chore: 同步本地在研改动（检索/图/评估/RAGAS）` — 提交此前未入库的 15 个文件（`evaluation/*`、`graph.py`、`models.py`、`ingestion.py`、`db.py`、`evidence.py`、`pyproject.toml`、`uv.lock` 等）。
  - `64a6709 feat(mcp): 新增面向 Agent 的 knowledge_search 契约工具` — 阶段 0 成果，含 `mcp_server.py`（此前从未入库）、`README.md`、`demo/`、`tests/test_mcp_server.py`。
  - 已推送 `3b70be2..64a6709`。
- **Agent 仓库**
  - `ab5f0ec feat(tools): 支持同步与异步工具混跑` — 阶段 1 成果，含 `tool_dispatch.py`、`tests/test_async_tools.py`、方案文档。
  - 已推送 `6360a88..ab5f0ec`。
- 两个仓库工作区均已干净。

### 2026-10-29 · A2 依赖与环境补全

- `requirements.txt`：新增 `mcp>=1.0,<2.0`；因 mcp 传递依赖会把 starlette 顶到 1.x（与 fastapi 0.115 的 `<0.42` 冲突），显式固定 `starlette==0.41.3`、`sse-starlette<3`；`pydantic` 由 2.10.6 升至 2.14.1（mcp 要求 `>=2.11`）。
- 安装结果：`mcp 1.30.0` / `fastapi 0.115.6` / `starlette 0.41.3` / `sse-starlette 2.4.1` / `pydantic 2.14.1`，`mcp.server.fastmcp` 与 `mcp.client.stdio` 导入正常，82 passed。
- `config.py`：新增 9 项 `RAG_MCP_*` 配置（含 `_validate_rag_mcp`：启用但路径缺失/不存在时启动即失败）；`MAX_OBSERVATION_CHARS` 默认 4000 → **2000**；`RAG_MCP_TOOL_TIMEOUT` 默认 **60s**。
- `agent.py`：`_tool_timeout()` 改为按工具取值——本地工具 `TOOL_TIMEOUT`，MCP 工具 `RAG_MCP_TOOL_TIMEOUT`。
- `.env.example` 补齐 `MAX_OBSERVATION_CHARS` 与 9 项 `RAG_MCP_*`。
- 说明：本地 `.env` 未改动（用户拒绝了内容共享），联调时由 Demo 脚本与命令行环境变量注入。

### 2026-10-29 · A3 阶段 2（P0）完成：MCP Client 打通最小闭环

**新增文件**

| 文件 | 内容 |
|---|---|
| `mcp_client.py` | `RagMcpClient`：`AsyncExitStack` 管 stdio + ClientSession，`start()` 完成握手与必需工具校验，`call_tool()` 归一化 `structuredContent` / JSON 文本 / 纯文本三种返回，`close()` 逆序释放 |
| `knowledge_backend.py` | `KnowledgeBackend` Protocol + `McpKnowledgeBackend`（信号量限流）/ `StubKnowledgeBackend`（测试替身）/ `NullKnowledgeBackend`（未启用降级）；模块级 `set_backend` / `get_backend` |
| `demo/run_integrated_demo.py` | 两段式联调：A 链路自检（不依赖 LLM）+ B 端到端 4 场景 |

**改动文件**

| 文件 | 内容 |
|---|---|
| `tools.py` | 新增 `KnowledgeSearchInput`（`question` 必填、`knowledge_base` 可选，不枚举别名）；注册到 `MCP_TOOL_SPECS` |
| `tool_dispatch.py` | 新增 `_MCP_HANDLERS` 路由表，`execute_tool_async` 返回 `str \| dict` |
| `prompts.py` | 知识库规则增加「可用知识库」清单与「按主题选库」要求，别名由 `RAG_MCP_KB_ALIASES` 生成 |
| `tests/test_async_tools.py` | 新增 6 例（工具注册、backend 路由、disabled、observation JSON、别名提示、无 MCP 工具时规则消失） |

**联调过程中发现并修复的真实问题**

1. **依赖链冲突**：`mcp` 默认拉 `sse-starlette 3.x` → `starlette 1.10` → 与 `fastapi 0.115.6`（要求 `<0.42`）冲突。固定 `starlette==0.41.3` + `sse-starlette<3` 后正常。
2. **Agent 一律使用默认知识库**：首次联调 4 个任务全部查 `financial_report`，合同问题靠"碰巧拒答"通过，掩盖了选库错误。原因是提示词只说了 `knowledge_base` 存在，没说有哪些库。补上别名清单与选库规则后，合同任务稳定切到 `contract` 并拿到 `success` + `[S1]`。

**联调结果（真实 MCP 子进程 + 真实 DeepSeek）**

```
MCP 已连接，工具：['rag_query','rag_retrieve','rag_run_detail','knowledge_search','rag_health']

[PASS] rag_health 报告知识库就绪          ready=['财务报告', '合同条款']
[PASS] 财务问答 status=success 且含 [S1]  citations=['S1'] run_id=run_24a5...
[PASS] 财务问答 evidence 带 chunk_id/score score=0.529
[PASS] 无证据问题 status=abstain           failure_type=insufficient_coverage
[PASS] run_id 可回查完整 Trace            stages=[query_received,retrieval,evidence_gate,answer_generation,answer_verification]

任务1 财务 → success，答案保留 [S1]
任务2 合同 → 自动切库 contract，success，保留 [S1][S2]
任务3 违约金 → abstain，明确说明证据不足，未编造比例
任务4 本地文件 + 知识库 → read_file 1 次 + knowledge_search 1 次，区分「本地材料」与「知识库证据」

合计 13/13 通过
```

**回归**

- Agent：88 passed（原 82 + 新增 6），`ruff check` 仅剩 `demo/run_demo.py`、`demo/memory_budget.py` 的 6 条历史告警。
- RAG：124 passed。
- `python -m demo.run_demo` 本地工具链路行为不变。

### 2026-10-29 · A4 P1 完成：约束强化与可观测

**改动/新增**

| 文件 | 内容 |
|---|---|
| `tool_dispatch.py` | 新增 `_STATUS_CONSTRAINTS` 与 `constraint_for(payload)`：abstain / clarify / verification_failed / disabled / error 五种状态的运行时约束文本，带 `run_id` |
| `agent.py` | `_execute_action` 在非 success 时把 `[系统约束]` 追加到 observation（代码注入，不依赖提示词）；新增 `knowledge_result` 事件 |
| `api.py` | 新增 `lifespan`：启动时按配置拉起 MCP 子进程并注入 backend，退出时逆序关闭；`/health` 返回 `{"status":"ok","mcp":"ready|disabled|error: ..."}`；MCP 启动失败降级而非整站不可用 |
| `tests/test_mcp_bridge.py` | 新增 15 例：约束注入 3 例、状态覆盖参数化 4 例、事件 3 例、错误/超时 2 例、API 生命周期 3 例、真集成 1 例（默认 skip） |
| `demo/run_integrated_demo.py` | 事件采集改读 `knowledge_result`，不再解析带约束尾巴的 observation 文本 |
| `README.md` | 新增「MCP 知识库集成」与「并发与部署约束」两节 |

**关键设计**

- **约束是历史里的真实文本，不是提示词**：`abstain` 时下一轮 observation 末尾固定追加
  `[系统约束] 知识库返回 abstain（证据不足）。不得基于常识或推测补充答案…（run_id=...）`。
  联调里模型在拒答回答中主动引用了该 `run_id`，说明约束确实进了上下文。
- **超时分级可验证**：单测同时 monkeypatch `TOOL_TIMEOUT=30` 与 `RAG_MCP_TOOL_TIMEOUT=0.01`，
  证明 MCP 工具走的是后者。
- **降级不掩盖**：MCP 起不来时服务仍返回 200，但 `/health` 明确 `error: ...`，不假装 ready。

**验证**

```
pytest -q                                      102 passed, 1 skipped（集成用例默认跳过）
RAG_INTEGRATION=1 pytest -k real_mcp            1 passed（真实子进程 3.66s 内完成握手+查询）
ruff check .                                    仅剩 demo/ 下 6 条历史告警
TestClient(api.app) 真实 MCP 启动             health={'status':'ok','mcp':'ready'}
                                              backend=McpKnowledgeBackend
                                              退出后 backend=NullKnowledgeBackend（无 cancel scope 报错）
python -m demo.run_integrated_demo             13/13 通过
```

**联调回归中的一个坑**：约束追加到 observation 后，Demo 原先「解析 observation 前缀 JSON」的采集逻辑失效，
两次 FAIL 其实是**检查器坏了**而不是行为坏了。改为消费 `knowledge_result` 事件后恢复 13/13——
这也顺带证明该事件对可观测性的必要性。

### 2026-10-29 · A5 P2 完成：测试数据统一、文档收尾

**测试数据统一（核心改动）**

新增 `evidence-reflective-trace-rag/demo/cases.json` + `demo/cases.py`，作为**两端唯一数据源**：

```json
{
  "documents": [{"alias": "financial_report", "name": "财务报告", "filename": "financial_report.md"}, ...],
  "cases": [
    {"id": "finance_revenue", "knowledge_base": "financial_report", "question": "...",
     "expected_route": "success", "expected_answer": "... [S1]", "expected_citations": ["S1"],
     "agent_task": "..."},
    {"id": "contract_penalty_ratio", "expected_route": "abstain", "expected_failure_type": "insufficient_coverage", ...}
  ]
}
```

消费方收敛为 4 处，全部读同一份：

| 消费方 | 用途 |
|---|---|
| `demo/seed_demo.py` | 文档定义（`DOCUMENTS` 由 cases.json 生成） |
| `demo/run_demo.py` | 演示问题列表（原硬编码 6 条 → 改为读取） |
| `tests/test_demo.py` | 预期路由/答案断言 + **答案事实回溯校验**（答案必须能在源文档中原样找到） |
| Agent `demo/run_integrated_demo.py` + `tests/test_mcp_bridge.py` | 联调用例与真集成断言 |

用例从 6 条扩到 7 条，新增 `contract_penalty_ratio`（拒答正例）；新增 `contract_renewal_trap` 的 `note` 字段，明确"route=success 但答案是否定事实"的陷阱语义。

**其它改动**

- `demo/run_integrated_demo.py`：改为数据驱动（`--cases` 可指定路径）；A 段遍历全部 7 条用例，B 段跑 5 个场景（财务、合同成功、拒答、陷阱、本地文件联合），断言由用例预期生成。
- `README.md`：补充事件/约束流程图与「已知限制」5 条。
- RAG `README.md`：说明 `demo/cases.json` 是数据单一来源。
- 方案文档第 11 节简历措辞按实测数据重写。

**验证（本轮全量）**

```
RAG：   pytest -q                     132 passed
        demo/seed_demo + run_demo      7 条用例全部按预期路由
Agent： pytest -q                     102 passed, 1 skipped
        pytest -q + RAG_INTEGRATION=1  1 passed（真子进程 + 共享用例断言）
        ruff check .                   仅剩 demo/ 下 6 条历史告警
        demo.run_demo                  本地工具链路不变
        demo.run_integrated_demo       21/21 通过（9 项链路自检 + 12 项端到端）
```

**统一数据后的一个直接收益**：A 段自检从"抽查 4 条"变成"覆盖全部 7 条用例"，
其中 `contract_termination` 这类多 chunk 命中会返回 3 行答案——断言改为"预期事实片段包含在答案中 + 引用 ID 全部出现在答案里"，
既能容忍抽取式噪音，又能抓住"事实错位"和"引用丢失"两类真问题。

### 2026-10-29 · A6 存量告警清理与整理

**静态检查归零**

| 仓库 | 清理前 | 清理后 | 处理方式 |
|---|---|---|---|
| RAG | 46 | **0** | `ruff --fix` 自动修 53 项（导入排序、`Dict→dict`、`Optional→| None`、`datetime.UTC` 等），手工修 5 项 |
| Agent | 6 | **0** | `demo/memory_budget.py` 的 `Self` 注解、`demo/run_demo.py` 的 `collections.abc.Iterator` / `TimeoutError` / 去掉已 ignore 的多余 noqa |

手工修 5 项的判断依据（都不是"加 noqa 了事"）：

- **`B008`（`UploadFile = File(...)`）**：FastAPI 的声明式默认值，属误报。在 `pyproject.toml` 里配
  `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls = ["fastapi.File", ...]`，从规则层面消除，
  而不是逐行 noqa。
- **`BLE001`（3 处 blind except）**：反射重写失败、LLM 调用失败、远端 embedding 失败——异常类型不可穷举，
  且**必须**降级而不是冒泡。保留捕获范围，逐处加 `# noqa: BLE001` 并写明"必须降级"的理由。
- **`RUF012`（可变类属性）**：`_STRATEGY_MAPPING` 改注 `ClassVar[dict[...]]`，语义更准确。

**其他整理**

- 两个 `pyproject.toml` 补 ruff 配置：RAG 新增 `target-version = "py311"`、`line-length = 100` 与
  `exclude`（`.pytest-db-final`、`demo/data` 等本地产物）；Agent 补 `exclude`（`.backup`、`.test-tmp`）。
  此前每次跑 ruff / `git status` 都会因 ACL 受限目录报 `拒绝访问`，现在消失。
- `.gitignore` 增补测试产物目录，消除 `git status` 的权限告警噪声。
- **README 时效性**：
  - Agent：项目结构补 `tool_dispatch.py`、`knowledge_backend.py`、`mcp_client.py`、`prompts.py`、`docs/`；
    Python 版本 3.10+ → 3.11+（`asyncio.timeout`）；补 `uv pip install` 与真集成测试命令。
  - RAG：安装段补 `uv` 方式；测试段写明 132 项、`ruff check .` 零告警、以及 `cases.json` 的事实回溯校验。

**验证**

```
RAG：  ruff check .           All checks passed
       pytest -q              132 passed
       compileall src demo tests / demo.run_demo    ok / 7 条路由符合预期
Agent：ruff check .           All checks passed
       pytest -q              102 passed, 1 skipped
       demo.run_demo / demo.run_integrated_demo    行为不变 / 21/21
```
