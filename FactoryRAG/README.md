# rag-service

> 单服务 + 共享内核承载 **A（追溯型）/ B（文档型）/ E（Agentic）** 三路线 RAG。
> 面向车间 MES（SMT/PCBA + 整机组装）的追溯 / 文档问答 / 统一入口场景。

设计与选型口径见上级文档：
- [`../RAG服务/rag-service-技术选型和实现方案.md`](../RAG服务/rag-service-技术选型和实现方案.md)
- [`../RAG服务/rag-service-整体结构设计.md`](../RAG服务/rag-service-整体结构设计.md)
- 实现进度：[`PROGRESS.md`](PROGRESS.md)

---

## 1. 架构定位

**单服务 + 共享内核**：A/B/E 三路线同居一个 FastAPI 进程，共用 `app/shared/` 内核
（LLM / Embedding / 可观测 / 配置 / Kafka / ACL / 持久化 / 租户 / Web）。路线间**禁止
直接 import 对方的 application/domain**，一律经 `shared/acl/` 的 Port（`TraceRagPort` /
`DocRagPort`）+ Adapter（单服务内 InProcess 直调，拆服务时换 Http，业务代码零改动）。

| 路线 | 类型 | 入口 | 主存储 | 默认 |
|------|------|------|--------|------|
| B | 文档型 | `POST /rag/docs/{query,search,ingest}` | ChromaDB（嵌入式）+ MinIO + MySQL | ✅ 开 |
| A | 追溯型 | `POST /rag/trace/{query,expand}` | Neo4j（5M1E 图）+ MySQL | ⏸ 灰度 |
| E | Agentic | `POST /agent/chat` `GET /agent/explain/{id}` | Redis（缓存/审计）+ MySQL | ⏸ 灰度 |

灰度顺序：**先 B 再 A，E 收口**（`RAG_<ROUTE>__ENABLED` 控制 router 注册与 consumer 启停）。

### 核心安全契约

- **只读红线**：rag-service 是 MES 的只读旁路，**从不回写**。`ReadOnly*Gate` 在 lifespan
  启动期扫描，任一命中写动作即 fail-fast 拒绝启动（最坏情况是"没检索出来"，不产生写副作用）：
  - `ReadOnlyAclGate`：MES 只读客户端方法名禁止写动词（create/update/delete/...）；
  - `ReadOnlyProjectionGate`：A 图投影 Cypher 禁 `DELETE`/`REMOVE`（历史快照边永不删）；
  - `RawDataTopicGate`：禁止订阅 `dc.*` 原始数据流（高频采集不全量入图）；
  - `ReadOnlyIngestionGate`：B 摄入/重索引 handler 禁止写 MES 调用；
  - `ReadOnlyToolGate`：E 工具注册表拒绝 `read_only=False` 的工具。
- **B chunk 不可变**：写入 ChromaDB 后所有 metadata 永不修改；版本升版 = 追加新版本 chunk
  （带新 `version`），不翻转老 chunk 的 state；版本隔离靠查询
  `where={"state":"PUBLISHED","version_kind":..,"version":..}` 过滤。
- **版本锚点通用化**：`VersionAnchor(kind, ref_id, version)` 统一贯穿 B/A/共享内核，
  覆盖 route/bom/rule/asset/standard 五类版本（工艺路线 SOP / 设备维修手册 / IPC 标准等皆可版本管理）。
- **强制带版本红线**：B 工艺绑定型（PROCESS_BOUND）文档检索需 ROUTE 版本锚点（`version`+
  `version_kind="route"`）必填，入口校验拒绝缺失，**绝不退回"查最新 ACTIVE"**（避开在制品不切换
  工艺语义陷阱）；设备绑定型需 ASSET 锚点（`version_ref_id` 必填）。
- **版本一致性三段链**（rag-service 负责第一段）：图 `[:SNAPSHOT_OF_{kind} {version}]`
  快照边物理锁定生产时版本 -> L1 `evidence.version_anchor` -> L2 `Draft.version_anchor` ->
  MES 校验 ACTIVE。A 升版发 `rag.reindex.request` 内部事件通知 B 重索引。

---

## 2. 目录结构

