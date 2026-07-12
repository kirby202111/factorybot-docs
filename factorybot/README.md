# factorybot — MES Agent 服务

基于 `整体技术选型与模块划分.md` 落地的 Agent 服务，三层自主度 + 三大横切能力：

- **L1 诊断型 Agent**：LangGraph ReAct，多步只读推理，输出 5M1E 根因假设（全程只读）。
- **L2 草稿型 Agent**：返工单 / 8D / SOP 草稿，策略模式生成，不落库（`requires_confirmation` 恒 True）。
- **L3 编排型 Agent**：supervisor + 代码节点 + agent 子图，confirmation gate 人在回路（受限写）。
- 横切：成本优化（`cost/`）、可观测（`obs/`）、长程任务（`longtask/` + LangGraph interrupt/resume）。

## 运行

默认 **mock 模式**——无需真实 Kafka / MySQL / Redis / Neo4j / LLM API Key：

```bash
cd factorybot
pip install -e .
python main.py            # http://127.0.0.1:8000/docs
```

mock 模式下：
- ACL 客户端从 `data/` 下 JSON fixtures 读数据（替代 MES REST / RAG 服务）。
- LLM 用 `MockChatModel`（确定性，驱动 ReAct 与结构化输出）。
- checkpoint 用 `MemorySaver`（替代 MySQL SqlSaver）。
- Redis 用进程内 `FakeRedis`；Kafka 动作卡推送用 `MockActionCardProducer`（仅记录日志）。

切真实模式：复制 `.env.example` → `.env`，按需填写 `LLM_API_KEY` / `MYSQL_URL` / `REDIS_URL` / `KAFKA_BOOTSTRAP_SERVERS`，对应组件自动从 mock 切换到真实实现。

## 目录

```
factorybot/
├── data/                # 模拟数据 (Kafka 事件 / REST 响应 / 图节点 / 文档)
├── app/
│   ├── api/             # FastAPI 路由 (diagnose / draft / l3)
│   ├── application/     # 应用服务 (编排) + L2 builders + L3 orchestrator
│   ├── domain/          # Agent 领域模型 (session/report/tool/draft/l3_state...)
│   ├── orchestration/   # L3: supervisor_graph + code_nodes + agents + scenarios
│   └── infrastructure/  # ai / acl / kafka / persistence / redis_ / obs / cost
└── tests/               # L1/L2/L3 端到端冒烟
```

## 对外端点

| 端点 | 层级 | 说明 |
|------|------|------|
| `POST /agent/diagnose` | L1 | 诊断，返回 `DiagnosisReport`（含 `subgraph_ref`） |
| `POST /agent/draft` | L2 | 草拟处置，返回 `Draft`（`requires_confirmation=True`） |
| `GET /agent/draft/{id}/evidence` | L2 | 回溯草稿证据 |
| `POST /agent/l3/{scenario}/start` | L3 | 启动编排（changeover/fault_response/complaint_8d/process_change） |
| `POST /agent/l3/{session_id}/confirm` | L3 | gate 确认，`Command(resume=token)` 续跑 |
| `GET /agent/l3/{session_id}/state` | L3 | 查会话状态（调试用） |

## 安全红线（启动断言）

- **L1 ReadOnlyToolGate**：注册期 + 启动期双重断言，任何非只读工具拒绝注册。
- **L2 NoWriteClientGate**：扫描 ACL client 方法名，命中写动词拒绝启动。
- **L3 WriteToolGate**：写工具必须声明 `requires_confirmation` + `writes_via`；supervisor 不持工具；禁止放行/拦截类工具。

> Agent 全程不碰 MES 原始表，写路径与人工下达一致，只是触发源从"人点按钮"变成"Agent 草拟 + 人确认"。
