# Text ReAct Agent

基于 ReAct 的文本文件 Agent，所有文件操作都限制在 `WORKSPACE` 工作区内。

## 安装

需要 Python 3.10+、Redis 和项目依赖：

```powershell
pip install -r requirements.txt
redis-server
```

复制 `.env.example` 为 `.env` 并填写 `CHAT_URL` 与 `DEEPSEEK_API_KEY`。

## 配置

配置由 `config.py` 统一读取环境变量。`MAX_CONCURRENT_RUNS` 限制同时运行的 Agent 数量，超出的请求排队等待。生产环境建议设置 `EMIT_THOUGHT=false`，因为 thought 事件可能泄露敏感信息。

## API

启动服务：

```powershell
uvicorn api:app --reload
```

调用任务：

```powershell
curl -X POST http://localhost:8000/invoke -H "Content-Type: application/json" -d '{"task":"读取 hello.txt","session_id":"demo-1"}'
curl -N -X POST http://localhost:8000/stream -H "Content-Type: application/json" -d '{"task":"读取 hello.txt","session_id":"demo-2"}'
```

## 离线评估

评估任务定义在 `evaluation/eval_tasks.json`，每个任务使用独立临时工作区并串行执行：

```powershell
python -m evaluation.eval_runner --fail-under 0.8
python -m evaluation.eval_runner --tasks read_file,combo --report-path evaluation/report.json
# 如需真实 LLM/Redis，显式使用 --online / Use real LLM/Redis explicitly with --online
python -m evaluation.eval_runner --online --fail-under 0.8
```

判定支持 `all`（全部关键词）、`any`（至少一个关键词）和 `regex`（正则表达式），匹配不区分大小写。通过率低于 `--fail-under` 时命令返回退出码 1。

## 测试与质量门禁

```powershell
python -m pytest -q
python -m pytest --cov=. --cov-report=term-missing --cov-fail-under=80
python -m compileall -q .
python -c "import api, agent, context, config, logger, tools, budget"
ruff check .
```