```
FactoryRAG/
├── app/
│   ├── main.py                    # FastAPI 入口（create_app：lifespan+middleware+register）
│   ├── config.py                  # load_settings()
│   ├── api/                       # 路由层：deps / middleware / errors / register / v1/{trace,doc,chat}_router
│   ├── routes/                    # 三路线（各自 domain/application/infrastructure）
│   │   ├── document/              # B 文档型（chromadb/minio/parser/handlers）
│   │   ├── traceability/          # A 追溯型（neo4j schema/retriever/projections/rag）
│   │   └── agentic/               # E Agentic（ai/route_graph_builder + acl/l1,l2 + persistence）
│   └── shared/                    # 共享内核（10 子包）
│       ├── ai/                    # LlmPort + ObservableChatModel + llm_factory（provider 无关，缺依赖静默回退 Mock）
│       ├── embedding/             # EmbeddingPort + BgeClient/BgeReranker（sidecar+本地兜底）
│       ├── obs/                   # OTel+prometheus+structlog（失败不反噬业务）
│       ├── config/                # BaseSettings + RagSettings（路线开关+子配置）
│       ├── kafka/                 # ConsumerGroup+幂等+位点+projection_handler（手动 ack+双重幂等）
│       ├── acl/                   # 路线间 Port/Adapter + MES 只读客户端 + ReadOnly*Gate
│       ├── persistence/           # DeclarativeBase + DbEngines（mysql/neo4j/chroma/redis 懒初始化）+ 幂等/位点模型
│       ├── tenant/                # TenantContext + 跨服务传递协议一处定义
│       ├── web/                   # container（DI 组合根）+ lifespan + health
│       └── events/                # 版本契约（VersionAnchor/ReindexRequest，三段链第一段）
├── alembic/                       # MySQL 多 schema 迁移（env.py async+asyncmy / versions/0001_initial）
├── alembic.ini
├── tests/                         # 单元测试（锁住红线不变式）
├── pyproject.toml
├── Dockerfile                     # python:3.11-slim + gunicorn uvicorn worker
├── docker-compose.yml             # rag-service + neo4j/mysql/redis/minio/kafka/bge-inference
├── .env.example                   # 前缀 RAG_、嵌套 __、路线级开关
└── PROGRESS.md                    # 实现进度
```

> Neo4j 用 `SchemaInitializer`、ChromaDB 用 `ChromaCollectionInitializer` 初始化（均非 Alembic）；
> Alembic 只管 MySQL 多 schema（`rag_shared`/`rag_trace`/`rag_doc`/`rag_agentic`）。

---

## 3. 启动

### 3.0 快速开始（Quick Start）

