# Text ReAct Agent

基于 ReAct 的安全文件操作 Agent。读写工具仅允许访问 `WORKSPACE` 工作区内、且扩展名符合白名单的文件。

## 项目结构

```text
.
├── agent.py                 # Agent 主循环、LLM 调用、协议解析与事件
├── tools.py                 # 工具注册表（本地工具 + MCP 工具契约）
├── tool_dispatch.py         # 同步/异步工具分派、observation 序列化与状态约束
├── knowledge_backend.py     # 知识库后端（Mcp / Stub / Null）
├── mcp_client.py            # RAG MCP stdio 客户端（会话复用）
├── prompts.py               # 由注册表生成的系统提示词
├── parser.py                # ReAct 文本协议解析
├── budget.py                # 本地与 Redis 会话预算、历史和锁
├── api.py                   # FastAPI、SSE 接口与 MCP 生命周期
├── config.py                # 环境变量配置
├── context.py               # 会话上下文压缩
├── demo/                    # 离线演示、MCP 联调 Demo、内存预算
├── docs/                    # 方案与阶段汇报
├── evaluation/              # 评测器和评测任务
└── tests/                   # 单元与集成测试
```

## 安装与配置

需要 Python 3.11+（`asyncio.timeout` 等 API）。仓库使用 uv 管理依赖（`pyproject.toml` 为唯一来源）：

```powershell
# 开发环境（含运行时 + 测试/静态检查依赖，推荐）
uv sync --group dev
# 传统 pip 方式（requirements.txt 为冻结快照，与 pyproject 保持一致）
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，配置 `CHAT_URL`、`DEEPSEEK_API_KEY` 和工作区等参数。启用 RAG 知识库后端需在 `.env` 中保留 `.env.example` 里的 `RAG_MCP_*` 配置块并将 `RAG_MCP_ENABLED` 设为 `true`；留空或置 `false` 时 Agent 仅加载本地文件工具（便于单独调试）。在线 API/Agent 模式需要可访问的 Redis 服务；离线 Demo 和确定性评测不需要启动 Redis，也不需要配置或访问外部 LLM。

## 离线 Demo

运行真实 Agent 主循环的确定性演示：

```powershell
python -m demo.run_demo
```

Demo 使用 `ScriptedLLM` 和内存预算，仍经过 Agent 的 ReAct 解析及真实文件工具调用。它在临时工作区创建实验输入，并写出 `experiment/summary.md`；控制台显示最终答案、事件序列和摘要内容。临时工作区会在演示结束时清理，需保留文件时可在演示运行期间从控制台查看其输出内容。

## API

在线服务需要配置 LLM 和 Redis，然后启动：

```powershell
uvicorn api:app --reload
```

调用普通接口或 SSE 流接口：

```powershell
curl -X POST http://localhost:8000/invoke -H "Content-Type: application/json" -d '{"task":"读取 hello.txt","session_id":"demo-1"}'
curl -N -X POST http://localhost:8000/stream -H "Content-Type: application/json" -d '{"task":"读取 hello.txt","session_id":"demo-2"}'
```

SSE 会按执行过程发送 `step_start`、`tool_call`、`tool_output`、错误/恢复事件和 `final_answer` 等事件。生产环境建议设置 `EMIT_THOUGHT=false`，避免 thought 事件泄露敏感信息。

## MCP 知识库集成

Agent 可把证据约束 RAG（`evidence-reflective-trace-rag`）作为 MCP 工具使用。边界是进程级的：

```text
ReAct Agent
├── read_file / write_file        ← 本地工作区
└── knowledge_search              ← MCP stdio 子进程
        └── evidence-reflective-rag：retrieval → evidence gate → reflection
            → generation → verification → failure attribution
```

每轮工具返回后的处理链：

```text
tool_output(JSON) ──> status == success ? ──是──> 答案保留 [S1] 引用
                                  │                        │
                                  否                        │
                                  ▼                        │
                   追加 [系统约束] 到历史（禁止常识补答）    │
                                  │                        │
                                  └──────> knowledge_result 事件（run_id / route / citations）
                                                          └─> rag_run_detail 回查完整 Trace
