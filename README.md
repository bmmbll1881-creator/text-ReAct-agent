# Text ReAct Agent

基于 ReAct 的安全文件操作 Agent。读写工具仅允许访问 `WORKSPACE` 工作区内、且扩展名符合白名单的文件。

## 项目结构

```text
.
├── agent.py                 # Agent 主循环、LLM 调用、协议解析与事件
├── tools.py                 # 工作区隔离的文件读写工具
├── parser.py                # ReAct 文本协议解析
├── budget.py                # 本地与 Redis 会话预算、历史和锁
├── api.py                   # FastAPI 与 SSE 接口
├── config.py                # 环境变量配置
├── context.py               # 会话上下文压缩
├── demo/                    # 离线确定性演示和内存预算
├── evaluation/              # 评测器和评测任务
└── tests/                   # 单元与集成测试
```

## 安装与配置

需要 Python 3.10+。安装项目和测试依赖：

```powershell
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，配置 `CHAT_URL`、`DEEPSEEK_API_KEY` 和工作区等参数。在线 API/Agent 模式需要可访问的 Redis 服务；离线 Demo 和确定性评测不需要启动 Redis，也不需要配置或访问外部 LLM。

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

## 评测

快速离线 smoke test 使用 `evaluation/eval_tasks.json`，不经过 Agent 主循环：

```powershell
python -m evaluation.eval_runner --fail-under 0.8
```

验证真实 Agent 主链时，运行确定性场景任务：

```powershell
python -m evaluation.eval_runner --deterministic-agent --tasks-file evaluation/deterministic_tasks.json --report-path evaluation/deterministic-report.json
```

The deterministic suite contains 40 cases: 11 multi-step file operations, 10 recovery cases, 10 security rejection cases, and 9 budget cases. The latest run passed 40/40 (100%), with 100% in every category. Tasks define inputs with `setup`, scripted turns with `replies`, file side effects with `expected_files`/`absent_files`, and event/output contracts with `expected_events` and `expected`. This does not measure real-LLM generalization.

需测试在线 LLM/Redis 时显式使用：

```powershell
python -m evaluation.eval_runner --online --tasks-file evaluation/deterministic_tasks.json --report-path evaluation/online-report.json
```

报告 JSON 中：

Redis multi-process shared-budget/lock acceptance and local deterministic-Agent load test:

```powershell
python -m evaluation.redis_acceptance --instances 2 --operations 10
python -m evaluation.load_test --requests 100 --workers 4
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
python -m pytest -q
python -m pytest --cov=. --cov-report=term-missing --cov-fail-under=80
python -m compileall -q .
ruff check .
```