前置：已装 [uv](https://docs.astral.sh/uv/)（`pip install uv` 或见官网安装脚本）与 Docker（起基础设施用）。
Python 由 `.python-version` 锁定为 3.12，uv 复用本地已装解释器，无需手动安装。

```bash
# 1) 安装依赖（建 .venv + 生成 uv.lock + 装 dev 组）
uv sync

# 2) 准备配置（按需改 MySQL/Redis/Neo4j/MinIO/Kafka 连接 + 路线开关）
cp .env.example .env

# 3) 起基础设施：mysql/redis/neo4j/minio/kafka/bge-inference
docker-compose up -d

# 4) 首次：跑 MySQL 迁移（DB URL 取自 RAG_MYSQL__DSN，覆盖 alembic.ini 占位）
uv run alembic upgrade head

# 5) 起服务
uv run uvicorn app.main:app --reload                      # 开发热重载
# uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b :8000   # 类生产

# 6) 验证
curl http://localhost:8000/health        # 存活
curl http://localhost:8000/ready         # 就绪（各存储/路线降级状态）
uv run pytest                            # 跑测试（单元红线不变式，不触重依赖/外部基础设施）
```

> 默认仅 **B（document）** 开；A/E 经 `RAG_TRACEABILITY__ENABLED` / `RAG_AGENTIC__ENABLED` 灰度打开，见 §3.2、§7。
> 重依赖（chromadb / neo4j / langgraph / minio / aiokafka / langchain）均懒导入，模块导入不触发；
> 运行时按启用的路线需要对应依赖就绪。下文 §3.1–§3.3 为各步详细说明。

### 3.1 依赖（uv 管理）

项目用 [uv](https://docs.astral.sh/uv/) 管理虚拟环境与依赖锁。Python 版本锁定为 **3.12**
（见 `.python-version`，满足 `requires-python>=3.11`；uv 复用本地已装的 cpython-3.12，
无需联网下载解释器）。生产镜像 `Dockerfile` 仍用 `python:3.11-slim`，二者均在受支持区间内。

```bash
uv sync                # 建 .venv、解析依赖、生成 uv.lock、装 dev 组
```

常用命令：

| 操作 | 命令 |
|------|------|
| 同步环境（按 `uv.lock`） | `uv sync` |
| 加 / 升依赖 | `uv add <pkg>` / `uv add --dev <pkg>` |
| 在 venv 内跑命令 | `uv run <cmd>`（如 `uv run pytest`、`uv run uvicorn ...`） |
| 升级锁文件 | `uv lock --upgrade` |

重依赖（chromadb / neo4j / langgraph / minio / aiokafka / langchain）均在函数内懒导入，
模块导入不触发；运行时按启用的路线需要对应依赖就绪。`uv.lock` 提交进库以锁定可复现环境。

### 3.2 配置

复制 `.env.example` 为 `.env`，按需调整。环境变量前缀 `RAG_`、嵌套分隔符 `__`，
如 `RAG_MYSQL__DSN` -> `mysql.dsn`、`RAG_DOCUMENT__ENABLED` -> `document.enabled`。

路线级开关（灰度引入）：

```ini
RAG_DOCUMENT__ENABLED=true       # B 先行
RAG_TRACEABILITY__ENABLED=false  # A 灰度打开
RAG_AGENTIC__ENABLED=false       # E 收口（依赖 A/B 就绪）
```

### 3.3 基础设施 + 服务

```bash
docker-compose up -d              # mysql/redis/neo4j/minio/kafka/bge-inference
```

首次启动前跑 MySQL 迁移（见 §4）。随后本地起服务：

```bash
uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b :8000
# 或开发期
uv run uvicorn app.main:app --reload
```

健康检查：`GET /health`（存活）、`GET /ready`（就绪，含各存储/路线降级状态）、`GET /metrics`（prometheus）。

---

## 4. 数据库迁移（Alembic）

Alembic 管 MySQL 4 schema / 6 表；DB URL 从 `RAG_MYSQL__DSN` 注入（覆盖 `alembic.ini` 占位）。

```bash
# 升级到最新
uv run alembic upgrade head

# 回滚
uv run alembic downgrade base

# 生成新迁移（模型变更后）
uv run alembic revision --autogenerate -m "describe change"
```

`0001_initial` 建 `rag_shared`（index_idempotency / index_offset）、`rag_trace`（subgraph_audit）、
`rag_doc`（knowledge_document / document_version）、`rag_agentic`（answer_audit / route_trace）。

---

## 5. 测试

```bash
uv run pytest             # 全量（需装齐重依赖）
uv run pytest tests/ -v   # 单元测试（红线不变式，不触重依赖）
```

| 测试 | 锁住的不变式 |
|------|--------------|
| `test_gates.py` | 5 个 ReadOnly*Gate 启动断言（写动作 fail-fast） |
| `test_chunk_immutability.py` | B chunk 不可变 + `to_metadata_dict` 字段完备 + `_build_where` 强制 state/版本锚点 |
| `test_version_contract.py` | VersionAnchor/ReindexRequest + `parse_anchor` 缺失版本抛错 + `to/from_metadata` |
| `test_port_adapter.py` | InProcess Adapter 收原语 -> 构造路线 DTO -> 直调 svc（跨路线零 import） |
| `test_retrieval_enforce_version.py` | B PROCESS_BOUND 缺 ROUTE 锚点 / ASSET_BOUND 缺 ASSET 锚点 -> ValueError |
| `test_route_graph.py` | E `_FallbackGraph` 按意图走 tool/delegate/converge（langgraph 不可用时） |

---

## 6. 路线间调用约定

跨路线调用经 `shared/acl/` Port，**Port 方法只收原语**（str / datetime / list[str]），
调用方（A/E）零跨路线 import；各路线 DTO 由 InProcess Adapter 内部构造，Http Adapter
组装端点 JSON。跨路线枚举一律用**枚举值字符串**：

- seed kind：`"WipUnit" | "WorkOrder" | "InventoryBatch" | "Defect" | "Asset"`
- doc category：`"PROCESS_BOUND" | "ASSET_BOUND" | "GENERAL"`
- doc type：`"SOP" | "MANUAL" | "STANDARD"`

| 调用方 | Port | 用途 |
|--------|------|------|
| A `_enrich_suggested_action` | `DocRagPort.query` | 拉 B 的 SOP 片段补充 suggested_action（带版本锚点 route/bom/...） |
| E `query_traceability_graph` 工具 | `TraceRagPort.expand` | 取 A 子图（不综合） |
| E `search_docs` 工具 | `DocRagPort.search` | 检索 B 文档片段 |
| agent-service L1/L2（拆服务后） | `TraceRagPort.query` / `DocRagPort.query` | 经 Http Adapter |

---

## 7. 灰度顺序

1. **B（document）先行**：`RAG_DOCUMENT__ENABLED=true`，ChromaDB + MinIO + MySQL 就绪。
2. **A（traceability）灰度**：`RAG_TRACEABILITY__ENABLED=true`，Neo4j + Kafka 投影就绪；
   A 升版发 `rag.reindex.request` 通知 B 重索引。
3. **E（agentic）收口**：`RAG_AGENTIC__ENABLED=true`，依赖 A/B Port 就绪 + agent-service L1/L2。
   E 不自己多步推理，深度推理委托 L1/L2（透传 `traceparent`，决策#1）。

---

## 8. 可拆性

单服务内全部 Port 绑定 InProcess Adapter（直调 application service，不走本机 REST）。
拆服务时仅需在 `shared/web/container.py` 把 `InProcess*Adapter` 换成 `Http*Adapter`，
业务代码（routes）零改动。组合根（container）是唯一允许 import 路线 application 的地方。
