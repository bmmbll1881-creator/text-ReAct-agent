# 文本处理 ReAct Agent

这是一个可在指定工作目录内安全读写文本文件的 ReAct Agent。它调用
DeepSeek 生成下一步操作，并通过本地与 Redis 双层预算限制执行次数。

## 功能边界

- 只允许读取和写入 `.txt`、`.json`、`.md` 文件。
- 所有文件路径必须在 `WORK_SPACE` 指定的工作目录内。
- 单次最多读取 20,000 个字符，单次最多写入 1,000,000 个字符。
- 同一会话默认最多执行 8 步；Redis 预算可跨进程共享。

## 配置环境

使用 Python 3.13 或更高版本，并安装仓库根目录中声明的依赖。将
`.env.example` 复制为 `.env`，再填入以下配置：

```dotenv
DEEPSEEK_API_KEY=你的 DeepSeek API 密钥
REDIS_URL=redis://localhost:6379/0
WORK_SPACE=./workspace
```

建议将 `WORK_SPACE` 设置为专用临时目录，不要直接指向项目根目录。

## 运行示例

```powershell
python agent.py "创建 note.txt，写入你好" --session-id demo-001
```

`session-id` 用于隔离 Redis 会话预算。每个独立任务应使用新的会话标识；
默认最大步数为 8，也可以通过 `--max-steps` 指定。

## 运行测试

```powershell
python -m pytest -q
```

测试使用模拟的 Redis 与大模型调用，并在临时目录执行文件操作，因此不需要
网络、真实 API 密钥或 Redis 服务。真实联调时，请使用独立的 Redis 数据库和
临时工作目录，避免影响已有会话与文件。