```

准备知识库（在 RAG 仓库执行）：

```powershell
python -m demo.seed_demo --data-dir demo/data
```

配置 `.env`（完整清单见 `.env.example`）：

```env
RAG_MCP_ENABLED=true
RAG_MCP_PYTHON=...\evidence-reflective-trace-rag\.venv\Scripts\python.exe
RAG_MCP_PROJECT=...\evidence-reflective-trace-rag
RAG_MCP_DATA_DIR=...\evidence-reflective-trace-rag\demo\data
RAG_MCP_DEFAULT_KB=financial_report
RAG_MCP_KB_ALIASES=financial_report=财务报告,contract=合同条款
```

联调（A 段不依赖 LLM，B 段需要 `DEEPSEEK_API_KEY`）：

```powershell
python -m demo.run_integrated_demo            # 链路自检 + 4 个端到端任务
python -m demo.run_integrated_demo --offline  # 只跑链路自检
```

`knowledge_search` 返回 `status` 四态（`success` / `clarify` / `abstain` / `verification_failed`），
只有 `success` 带 `answer` 与 `citations`。**非 success 状态会在下一轮 observation 后追加
`[系统约束]` 文本**（由代码注入，不是提示词约定），禁止模型用常识补齐证据缺口。

SSE 流会额外发送 `knowledge_result` 事件，携带 `run_id` / `route` / `citations` / `failure_type`，
可据此调用 RAG 的 `rag_run_detail` 回查完整 Trace。

### 并发与部署约束

- **单进程**：`uvicorn api:app --workers 1`。每个进程会在 lifespan 中拉起一个 MCP 子进程，
  多 worker 等于多份子进程与多份 SQLite 连接。
- **MCP 并发**：`RAG_MCP_MAX_CONCURRENCY`（默认 2）。RAG 侧 SQLite 单锁串行，
  提高并发只会排队，不会提升吞吐。
- **超时分级**：本地文件工具 `TOOL_TIMEOUT`（30s），MCP 工具 `RAG_MCP_TOOL_TIMEOUT`（60s，
  覆盖最多 2 轮检索 + 1 轮 LLM 反思）。
- **观测体积**：`MAX_OBSERVATION_CHARS`（默认 2000）对结构化 observation 逐级收缩，
  优先裁证据条数，其次裁 answer，最后只留标量字段，保证写进历史的始终是合法 JSON。
- **降级**：`RAG_MCP_ENABLED=false` 时工具返回 `status=disabled`；MCP 启动失败时服务仍可用，
  `GET /health` 返回 `{"status":"ok","mcp":"error: ..."}`。

### 已知限制

1. **抽取式答案有噪音**：RAG 的答案生成器会把证据集里所有命中 chunk 的首句拼接，
   与问题无关的句子可能一起出现（`evidence[].score` 已暴露，供上层裁剪）。
2. **`success` 不代表肯定**："合同未约定自动续约期限" 也是 `success`，演示时不要把
   否定事实当成"门控失效"或"肯定回答"。
3. **RAG 侧串行**：SQLite 单锁，`knowledge_search` 有效并发约 1~2，提高
   `RAG_MCP_MAX_CONCURRENCY` 不会提升吞吐。
4. **单进程部署**：`uvicorn --workers` 必须为 1，否则每个 worker 各起一个 MCP 子进程。
5. **无鉴权**：MCP stdio 走本机管道；若改 HTTP transport 需要自行补鉴权与限流。

## 评测

快速离线 smoke test 使用 `evaluation/eval_tasks.json`，不经过 Agent 主循环：

```powershell
python -m evaluation.eval_runner --fail-under 0.8
```

验证真实 Agent 主链时，运行确定性场景任务：

```powershell
python -m evaluation.eval_runner --deterministic-agent --tasks-file evaluation/deterministic_tasks.json --report-path evaluation/deterministic-report.json
```

The latest real-LLM run passed 31/31 online tasks (100%): 11 multi-step, 10 recovery, and 10 security cases; average task duration was 3.84 s. Redis acceptance passed with 4 processes, 1,000 shared budget increments, and 4 lock acquisitions (390.92 budget operations/s). The local in-memory Agent load test completed 40 requests across 4 processes at 53.61 requests/s (P95 7.377 ms); this is not an end-to-end online service benchmark.

The deterministic suite contains 40 cases: 11 multi-step file operations, 10 recovery cases, 10 security rejection cases, and 9 budget cases. The latest run passed 40/40 (100%), with 100% in every category. Tasks define inputs with `setup`, scripted turns with `replies`, file side effects with `expected_files`/`absent_files`, and event/output contracts with `expected_events` and `expected`. This does not measure real-LLM generalization.

需测试在线 LLM/Redis 时显式使用：

```powershell
python -m evaluation.eval_runner --online --tasks-file evaluation/online_tasks.json --report-path evaluation/online-report.json
```

报告 JSON 中：

Redis multi-process shared-budget/lock acceptance and local deterministic-Agent load test:

```powershell
python -m evaluation.redis_acceptance --instances 4 --operations 250
python -m evaluation.load_test --requests 40 --workers 4
```

The local load test uses scripted replies and an in-memory budget; it does not measure network LLM/Redis latency or contention.

- `summary.total`, `passed`, `failed`, `pass_rate`, and `categories` report overall and per-category results;
- `summary.average_steps` and `average_duration_ms` report step count and latency;
- `results` lists `status`, `output`, `error`, and `duration_ms` per task;
- `output` 包含最终答案、执行事件及隔离工作区文件内容；
- `error` 非空表示执行异常、缺少预期文件或缺少预期事件；
- 退出码为 `0` 表示通过率达到 `--fail-under`，否则为 `1`。

默认报告路径是 `evaluation/report.json`，报告为运行产物，已加入 Git 忽略规则。

## 测试与质量检查

```powershell
python -m pytest -q                      # 单元测试（真集成用例默认跳过）
$env:RAG_INTEGRATION=1; python -m pytest -q -k real_mcp   # 启动真实 MCP 子进程
python -m pytest --cov=. --cov-report=term-missing --cov-fail-under=80
python -m compileall -q .
ruff check .                             # 零告警
```

真实集成用例需要 `RAG_MCP_*` 指向可用的 RAG 仓库与 `demo/data`，
并读取共享用例 `demo/cases.json`；缺文件时会 `skip` 而不是失败。
