# rag-service 技术选型和实现方案

> 本文是 rag-service（单一 Python 微服务）的**技术选型与实现方案**，位于 [`rag-service-整体结构设计.md`](rag-service-整体结构设计.md) 之下、各路线实现方案之上的一层：
> - 把整体结构设计引用但**未集中**的技术选型（版本 / 库 / 模型）收敛为一张表，并解决三路线文档之间的版本漂移（如 LangGraph 版本、默认 LLM provider）；
> - 把整体结构设计 §11 列为"待办"的**迁移与落地**变成可执行计划：共享内核构建顺序、三路线文档从"独立服务包结构"投影到"单服务模块"的迁移、决策 #3 联动 PUBLISHED 的修订落地；
> - 给出共享内核关键抽象的**代码骨架**、**分阶段交付**、**风险与对策**、**测试与验收**。
>
> **与整体结构设计的关系**：结构设计定"怎么切模块 / 怎么隔离 / 怎么可拆"；本文定"选什么版本 / 按什么顺序建 / 怎么从现状迁过去 / 怎么验收"。
> **与各路线实现方案的关系**：各路线实现方案定"路线内部细节（Cypher / 向量检索 / 意图路由）"；本文定"共享内核 + 单服务骨架 + 跨路线迁移"，路线内部技术细节仍以各路线文档为准。
>
> 口径纪律：RAG 在本项目是**设计阶段的引入规划**，不是线上已落地能力（同 [`RAG服务引入路线.md`](RAG服务引入路线.md) §6）。本文讲"规划 / 选型 / 落地路径"，不讲"已上线"。
>
> **范围说明**：C 数据型、D 防错即时辅助两条路线暂不建设（descoped），不在本文档范围内。

---

## 0. 设计取舍回顾

承接 [整体结构设计 §0](rag-service-整体结构设计.md#0-设计取舍)，本文在此基础上补两条**选型层**的取舍：

| # | 取舍 | 理由 |
|---|------|------|
| 1 | 模块化单体 + 共享内核（Shared Kernel） | 三路线基础设施 90% 同构，合并单服务消除重复基类 |
| 2 | 可拆性是硬约束 | 路线间调用走 `shared/acl/` Port/Adapter，拆服务只换 Adapter 绑定 |
| 3 | 多存储共存于单进程 | Neo4j/ChromaDB/MySQL/Redis/MinIO 共存，Port 隔离故障域，按路线降级 |
| 4 | 只读红线靠启动断言兜底 | 统一 `ReadOnly*Gate` 在 lifespan 启动期扫描，发现写动作即拒绝启动 |
| 5 | **选型收敛到一张表，解决三路线版本漂移** | 三路线文档各自钉死版本且存在不一致（LangGraph 0.1+ vs ≥0.2）；本文以共享内核为权威收敛口径，路线文档以本文为准回填 |
| 6 | **B 向量库选 ChromaDB（而非 PGVector/专用向量库）** | 车间 ToB 文档量小 + 查询强制带 `route_version`（版本过滤退化成等值）+ 求开发简；chunk 不可变绕开多记录事务弱点，MinIO 重建兜底备份弱。详见 §1.2 |

> 不选多服务的理由见整体结构设计 §0 末。选型层同样遵循"先收敛、再灰度引入（B -> A -> E）"。

---

## 1. 技术选型总览

### 1.1 选型总表

> **收敛原则**：被 ≥2 路线复用的依赖归 `shared/`（版本由本文钉死）；仅单路线使用的依赖留该路线 `infrastructure/`（版本以路线文档为准，本文列出供对齐）。冲突项在 §1.2 给出统一口径。

| 层次 | 选型 | 版本 | 用途 | 归属 |
|------|------|------|------|------|
| 语言 | Python | 3.11+ | 主体语言 | shared |
| Web 框架 | FastAPI | 0.110+ | HTTP 入口，异步，原生 OpenAPI，与 Pydantic 无缝 | shared |
| ASGI / 部署 | gunicorn + uvicorn worker | ≥21.2 / ≥0.29 | K8s 单服务，共享 worker 池 | shared |
| LLM 抽象 | langchain-core `BaseChatModel` | 0.2+ | `LlmPort` 包装对象，provider 无关 | shared/ai |
| LLM 工厂 | `llm_factory`（自研） | - | 按 config 创建 `ObservableChatModel`，适配 Claude / 通义千问 / DeepSeek / 本地模型 | shared/ai |
| Agent 编排 | LangGraph | **≥0.2（统一）** | E 轻量路由图，`recursion_limit=6` | E |
| 检索编排 | LlamaIndex | 0.10+ | A: `PropertyGraphIndex`；B: `VectorStoreIndex` + `ChromaVectorStore` | A / B |
| Embedding 模型 | bge-m3 | 1.0+，1024 维 cosine | 向量化（A 缺陷语义入口 / B 文档主体 / A DefectCatalog） | shared/embedding |
| Embedding 推理 | sentence-transformers + FlagEmbedding | ≥3.0 / ≥1.2 | 本地化推理（车间网隔离） | shared/embedding |
| Reranker | bge-reranker-v2-m3（`FlagReranker use_fp16=True`） | - | B cross-encoder 精排 | shared/embedding |
| 图存储 | Neo4j | 5.x（镜像 `neo4j:5.20`） | A 图主体 + DefectCatalog 原生向量索引（1024 维 cosine） | A |
| 图驱动 | neo4j-python-driver（async） | ≥5.20 | A Cypher 异步执行 | A |
| 向量库 | **ChromaDB** | 0.5+（嵌入式 Parquet 持久化） | B chunk 向量 + metadata，chunk 不可变 + 版本隔离靠查询过滤 | B |
| 关系库 | MySQL | 8.0（镜像 `mysql:8.0`） | shared 幂等 / 位点 + A 审计 + E `answer_audit`/`route_trace` + B 治理聚合导出 | shared / A / E / B |
| MySQL 驱动 | SQLAlchemy 2.0 async + asyncmy | 2.0+，asyncmy ≥0.2.9 | shared / A / E / B | shared |
| ORM 基类 | SQLAlchemy 2.0 `DeclarativeBase` | 2.0+ | `shared/persistence/base.py`，MySQL | shared |
| 迁移工具 | Alembic | 1.13+ | 统一管 MySQL 多 schema（Neo4j/ChromaDB 除外） | shared |
| 对象存储 | MinIO（S3 兼容，path-style-access） | minio-py ≥7.2 | B 原始文档文件（向量库可从 MinIO 重建） | B |
| 缓存 | Redis | 7（镜像 `redis:7-alpine`） | A 子图缓存 / B 检索缓存 / E 查询缓存 | shared / A / B / E |
| Redis 客户端 | redis-py（async） | 5.0+ | | shared |
| 消息中间件 | Kafka + aiokafka | aiokafka 0.10+ | 领域事件消费（A 图投影 / B 重索引） | shared/kafka |
| HTTP 客户端 | httpx（async） | 0.27+ | ACL 出站：MES 只读 REST / agent-service 委托 / 拆服务后的 Http Adapter | shared/acl |
| 数据校验 | Pydantic v2 | ≥2.6 | 端点 schema / 结构化输出（`with_structured_output`） | shared |
| 配置 | pydantic-settings | ≥2.2 | `BaseSettings` + `RagSettings`，环境变量前缀 `RAG_` | shared/config |
| 可观测-trace | OpenTelemetry Python SDK | ≥1.24 | span / context 传播，W3C `traceparent` | shared/obs |
| 可观测-metrics | prometheus-client | ≥0.20 | Counter/Histogram，统一前缀 `rag_` | shared/obs |
| 可观测-log | structlog + JSONRenderer | - | 自动注入 `trace_id`/`span_id` | shared/obs |
| trace 存储（火焰图） | Tempo / Jaeger | - | SRE 火焰图 | shared/obs |
| trace 存储（证据链） | MySQL 平铺表 | - | 工程师 UI 证据链回溯，同源 `trace_id` 串联 agent-service 与 MES | shared/obs |
| 文档解析 | unstructured + pypdf + python-docx | ≥0.14 / ≥4.0 / ≥1.1 | B 摄入期半结构化 / 非结构化解析 | B |
| Docker 基础镜像 | python:3.11-slim | - | 单服务镜像 | shared |

> **默认 LLM provider（路线级，经 `llm_factory` 统一切换）**：A = DeepSeek，B = 通义千问（qwen-plus），E = DeepSeek。三者均 provider 无关、可插拔，任何模型降级须过 mes-eval `EvalGate`（成本优化横切归 agent-service，rag-service 仅 E 路由图按需引用）。
>
> **B 向量库选型说明**：ChromaDB 嵌入式 persistent client 跟随 rag-service 进程，**无需独立向量库 service**；原始文档留 MinIO，向量库可从 MinIO 重建（备份兜底）。详见 §1.2。

### 1.2 选型理由（关键项）

**为什么 B 选 ChromaDB（而非 PGVector / Milvus / Qdrant）？**

前提三条：① 车间 ToB 项目**文档量小**（数千文档 / 数十万 chunk 以内，远未到任何向量库瓶颈）；② 工艺路线类查询**强制带 `route_version`**，版本过滤退化成单字段等值，ChromaDB 的 `where` 能做且能 pre-filter；③ **求开发简**--ChromaDB 嵌入式零额外服务、LlamaIndex `ChromaVectorStore` 集成最成熟、少装一套 PG+pgvector+asyncpg+Alembic PG 方言。

核心设计**chunk 不可变**绕开 ChromaDB 最大弱点（多记录翻转无事务）：
- chunk 写入后不再修改，metadata 固定带 `route_version`/`state`/`tenant_scope`/`doc_id`/`doc_type`/`chunk_seq`/`locator`；
- 工艺升版时**不翻转老 chunk 状态**，而是追加新版本 chunk；查询带 `where={"state":"PUBLISHED","route_version":rv}` 天然只召回对应版本；
- ChromaDB 里根本没有"批量翻转"这个操作，多记录事务弱点直接消失；这本身也符合 B 详细设计 §4.4"版本即不可变快照、旧版不删可回溯"的语义。

代价（已知接受）：
- HA / 备份弱（无 PITR）-> 用"**可重建**"兜底：原始文档留 MinIO，ChromaDB Parquet 文件定期备份，向量库可从 MinIO 原始文件 + chunk 策略重建；
- 无聚合查询（只有 count）-> 治理 / 审计聚合（`GROUP BY`）导出到 MySQL 做（幂等 / 位点 / 审计表本就在 MySQL）；
- 规模上限 -> 文档少不触发；
- 单写者并发 -> 重索引量小可接受。

对比 PGVector / Milvus / Qdrant：在"文档少 + 强制带版本 + 求简"前提下，ChromaDB 的简大于其弱；PGVector 的 SQL 过滤 / 同库事务优势在本场景（等值版本过滤 + chunk 不可变）不再决定性。若未来文档量 / QPS 上到专用向量库甜区，LlamaIndex `VectorStoreIndex` 抽象兜住切换成本，但版本过滤逻辑要从 ChromaDB `where` 改写为各库标量过滤 API。

> **必须守的红线（用 ChromaDB 也守）**：
> 1. **强制带版本**：`DocumentRetrievalService` 入口校验 `route_version` 必填（工艺绑定型），缺失拒绝，不退回"查最新 ACTIVE"--避开"在制品不切换工艺"语义陷阱（工单绑 v3，最新 ACTIVE 是 v4，退回查 v4 会答出不适用 SOP）。设备绑定型按 `asset_id` 过滤，通用知识型不带版本。
> 2. **chunk 不可变**：升版 = 追加新版本 chunk，不改老 chunk。
> 3. **单条软删可接受**：文档撤回 / 废弃是单条 upsert 改 `state=DEPRECATED`（ChromaDB 单条原子），区别于"批量翻转"（不行）。
> 4. **备份兜底**：MinIO 留原始文件，ChromaDB 可重建。
> 5. **聚合导出 MySQL**：治理 / 审计聚合在 MySQL 做。
> 6. **重索引幂等**：`event_id` + `(doc_id, route_version, chunk_seq)` 去重，可重跑。

**为什么 LangGraph 仅 E 用、A/B 不用？**
A 是"事件投影建图 + Cypher 检索 + LLM 单轮综合"，无开放规划；B 是"向量检索 + rerank + LLM 单轮综合"，步骤固定。两者用 async 函数编排 + 策略模式更简洁。E 是"意图路由 + 工具选择 + 轻量多步组合"，存在开放分支，用 LangGraph `StateGraph`，`recursion_limit=6` 作硬上限靠框架兜底。与 agent-service L1/L3 同构、L2 不用 LangGraph 的取舍一致（见 [整体技术选型与模块划分](../整体技术选型与模块划分.md) §2.2）。

> **版本漂移统一口径**：E 实现方案写 LangGraph 0.1+、E 详细设计写 ≥0.2，不一致。本文以 **≥0.2** 为准（`Command(resume=…)` 语义依赖，与 agent-service 对齐），E 两份路线文档需回填统一。

**为什么图（Neo4j）与向量（ChromaDB）分离，而非一库通吃？**
A 的核心是**属性图 + Cypher 多跳**（5M1E 串联靠显式图边，`SNAPSHOT_OF_ROUTE` 快照边物理锁定版本），Neo4j 的原生向量索引只用于 DefectCatalog 缺陷描述语义入口这一个点；B 的核心是**大规模 chunk 向量检索 + 版本/权限过滤**，ChromaDB 嵌入式轻量、LlamaIndex 集成成熟。强行用 Neo4j 存 chunk 向量会在多跳查询与高维近邻检索之间互相拖累；强行让 ChromaDB 存图则丢失 Cypher 表达力。分离后故障域也独立（Neo4j 不可用只拖垮 A，ChromaDB 不可用只拖垮 B）。

> **Neo4j 向量索引（A）**：原生向量索引，`vector.dimensions=1024, similarity=cosine`（A 实现方案 §4.5）。ChromaDB（B）用默认 HNSW + cosine，不暴露 `m`/`ef_construction` 参数（chunk 不可变 + 文档少，默认参数足够，评测后若需调优再评估切换）。

**为什么 Alembic 统一管 MySQL 多 schema？**
现状缺口：各路线用原始 SQL DDL，无迁移工具（整体结构设计 §9）。Alembic 把 MySQL（shared/A/E + B 的幂等/位点/审计）的 schema 演进纳入版本管理，CI 校验"模型与迁移一致"。Neo4j 与 ChromaDB 除外：图库 DDL 幂等即可（`SchemaInitializer`），ChromaDB collection 由代码初始化（chunk 不可变，无 schema 演进）。

**LlamaIndex 与 langchain-core 如何共存？**
边界清晰：`shared/ai/` 基于 langchain-core `BaseChatModel` 做 LLM 抽象（`LlmPort` + `ObservableChatModel`），所有路线的 LLM 调用走它；A/B 的**检索编排**（图索引构建、向量索引构建、retriever）用 LlamaIndex 0.10+（A `PropertyGraphIndex` / B `VectorStoreIndex` + `ChromaVectorStore`）。两者不重叠：LLM 抽象归 shared，检索编排归路线 `infrastructure/`。

### 1.3 依赖清单（pyproject.toml 片段）

> 合并三路线依赖、去重，按归属分组。仅单路线使用的依赖标 `[route]`。

```toml
[project]
name = "rag-service"
requires-python = ">=3.11"

[project.dependencies]
# ── shared：Web / 部署 / 校验 / 配置 ──
fastapi = ">=0.110"
uvicorn = {extras = ["standard"], version = ">=0.29"}
gunicorn = ">=21.2"
pydantic = ">=2.6"
pydantic-settings = ">=2.2"

# ── shared：LLM 抽象 ──
langchain-core = ">=0.2"
# provider 按需选装：langchain-anthropic / langchain-community(通义/DeepSeek) / 本地

# ── shared：持久化 / 迁移（MySQL）──
sqlalchemy = {extras = ["asyncio"], version = ">=2.0"}
asyncmy = ">=0.2.9"        # MySQL async driver
alembic = ">=1.13"
redis = ">=5.0"

# ── shared：消息 / HTTP / 可观测 ──
aiokafka = ">=0.10"
httpx = ">=0.27"
opentelemetry-sdk = ">=1.24"
opentelemetry-instrumentation-fastapi = ">=0.45b0"
opentelemetry-instrumentation-httpx = ">=0.45b0"
prometheus-client = ">=0.20"
structlog = ">=24.1"

# ── shared：embedding（bge 本地化推理）──
sentence-transformers = ">=3.0"
FlagEmbedding = ">=1.2"

# ── A 追溯型 ──
neo4j = {extras = ["async"], version = ">=5.20"}      # [A]
llama-index = ">=0.10"                                  # [A/B]

# ── B 文档型 ──
chromadb = ">=0.5"                                      # [B] 嵌入式向量库
llama-index-vector-stores-chroma = ">=0.2"              # [B] LlamaIndex Chroma 适配
minio = ">=7.2"                                         # [B] 原始文档对象存储
unstructured = ">=0.14"                                 # [B] 文档解析
pypdf = ">=4.0"                                         # [B]
python-docx = ">=1.1"                                   # [B]

# ── E Agentic ──
langgraph = ">=0.2"                                     # [E]
```

### 1.4 部署形态（docker-compose 片段）

> B 用 ChromaDB 嵌入式，无需独立向量库 service；Parquet 持久化挂卷。

```yaml
services:
  rag-service:
    build: .
    image: rag-service:latest
    environment:
      RAG_LLM__PROVIDER: deepseek
      RAG_EMBEDDING__BASE_URL: http://bge-inference:8080
      RAG_NEO4J__URI: bolt://neo4j:7687
      RAG_MYSQL__DSN: mysql+asyncmy://rag:rag@mysql:3306/rag
      RAG_REDIS__URL: redis://redis:6379/0
      RAG_KAFKA__BOOTSTRAP: kafka:9092
      RAG_MINIO__ENDPOINT: minio:9000
      RAG_CHROMA__PERSIST_DIR: /data/chroma   # ChromaDB 嵌入式 Parquet 持久化
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
      # 路线级开关（灰度引入：先 B 再 A，E 收口）
      RAG_DOCUMENT__ENABLED: "true"
      RAG_TRACEABILITY__ENABLED: "false"
      RAG_AGENTIC__ENABLED: "false"
    depends_on: [neo4j, mysql, redis, kafka, minio, bge-inference]
    volumes: ["chromadata:/data/chroma"]
    ports: ["8000:8000"]

  bge-inference:                    # bge-m3 + reranker 本地化推理 sidecar
    image: michaelfeil/infinity:latest   # 或 TEI；车间网隔离必备
    ports: ["8080:8080"]

  neo4j:        { image: "neo4j:5.20", environment: { NEO4J_AUTH: neo4j/rag } }
  mysql:        { image: "mysql:8.0", environment: { MYSQL_ROOT_PASSWORD: rag, MYSQL_DATABASE: rag } }
  redis:        { image: "redis:7-alpine" }
  minio:        { image: "minio/minio", command: "server /data --console-address ':9001'" }

volumes:
  chromadata:
```

---

## 2. 共享内核（shared/）实现方案

> 共享内核只放被 ≥2 路线复用的抽象与实现（整体结构设计 §3）。本节给出关键抽象的代码骨架，路线内部实现仍以各路线文档为准。

### 2.1 `shared/ai/` -- LLM 抽象

```python
# app/shared/ai/port.py
from typing import Protocol

class LlmPort(Protocol):
    """LLM 抽象接口（DIP）：业务层依赖它而非具体 provider。"""
    async def achat(self, messages: list, **kwargs) -> "ChatResult": ...
```

```python
# app/shared/ai/observable_chat_model.py
class ObservableChatModel:
    """包装任意 langchain-core BaseChatModel，统一埋 token/延迟/模型/prompt_version，provider 无关。"""
    def __init__(self, inner, obs: "ObservabilityPort", model_name: str, prompt_version: str):
        self._inner, self._obs = inner, obs
        self.model_name, self.prompt_version = model_name, prompt_version

    async def achat(self, messages, **kwargs):
        with self._obs.llm_span(model=self.model_name, prompt_version=self.prompt_version):
            resp = await self._inner.ainvoke(messages, **kwargs)
            self._obs.record_llm(model=self.model_name, tokens=resp.usage_total_tokens,
                                 latency_ms=..., prompt_version=self.prompt_version)
            return resp
```

```python
# app/shared/ai/llm_factory.py
def llm_factory(settings: "LlmSettings", obs: "ObservabilityPort") -> LlmPort:
    provider = settings.provider  # claude | qwen | deepseek | local
    inner = _build_inner(provider, settings)   # langchain-{provider} BaseChatModel
    return ObservableChatModel(inner, obs, settings.model_name, settings.prompt_version)
```

> 现状：A/B/E 三份实现方案各自定义 `llm_factory.py`。本节将其上移到 `shared/ai/`，各路线删本地副本、改 import（迁移映射见 §6.2）。

### 2.2 `shared/embedding/` -- Embedding 抽象

```python
# app/shared/embedding/port.py
class EmbeddingPort(Protocol):
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...   # 1024 维
```

```python
# app/shared/embedding/bge_client.py
class BgeClient(EmbeddingPort):
    """bge-m3 1024 维批量推理，A（缺陷语义入口）/ B（文档主体）共用。"""
    DIM = 1024
    async def embed_batch(self, texts): ...   # 走 bge-inference sidecar 或本地 sentence-transformers

class BgeReranker:
    """bge-reranker-v2-m3 cross-encoder 精排（B 用）。FlagReranker(use_fp16=True)。"""
    async def rerank(self, query: str, docs: list[str], top_k: int) -> list: ...
```

### 2.3 `shared/obs/` -- 可观测底座

对齐 agent-service 五层可观测模型与 trace 双存储（[整体技术选型与模块划分](../整体技术选型与模块划分.md) §3.5.2）。rag-service 复用同一套，仅指标前缀为 `rag_`。

| 组件 | 职责 |
|------|------|
| `ObservabilityContext` | 不可变 dataclass，随会话流动（`session_id`/`trace_id`/`tenant`/`route`/`prompt_version`/`step_no`） |
| `ObservabilityPort` | 抽象接口，业务节点依赖它而非 OTel/prometheus 具体实现（DIP） |
| `Tracing` | OTel span 封装（`session_span`/`retrieval_span`/`projection_span`/`llm_span`） |
| `MetricsCollector` | Counter/Histogram 集中定义，统一前缀 `rag_` |
| `LoggingContext` | structlog + JSONRenderer，自动注入 `trace_id`/`span_id` |
| `Redactor` | 脱敏纯函数（序列号保留前 4 后 2、物料批次白名单、PII 不采集） |

> 指标清单见 §9.1。观测是只读旁路：失败不反噬业务（图库崩返回 503，不影响 MES 过点）。

### 2.4 `shared/config/` -- 统一配置

```python
# app/shared/config/base.py
class BaseSettings(pydantic_settings.BaseSettings):
    """公共配置项，环境变量前缀 RAG_。"""
    model_config = SettingsConfigDict(env_prefix="RAG_", env_nested_delimiter="__")
    llm: LlmSettings
    embedding: EmbeddingSettings
    mysql: MysqlSettings
    redis: RedisSettings
    kafka: KafkaSettings
    otel: OtelSettings

# app/shared/config/rag_settings.py
class RagSettings(BaseSettings):
    """聚合各路线子配置 + 路线级开关。"""
    traceability: TraceSettings      # rag.traceability.enabled
    document: DocSettings            # rag.document.enabled（含 chroma_persist_dir）
    agentic: AgenticSettings         # rag.agentic.enabled
```

> 路线级开关控制 router 注册与 consumer 启停（§4.2），支持灰度引入：先 B 再 A，E 收口。

### 2.5 `shared/kafka/` -- 事件消费基类

| 组件 | 职责 |
|------|------|
| `DomainEvent` | 领域事件 envelope 公共定义（`event_id`/`event_type`/`event_version`/`occurred_at`/`source_service`/`trace_id`/`partition_key`），对齐 [消息处理实现说明](../实现说明/业务事件/消息处理实现说明.md) §4.3 |
| `ConsumerGroup` | aiokafka 消费者基类：手动 ack + 位点落 MySQL + 消费者组按主题前缀分组 |
| `IdempotencyRepo` | `event_id` 幂等表操作基类（A/B 共用，落 MySQL） |
| `OffsetRepo` | 消费者位点表操作基类（落 MySQL） |
| `ProjectionHandler`（Protocol） | 事件 -> 投影 handler 协议（ISP），A 图投影 / B 重索引都实现它 |

> 现状：A/B 各自实现 `consumer_group.py`/`listeners.py`/`idempotency_repo.py`/`offset_repo.py`，逻辑高度同构。本节上移基类（幂等/位点表落 MySQL），各路线只保留自己的 `handlers/`（事件 -> 投影动作映射）。

### 2.6 `shared/acl/` -- ACL 基类 + 路线间 Port/Adapter（可拆性核心）

**(a) 对 MES 只读 REST 的 ACL（出站）**

```python
# app/shared/acl/base_client.py
class BaseReadonlyAclClient:
    """httpx 异步基类：自动注入 traceparent / 超时重试 / 租户 header / 只读断言。"""
    _WRITE_VERBS = {"create", "update", "delete", "post", "put", "patch", "remove", "save"}
    def __init__(self, client: httpx.AsyncClient, base_url: str, tenant_propagator): ...
    # 方法名禁止写动词--由 ReadOnlyAclGate 在启动期扫描（§3.2）
```

**(b) 路线间调用的 Port/Adapter（可拆性关键）**

```python
# app/shared/acl/ports.py
class TraceRagPort(Protocol):
    async def query(self, req: "TraceQuery") -> "TraceAnswer": ...
    async def expand(self, req: "ExpandRequest") -> "TraceSubgraph": ...

class DocRagPort(Protocol):
    async def query(self, req: "DocQuery") -> "DocAnswer": ...
    async def search(self, req: "DocSearch") -> list: ...
```

```python
# app/shared/acl/adapters.py
class InProcessTraceRagAdapter(TraceRagPort):
    """单服务内：直调 A 的 TraceRetrievalService（决策 #4）。"""
    def __init__(self, svc: "TraceRetrievalService"): self._svc = svc
    async def query(self, req): return await self._svc.retrieve_and_synthesize(req)

class HttpTraceRagAdapter(TraceRagPort):
    """拆服务后：httpx -> rag-service A，业务代码零改动。"""
    def __init__(self, client: httpx.AsyncClient, base_url: str): ...
```

> 规则：路线间**禁止直接 import 对方的 application/domain**，一律依赖 `shared/acl/` 的 Port。单服务模式下 DI 容器注入 InProcess Adapter；未来拆服务只需把绑定换成 Http Adapter。这把"现在单服务、将来可拆"从口号变成结构属性。

### 2.7 `shared/persistence/` -- 多存储 Engine 工厂 + 迁移

```python
# app/shared/persistence/db.py
class DbEngines:
    """按 config 懒初始化，连接池分别配额。"""
    async def mysql(self) -> AsyncEngine: ...   # asyncmy，shared/A/E/B(幂等/位点/审计)
    async def neo4j(self) -> AsyncDriver: ...    # A
    async def chroma(self) -> "chromadb.api.Client": ...  # B 嵌入式 persistent client
    async def redis(self) -> Redis: ...          # shared/A/B/E
```

> 多 DB 注意：单进程同时持有 Neo4j driver + ChromaDB client + MySQL asyncmy + Redis client，连接池分别配额，lifespan 启动期做就绪探测，任一不可用按路线降级（§3.3）。ChromaDB 嵌入式跟随进程，Parquet 持久化到挂卷，无独立 service。Alembic 多库配置见 §5（仅 MySQL）。

### 2.8 `shared/tenant/` -- 租户上下文 + 跨服务传递

| 组件 | 职责 |
|------|------|
| `TenantContext` | 租户上下文（`tenant_id`/`scopes`: workshop/line 列表） |
| `dependency` | FastAPI 依赖：`tenant_from_token`（JWT）/ `tenant_from_header`（`X-Tenant-Scope`） |
| `propagation` | 跨服务传递协议：出站 httpx 自动注入 `X-Tenant-Scope`；Kafka 消费时从事件 envelope metadata 还原 |

> 现状缺口：各路线各自定义 `TenantContext` 与过滤逻辑（A 用 Cypher `WHERE`、B 用 ChromaDB `where`），但**未定义跨服务传递协议**。本文统一：A/B/E 共用一个 `TenantContext`，传递协议在 `shared/tenant/propagation.py` 一处定义。

### 2.9 `shared/web/` -- FastAPI 公共底座

```python
# app/shared/web/lifespan.py
@asynccontextmanager
async def lifespan(app: FastAPI, settings: RagSettings, container: "Container"):
    # 1. 启动断言（只读红线，§3）
    await run_startup_assertions(settings, container)
    # 2. 存储就绪探测（按路线降级，§3.3）
    await probe_storages(settings, container)
    # 3. 按路线开关启停 consumer / router
    await start_consumers(settings, container)
    yield
    await shutdown(container)
```

```python
# app/shared/web/container.py
class Container:
    """DI 容器：注册 LLM/Embedding/各 Port 的 Adapter 绑定。"""
    def __init__(self, settings: RagSettings):
        self.obs = ObservabilityContext(...)
        self.llm = llm_factory(settings.llm, self.obs)
        self.embedding = BgeClient(settings.embedding)
        # Port -> InProcess Adapter 绑定（拆服务时换 Http Adapter）
        self.trace_rag: TraceRagPort = InProcessTraceRagAdapter(self.trace_svc)
        self.doc_rag: DocRagPort = InProcessDocRagAdapter(self.doc_svc)
```

> `deps.py` 从容器取实例，FastAPI 路由用 `Depends(get_trace_service)` 注入。

### 2.10 `shared/events/` -- 版本契约

| 组件 | 职责 |
|------|------|
| `version_contract` | `route_version`/`bom_version`/`rule_version` 三类版本锚点定义；`ProcessRouteActivated` 驱动的版本失效事件 -> A 重投图 / B 重索引的统一入口 |

> 版本一致性三段传递链（核心安全契约）：图 `SNAPSHOT_OF_ROUTE{route_version}` -> L1 `evidence.route_version` -> L2 `Draft.route_version` -> MES 应用服务校验 ACTIVE。rag-service 侧负责第一段（图用快照边物理锁定版本）+ 发布 `rag.reindex.request` 内部事件通知 B（§8.1）。B 侧 chunk 不可变，版本隔离靠查询带 `route_version` 过滤。

---

## 3. 只读红线：ReadOnly\*Gate 启动断言实现

### 3.1 Gate 体系总览

承接 [整体结构设计 §7](rag-service-整体结构设计.md#7-启动断言与只读红线)。统一的 `ReadOnly*Gate` 体系在 `shared/web/lifespan` 启动期扫描，任一失败即拒绝启动（fail-fast）。

| Gate | 归属 | 断言内容 | 实现方式 |
|------|------|---------|---------|
| `ReadOnlyProjectionGate` | A | 图投影 handler 禁止 `DELETE`/`REMOVE`/历史覆盖性 `SET` | AST 扫描 Cypher 语句构造点 |
| `RawDataTopicGate` | A | 消费者组禁止订阅 `dc.*` 原始数据流（高频采集不全量入图） | 订阅拓扑配置扫描 |
| `ReadOnlyIngestionGate` | B | 摄入/重索引 handler 禁止任何写 MES 调用 | handler 依赖的 ACL client 方法名扫描 |
| `ReadOnlyToolGate` | E | `ToolRegistry` 拒绝注册 `read_only=False` 的工具 | 注册期断言 |
| `ReadOnlyAclGate` | shared | 所有 ACL client 方法名禁止写动词 | `BaseReadonlyAclClient` 子类方法名扫描 |

### 3.2 实现骨架

```python
# app/shared/acl/gates.py
class StartupAssertionError(RuntimeError): ...   # 拒绝启动

class ReadOnlyAclGate:
    """扫描所有 BaseReadonlyAclClient 子类，方法名禁止写动词。"""
    VERBS = {"create", "update", "delete", "post", "put", "patch", "remove", "save", "insert"}
    def assert_readonly(self, clients: list) -> None:
        for c in clients:
            for name in dir(type(c)):
                if any(name.startswith(v) or f"_{v}" in name for v in self.VERBS):
                    raise StartupAssertionError(f"写动词方法名 {name} 出现在只读 ACL {type(c).__name__}")
```

```python
# app/routes/traceability/domain/gates.py
class RawDataTopicGate:
    FORBIDDEN_PREFIX = "dc."        # 原始数据流
    def assert_no_raw_topic(self, subscriptions: list[str]) -> None:
        for t in subscriptions:
            if t.startswith(self.FORBIDDEN_PREFIX):
                raise StartupAssertionError(f"禁止订阅原始数据流 {t}（高频采集不全量入图）")
```

### 3.3 启动期就绪探测与按路线降级

```python
async def probe_storages(settings, container):
    """逐存储探测，不可用的路线标记降级，不拖垮其他路线。"""
    deps = {}
    if settings.traceability.enabled:
        deps["neo4j"] = await container.engines.neo4j().verify_connectivity()
    if settings.document.enabled:
        deps["chroma"] = container.engines.chroma().heartbeat()   # 嵌入式本地，基本不会失败
    deps["mysql"] = await container.engines.mysql().connect()
    deps["redis"] = await container.engines.redis().ping()
    # 不可用项落 /ready，对应路线降级（返回 503）
```

> 启动断言是把"只读旁路"从约定变成结构属性：最坏情况是"没检索出来"，不会产生写副作用。

---

## 4. 单服务骨架与启动编排

### 4.1 `main.py` + lifespan 编排

```python
# app/main.py
def create_app(settings: RagSettings) -> FastAPI:
    container = Container(settings)
    app = FastAPI(lifespan=lambda a: lifespan(a, settings, container))
    app.add_middleware(TenantMiddleware, propagator=container.tenant_propagator)
    app.add_middleware(RequestLogMiddleware, obs=container.obs)
    register_routers(app, settings, container)         # §4.2
    register_exception_handlers(app)                   # shared/api/errors.py
    return app
```

### 4.2 `register_routers` + 路线级开关

```python
# app/api/__init__.py
def register_routers(app, settings, container):
    if settings.document.enabled:      app.include_router(doc_router(container))
    if settings.traceability.enabled:  app.include_router(trace_router(container))
    if settings.agentic.enabled:       app.include_router(chat_router(container))
    app.include_router(health_router(container))       # /health /ready /metrics 始终注册
```

> 灰度引入顺序：`document.enabled=true` 先行，`traceability`/`agentic` 灰度打开（§7）。

### 4.3 健康检查

| 端点 | 内容 |
|------|------|
| `GET /health` | 进程存活（K8s liveness） |
| `GET /ready` | Neo4j/ChromaDB/MySQL/Redis/Kafka 连通性 + 各 consumer 组位点滞后度（K8s readiness） |
| `GET /metrics` | prometheus 指标（`rag_` 前缀） |

> 现状缺口：三路线均无健康检查端点。本文一次性补齐。

### 4.4 DI 容器绑定

DI 容器（§2.9）集中注册 Port -> Adapter 绑定。单服务内全部为 InProcess Adapter；拆服务时仅改容器绑定，业务代码零改动（§8.1）。

---

## 5. 数据库迁移与 Schema 管理（Alembic）

### 5.1 多 schema 分配

| schema | 归属 | 库 | 内容 |
|--------|------|----|----|
| `rag_shared` | shared | MySQL | `DomainEvent` 幂等/位点基表（A/B 共用） |
| `rag_trace` | A | MySQL | `index_idempotency`/`index_offset`/`subgraph_audit` |
| `rag_doc` | B | MySQL | B 的幂等/位点归 `rag_shared`；治理/审计聚合导出表（chunk 向量在 ChromaDB，不在 MySQL） |
| `rag_agentic` | E | MySQL | `answer_audit`/`route_trace` |

> B 的向量与 chunk metadata 在 ChromaDB collection（非 Alembic，代码初始化）；B 的幂等/位点/审计在 MySQL `rag_shared`，治理聚合导出表在 `rag_doc`（MySQL）。

### 5.2 Alembic 配置（仅 MySQL）

```python
# alembic/env.py 关键：单 mysql_engine 覆盖 rag_shared/rag_trace/rag_doc/rag_agentic
mysql_engine = create_async_engine(settings.mysql.dsn, module=asyncmy)   # asyncmy 方言

def run_migrations_online():
    # 所有 schema 走 mysql_engine；Neo4j 用 SchemaInitializer，ChromaDB 用 collection 代码初始化
```

> Alembic 迁移脚本按 `rag_<schema>/` 前缀分目录，CI 校验"模型与迁移一致 + 迁移可回滚"。

### 5.3 Neo4j 与 ChromaDB（非 Alembic）

```python
# app/routes/traceability/infrastructure/neo4j/schema_initializer.py
class SchemaInitializer:
    """启动时幂等执行约束/索引/向量索引 DDL（非 Alembic，图库 DDL 幂等即可）。"""
    async def ensure(self, driver): ...   # CONSTRAINT FOR (n:Unit) REQUIRE n.serial IS UNIQUE
                                          # CREATE VECTOR INDEX defect_catalog_vec ...

# app/routes/document/infrastructure/chromadb/schema.py
class ChromaCollectionInitializer:
    """启动时幂等创建/获取 collection（非 Alembic，chunk 不可变无 schema 演进）。"""
    def ensure(self, client) -> Collection:
        return client.get_or_create_collection(
            name="doc_chunks",
            metadata={"hnsw:space": "cosine"},
            # metadata 字段：route_version/state/tenant_scope/doc_id/doc_type/chunk_seq/locator
        )
```

### 5.4 迁移脚本规约

- 命名：`<schema>_<yyyymmdd>_<seq>_<slug>.py`（如 `rag_doc_20260801_001_audit_export.py`）。
- 每个脚本须含 `upgrade()` + `downgrade()`，CI 校验 downgrade 可执行。
- 破坏性变更（删列/改类型）须两阶段：先加新列 -> 双写 -> 切读 -> 删旧列。

---

## 6. 从"三独立服务文档"到"单服务模块"的迁移

> 整体结构设计 §11 把此项列为"待办"。本节给出可执行迁移映射与同步修订清单。

### 6.1 迁移映射表

| 各路线文档原位置 | 本文档新位置 | 动作 |
|----------------|------------|------|
| `rag_service/app/api/trace_router.py` 等 | `app/api/v1/trace_router.py` | 路由层上移到统一 `api/v1/` |
| 各路线 `app/application/` `app/domain/` | `app/routes/<route>/application/` `domain/` | 原样下沉到路线模块 |
| 各路线 `infrastructure/ai/llm_factory.py` | `app/shared/ai/llm_factory.py` | **上移合并**，删本地副本 |
| 各路线 `infrastructure/embedding/bge_client.py` | `app/shared/embedding/bge_client.py` | **上移合并** |
| 各路线 `infrastructure/obs/` | `app/shared/obs/` | **上移合并** |
| 各路线 `config.py` | `app/shared/config/`（公共）+ 路线子配置 | **拆分合并** |
| 各路线 `infrastructure/kafka/consumer_group.py`/`idempotency_repo.py`/`offset_repo.py` | `app/shared/kafka/` | **上移基类**，各路线只留 `handlers/` |
| 各路线 `infrastructure/acl/`（对 MES） | `app/routes/<route>/infrastructure/acl/`（路线专属）+ `app/shared/acl/MesClients`（公共） | 公共部分上移 |
| 路线间互调的 httpx 客户端 | `app/shared/acl/` Port + InProcess/Http Adapter | **重构为 Port/Adapter** |
| B 的 `infrastructure/pgvector/` | `app/routes/document/infrastructure/chromadb/` | **PGVector -> ChromaDB**（client/schema/retriever/document_repo） |
| 各路线 `main.py` | `app/main.py`（统一）+ `app/shared/web/lifespan` | **合并**，加健康检查/DI/router 注册 |

### 6.2 上移合并清单（from -> to）

| 组件 | A 当前 | B 当前 | E 当前 | 上移到 |
|------|--------|--------|--------|--------|
| `llm_factory.py` | `infrastructure/ai/` | `infrastructure/ai/` | `infrastructure/ai/` | `shared/ai/` |
| `bge_client.py` | `infrastructure/embedding/` | `infrastructure/embedding/` | （无） | `shared/embedding/` |
| `reranker.py` | （无） | `infrastructure/ai/` | （无） | `shared/embedding/` |
| `obs/`（tracing/metrics/logging/redactor） | `infrastructure/obs/` | `infrastructure/obs/` | `infrastructure/obs/` | `shared/obs/` |
| `config.py`（Settings） | `app/config.py` | `app/config.py` | `app/config.py` | `shared/config/`（公共）+ 路线子配置 |
| `consumer_group.py` | `infrastructure/kafka/` | `infrastructure/kafka/` | （无） | `shared/kafka/` |
| `idempotency_repo.py`/`offset_repo.py` | `infrastructure/persistence/` | `infrastructure/persistence/` | （无） | `shared/kafka/`（落 MySQL） |
| `TenantContext` | `domain/tenant.py` | `domain/tenant.py` | `domain/tenant.py` | `shared/tenant/` |
| `models.py`（幂等/位点基表） | `infrastructure/persistence/` | `infrastructure/persistence/` | `infrastructure/persistence/` | `shared/persistence/base.py`（MySQL） |
| 对 MES 只读 ACL（公共部分） | `infrastructure/acl/` | `infrastructure/acl/` | （无） | `shared/acl/MesClients` |

**各路线独有、不上移**（保留在 `routes/<route>/infrastructure/`）：
- A：`neo4j/`（driver/schema/retriever/projections）、`rag/graph_index.py`
- B：`chromadb/`（client/schema/retriever/document_repo）、`minio_/object_store.py`、`rag/index.py`
- E：`ai/route_graph_builder.py`（LangGraph 路由图）

### 6.3 路线文档同步修订清单

> 这是整体结构设计 §11 列为"待办"的具体化。每项需在对应路线文档落地，否则文档间矛盾。

| # | 路线文档 | 修订内容 | 关联决策 |
|---|---------|---------|---------|
| 1 | A/B/E 三份实现方案 §8（包结构） | 包结构从"独立服务根"（`rag_service/`/`rag_doc_service/`/`agent_gateway_service/`）投影到 `app/routes/<route>/`；补一句"包结构投影到 rag-service 整体结构设计 §2/§11，`llm_factory`/`embedding`/`obs`/`config`/`kafka` 基类见 §3" | 整体结构设计 §11 |
| 2 | E 实现方案 §2.1 + E 详细设计 §2 | LangGraph 版本统一回填为 **≥0.2**（解决 0.1+ vs ≥0.2 漂移） | 本文 §1.2 |
| 3 | **B 详细设计 §4.3 + B 实现方案 §4.3/§5.4/§9.2** | 工艺绑定型文档（SOP/检验标准）审核流从"独立人工审核（DRAFT->SUBMITTED->PUBLISHED + PENDING_REBIND）"改为"**联动 PUBLISHED**"：`ProcessRouteActivated` 直接置 PUBLISHED，去掉 SUBMITTED 人工确认中间态与 PENDING_REBIND；责任归工艺 owner | 决策 #3 |
| 4 | B 实现方案（`DocumentBinding` schema） | 预留 `rule_id`+`rule_version` 双轨字段，订阅 `quality.gate.lifecycle`；MVP 按 `route_version`，评测后切换 | 决策 #2 |
| 5 | E 实现方案 §4.3 / §10.2 | E 委托 L1 的 `traceparent` 透传：注明 L1 `main.py` 挂 `opentelemetry-instrumentation-fastapi` 为硬要求 | 决策 #1 |
| 6 | E 实现方案 §6/§7（Port 调用） | E 调 A/B 走 InProcess Adapter（直调 application service），不走本机 REST | 决策 #4 |
| 7 | **B 详细设计 §2.3/§4.2/§5.3/§6.2 + B 实现方案 §2.3/§2.5/§4.2/§5.3/§5.4/§6.2/§9.9** | 向量库 PGVector -> **ChromaDB**：chunk 不可变 + 强制带版本 + 删除 sync_chunk_state 批量翻转 + 检索改 ChromaDB `where` + 依赖/DDL/compose 改 ChromaDB（详见 B 文档已由专项修订完成） | 本文 §1.2 |

### 6.4 迁移顺序与回归点

1. **先建 shared 骨架**（§2 + §4），三路线文档暂不动。
2. **逐路线投影**：B -> A -> E。每条路线投影后跑该路线评测集回归（mes-eval `EvalTarget`），确认行为不变。
3. **每条路线投影完成即补 §6.3 对应修订项**，避免文档与现实脱节。
4. **决策 #3 修订**（B 审核流）作为 B 路线投影的**子任务**，与代码同步落地，不延后。
5. **B 向量库 ChromaDB 改造**（§6.3 #7）作为 B 路线投影的**子任务**，chunk 不可变设计与决策 #3 解耦（决策 #3 改 version 层状态机，ChromaDB 改存储层 chunk 不可变，两者独立可并行）。

---

## 7. 分阶段交付计划

> 引入顺序承接 [整体结构设计 §8](rag-service-整体结构设计.md#8-部署与运行时) 与 [RAG服务引入路线](RAG服务引入路线.md) §3：**先 B 后 A，E 收口**。每阶段含交付物 / 依赖 / 退出标准 / 周数估算（规划值）。

### 7.1 阶段总览

| 阶段 | 内容 | 路线开关 | 周数（估） |
|------|------|---------|-----------|
| 一 | 共享内核 + 单服务骨架 + B 文档型 | `document.enabled=true` | 4-6 |
| 二 | A 追溯型（GraphProjector + 4 上下文 MVP） | + `traceability.enabled=true` | 6-8 |
| 三 | E Agentic 收口 | + `agentic.enabled=true` | 3-4 |
| 四 | 可观测 / 评测 / 运维收口 | 全开 | 2-3 |

### 7.2 阶段一：共享内核 + 单服务骨架 + B 文档型

- **交付物**：`shared/`（ai/embedding/obs/config/kafka/acl/persistence/tenant/web/events）骨架 + `main.py`/lifespan/健康检查/DI 容器；B 路线投影到 `routes/document/`（含 ChromaDB 嵌入式 collection、chunk 不可变、强制带版本、决策 #3 联动 PUBLISHED、决策 #2 双轨字段）；Alembic 管 `rag_shared`/`rag_doc`（MySQL）；`/rag/docs/{query,search,ingest}` 端点；`ReadOnlyIngestionGate` + `ReadOnlyAclGate` 启动断言；MinIO 原始文件 + ChromaDB Parquet 备份。
- **依赖**：MinIO、MySQL、Redis、Kafka、bge-inference 就绪；B 实现方案 §11 阶段一交付物。
- **退出标准**：B 评测集（`mes_eval/infrastructure/targets/doc_rag.py`）通过；版本过滤（`route_version` 强制带）金标准用例全绿；`/ready` 报 ChromaDB/MySQL/Redis/Kafka 全通；只读断言在启动期生效；ChromaDB 从 MinIO 重建演练通过。

### 7.3 阶段二：A 追溯型

- **交付物**：A 路线投影到 `routes/traceability/`（含 `neo4j/`/`projections/`/`acl/`）；`GraphProjector` 订阅 MVP 4 上下文事件（`mes.checkpoint.lifecycle`/`mes.testresult.structured`/`mes.routing.progress`/`process.route.lifecycle`/`material.*`/`quality.*`）；`/rag/trace/{query,expand}` 端点；`ReadOnlyProjectionGate` + `RawDataTopicGate`；`rag.reindex.request` 内部事件发布给 B。
- **依赖**：阶段一的 shared 内核；Neo4j 5.20；A 实现方案 §13 阶段一-三交付物。
- **退出标准**：A 评测集（`traceability_rag.py`）5M1E 召回 + 证据回溯 + 版本锚点全绿；图投影幂等测试通过；Neo4j 不可用时 A 降级 503、不拖垮 B。

### 7.4 阶段三：E Agentic 收口

- **交付物**：E 路线投影到 `routes/agentic/`；LangGraph 轻量路由图（`recursion_limit=6`，3 意图路由）；`/agent/chat` + `/agent/explain/{id}`；`TraceRagPort`/`DocRagPort` InProcess Adapter 绑定；L1/L2 委托 httpx（透传 `traceparent`，决策 #1）；`ReadOnlyToolGate`。
- **依赖**：阶段一/二的 A/B 成型；agent-service L1/L2 可用。
- **退出标准**：E 评测集（`agentic_rag.py`）路由准确率 + 工具链正确性达标；traceparent 全链路（E -> L1 -> A/B/MES）在 Tempo 可见同一 `trace_id`。

### 7.5 阶段四：可观测 / 评测 / 运维收口

- **交付物**：`rag_` 指标全量埋点 + Grafana 看板；trace 双存储（Tempo + MySQL 平铺表）；三 `EvalTarget` 接入 CI 门禁（安全红线硬门禁 + 质量软门禁）；HPA（A/B/E 合一指标）；ChromaDB 定期备份 + 重建演练纳入运维 SOP。
- **退出标准**：SLO 看板上线；CI 评测门禁对三路线生效；可拆性演练（把 `DocRagPort` 绑定换 `HttpDocRagAdapter`，业务代码零改动跑通）。

---

## 8. 关键集成点实现

### 8.1 路线间调用（进程内 Port/Adapter）

| 调用方 -> 被调方 | Port | 单服务内路径 | 说明 |
|-----------------|------|------------|------|
| A -> B | `DocRagPort` | `TraceRetrievalService` -> `InProcessDocRagAdapter` -> `DocumentRetrievalService` | A 的 `suggested_action` 拉 SOP 片段，带 `route_version_filter` |
| A -> B（事件） | `rag.reindex.request` | A 发布 -> B 的 `ReindexCoordinator` 消费 | 工艺升版触发 B 重索引 |
| E -> A/B | `TraceRagPort`/`DocRagPort` | `GatewayService` -> InProcess Adapter -> 各路线 service | E 不自己多步推理，`recursion_limit=6` |

> 拆服务时：把 DI 容器里 Port -> InProcess Adapter 的绑定换成 Port -> Http Adapter，业务代码零改动。**可拆性演练**列入阶段四退出标准。

### 8.2 与 agent-service 的集成（traceparent 全链路，决策 #1）

| 集成点 | 方向 | 契约 |
|--------|------|------|
| L1 调图 | Agent -> rag-service (A) | `query_traceability_graph` 工具封装 `POST /rag/trace/query`，注册在 L1 ToolRegistry 首位 |
| L2 回查图 | Agent -> rag-service (A) | `fetch_subgraph_nodes(subgraph_ref)` -> `POST /rag/trace/expand` |
| L1/L2 调文档 | Agent -> rag-service (B) | `search_docs(query, route_version_filter)` -> `POST /rag/docs/query` |
| E 委托 L1/L2 | rag-service (E) -> agent-service | `POST /agent/diagnose`（60s）、`POST /agent/draft`（30s），透传 `traceparent` |

> **traceparent 全链路**：E 委托 L1 时手动注入 `traceparent`；L1 `main.py` 挂 `opentelemetry-instrumentation-fastapi` 为硬要求，接收 incoming `traceparent` 并续接 trace，出站 httpx instrumentation 自动透传到 A/B/MES。rag-service 侧 `shared/acl/base_client.py` 出站自动注入 `traceparent`。

### 8.3 与 MES 的集成（只读）

- **Kafka 只读事件**：`GraphProjector`(A) / `ReindexCoordinator`(B) 订阅各上下文 Outbox 事件，幂等消费（`event_id`）+ 位点落 MySQL。
- **只读 REST 降级**：图投影滞后或需聚合计算时，A 经 `MesClients` 调各上下文只读 REST 补齐；B 检索时若调用方仅有 `route_id` 无 `route_version`，经 ACL 查该 route 当前 ACTIVE 版本带入（仅"查当前生效"场景，历史回溯必须带具体版本）。
- **单向只读**：rag-service 从不回写 MES；图库/向量库崩返回 503 不阻塞生产。

### 8.4 subgraph_ref 与版本一致性传递链

```
图 SNAPSHOT_OF_ROUTE{route_version}（A，物理锁定版本）
  -> L1 evidence.route_version（透传）
  -> L2 Draft.route_version（锁定）
  -> MES 应用服务校验 ACTIVE（最后一道）
```

rag-service 侧：A 用快照边把版本一致性变成结构属性；A 升版发 `rag.reindex.request` 通知 B 重索引；B chunk 不可变，查询带 `route_version` 过滤（决策 #2，MVP 按 `route_version`，评测后切 `rule_version`）。

---

## 9. 可观测性与兜底

### 9.1 指标清单（`rag_` 前缀，按归属分类）

| 归属 | 指标 | 类型 |
|------|------|------|
| A | `rag_trace_query_duration_seconds` | Histogram |
| A | `rag_projection_lag_ms` | Histogram（事件 -> 入图滞后） |
| A | `rag_projection_events_total` | Counter（按上下文/动作） |
| B | `rag_doc_search_hits` | Histogram（top-k 命中数） |
| B | `rag_doc_ingest_chunks_total` | Counter |
| B | `rag_doc_reindex_lag_ms` | Histogram |
| B | `rag_doc_chroma_query_duration_seconds` | Histogram（ChromaDB 检索延迟） |
| E | `rag_agent_route_total` | Counter（按意图） |
| E | `rag_agent_delegation_duration_seconds` | Histogram（L1/L2 委托） |
| shared | `rag_llm_call_duration_seconds` | Histogram（按 model/prompt_version） |
| shared | `rag_llm_tokens_total` | Counter（按 model） |
| shared | `rag_kafka_consumer_lag` | Gauge（按消费者组） |
| shared | `rag_storage_health` | Gauge（按 DB/路线：neo4j/chroma/mysql/redis，0/1） |

### 9.2 trace 双存储 + traceparent

- **Tempo / Jaeger**：SRE 火焰图，`session_span`/`retrieval_span`/`projection_span`/`llm_span`。
- **MySQL 平铺表**：工程师 UI 证据链回溯，同源 `trace_id` 串联 agent-service 与 MES。
- **traceparent**：跨服务（rag-service -> agent-service -> MES）全链路，决策 #1。

### 9.3 兜底

- **存储故障域隔离**：Neo4j/ChromaDB/MySQL/Redis 任一不可用，对应路线降级（返回 503），不拖垮其他路线（§3.3）。
- **ChromaDB 重建兜底**：ChromaDB 数据丢失/损坏时，从 MinIO 原始文件 + chunk 策略 + 事件回放重建向量库（chunk 不可变使重建幂等）。
- **低置信转人工**：与 MES 防错理念一致--宁可拦下让人判，不可错放。LLM 综合低置信度时返回"建议转人工/转规则引擎"，不硬答。
- **版本失效保护**：检索强制带 `route_version` 过滤（入口校验），物理杜绝失效工艺泄漏（安全红线，评测 CI 硬门禁）。

---

## 10. 测试策略

### 10.1 单元测试

- `shared/` 抽象：`LlmPort`/`EmbeddingPort`/Port/Adapter 的 InProcess 路径、`Redactor` 脱敏纯函数。
- `ReadOnly*Gate` 断言：构造违规 handler / 方法名 / 订阅拓扑，断言启动期 `StartupAssertionError`。
- B 的 chunk 不可变：断言升版后老 chunk metadata 不变、新 chunk 追加、查询带版本隔离正确。
- 版本契约：`version_contract` 锚点解析。

### 10.2 集成测试

- **Port/Adapter InProcess**：A -> B（`DocRagPort`）+ E -> A/B 全链路单服务内调用。
- **Kafka 投影幂等**：重复投递同一 `event_id`，断言图/索引不重复写入（A 实现方案 §12.1）。
- **多 DB 就绪**：lifespan 启动期就绪探测 + 按路线降级（拔 Neo4j，断言 A 返回 503、B 正常；拔 ChromaDB，断言 B 返回 503、A 正常）。
- **ChromaDB 重建**：删 ChromaDB 持久化目录，从 MinIO 重建，断言检索结果一致。
- **版本一致性**：工艺升版后图快照边更新 + `rag.reindex.request` 触发 B 重索引（A 实现方案 §12.2）；B 侧老 chunk 不变、新版本 chunk 追加、查询带版本隔离。

### 10.3 契约测试

- **对外端点契约**：`/rag/trace/query`、`/rag/docs/query`、`/agent/chat` 的 schema 与 agent-service L1/L2 工具封装（`query_traceability_graph`/`search_docs`/`fetch_subgraph_nodes`）对齐。
- **traceparent 契约**：E 委托 L1 的 header 注入、L1 续接 trace 的端到端断言。
- **强制带版本契约**：B 检索入口 `route_version` 缺失时拒绝（工艺绑定型），断言不退回"查最新"。

### 10.4 评测接入（mes-eval）

| 被测对象 | 适配器 | 评测入口 | 断言 |
|---------|--------|---------|------|
| A 追溯型 | `traceability_rag.py` | `POST /rag/trace/query` | 5M1E 召回 + 证据回溯 + 版本锚点 |
| B 文档型 | `doc_rag.py` | `POST /rag/docs/query` | 忠实度/答案相关性 + 版本过滤（强制带 `route_version`） |
| E Agentic | `agentic_rag.py` | `POST /agent/chat` | 路由准确率 + 工具链正确性 |

> 版本锚定贯穿评测全程：每条金标准用例钉死 `route_version`/`bom_version`/`rule_version`，`VersionAnchorChecker` 强制比对。安全红线（失效工艺泄漏/写越界/租户越权/PII/实体幻觉/证据空）任一非 0 阻断 CI。

---

## 11. 风险与对策

| # | 风险 | 影响 | 对策 | 关联 |
|---|------|------|------|------|
| 1 | 只读红线被绕过（路线间直 import 或 ACL 写动词） | 产生写副作用，违背"只读旁路" | `ReadOnly*Gate` 启动断言 + CI 静态检查禁止路线间直 import | §3 / 决策 #4 |
| 2 | 多 DB 任一故障拖垮全服务 | 单点故障扩散 | 按路线降级（§3.3），故障域 Port 隔离 | 决策 #3 |
| 3 | 迁移期路线文档与代码不一致 | 文档间矛盾、新人误读 | §6.3 同步修订清单逐项落地，决策 #3 不延后 | §6 |
| 4 | bge 本地化推理资源不足（车间网 GPU/内存） | Embedding 延迟飙升 | bge-inference sidecar 独立配额；批量推理；Redis 缓存向量 | §1.4 |
| 5 | 三路线版本漂移再次发生 | 依赖冲突、行为不一致 | 选型以本文 §1.1 为权威，路线文档改动需同步本文 | §1.2 |
| 6 | 可拆性退化（路线间偷偷直 import） | 未来拆服务需大改 | 阶段四可拆性演练（换 Http Adapter 跑通）+ CI 禁止直 import | §7.5 / §8.1 |
| 7 | 图投影滞后导致追溯不准 | 5M1E 召回不全 | `rag_projection_lag_ms` 指标告警 + MES 只读 REST 降级补齐 | §8.3 / §9.1 |
| 8 | 决策 #3 联动 PUBLISHED 误发未审文档 | 未审 SOP 随工艺生效 | 责任归工艺 owner（工艺 owner 即文档 owner）；通用知识型/设备绑定型仍走独立 DRAFT->PUBLISHED | 决策 #3 |
| 9 | **ChromaDB 数据丢失/损坏（无 PITR）** | B 检索不可用 | MinIO 原始文件 + chunk 策略 + 事件回放重建（chunk 不可变使重建幂等）；Parquet 定期备份 | §1.2 / §9.3 |
| 10 | **ChromaDB 单写者并发（重索引阻塞查询）** | 重索引期间 B 检索超时 | 文档量小、重索引量小可接受；重索引走异步 consumer + 查询侧超时降级 | §1.2 |
| 11 | **强制带版本被绕过（调用方不带 route_version 退回查最新）** | 答出不适用工艺 SOP | 入口校验拒绝 + 契约测试 + 评测安全红线 | §1.2 / §10.3 |

---

## 12. 约束落地检查清单（DoD）

> 承接三路线实现方案的检查清单，补共享内核 / 单服务 / 迁移 / ChromaDB 维度。

- [ ] 共享内核：`shared/` 下 ≥2 路线复用的设施全部上移，单路线专属设施留 `routes/<route>/infrastructure/`
- [ ] 路线间无直接 import 对方 application/domain，一律走 `shared/acl/` Port
- [ ] `ReadOnlyProjectionGate`/`RawDataTopicGate`/`ReadOnlyIngestionGate`/`ReadOnlyToolGate`/`ReadOnlyAclGate` 启动断言全部生效
- [ ] `main.py` + lifespan 编排：启动断言 -> 存储就绪探测 -> 按路线开关启停
- [ ] `/health`/`/ready`/`/metrics` 三端点就绪，`/ready` 含各 DB（neo4j/chroma/mysql/redis）连通性 + consumer 位点滞后
- [ ] DI 容器注册 Port -> InProcess Adapter 绑定，可拆性演练通过
- [ ] Alembic 管 MySQL 的 `rag_shared`/`rag_trace`/`rag_doc`/`rag_agentic`；Neo4j 用 `SchemaInitializer`，ChromaDB 用 collection 代码初始化
- [ ] `TenantContext` 全服务共用，跨服务传递协议在 `shared/tenant/propagation.py` 一处定义
- [ ] 路线级开关 `rag.<route>.enabled` 控制 router/consumer 启停，灰度顺序 B -> A -> E
- [ ] 多 DB 故障域隔离：任一 DB 不可用只降级对应路线，不拖垮其他
- [ ] **B 向量库 ChromaDB：chunk 不可变 + 强制带 `route_version`（入口校验）+ 检索 `where` pre-filter**
- [ ] **B 备份兜底：MinIO 留原始文件，ChromaDB 可重建（重建演练通过）+ Parquet 定期备份**
- [ ] traceparent 全链路：E -> L1 -> A/B/MES 同源 `trace_id` 在 Tempo 可见（决策 #1）
- [ ] 版本一致性三段传递链：图快照边 -> L1 evidence -> L2 Draft -> MES 校验 ACTIVE
- [ ] B 审核流联动 PUBLISHED 落地，去掉 SUBMITTED/PENDING_REBIND 中间态（决策 #3）
- [ ] B `DocumentBinding` 预留 `rule_id`+`rule_version` 双轨字段（决策 #2）
- [ ] LangGraph 版本统一 ≥0.2，路线文档回填一致
- [ ] 三路线文档补"投影到 §2/§11，基类见 §3"说明（§6.3 #1）
- [ ] mes-eval 三 `EvalTarget` 接入 CI，安全红线硬门禁 + 版本锚定强制比对
- [ ] 指标统一 `rag_` 前缀，Grafana 看板 + SLI/SLO 告警上线

---

## 13. 面试防守 Q&A

**Q：rag-service 为什么不拆成三个独立微服务？**
A：三路线基础设施 90% 同构--同一个 LLM 抽象、同一个 bge-m3、同一套 OTel/prometheus/structlog、同一套 Kafka envelope/幂等模式。拆三服务会把这 90% 复制三份，违背 DRY 与 SRP。我用"单服务 + 共享内核 + 路线间 Port/Adapter"：路线模块自包含，调用走 `shared/acl/` 的 Port，单服务内注入 InProcess Adapter，未来某路线 QPS/数据量单独增长时，把 Adapter 绑定换成 Http Adapter 即可拆出，业务代码零改动。可拆性是结构属性，不是口号。

**Q：rag-service 只读红线怎么保证？靠自觉吗？**
A：不靠自觉，靠启动断言兜底。统一的 `ReadOnly*Gate` 体系在 lifespan 启动期扫描：图投影 handler 禁止 `DELETE`/`REMOVE`、消费者组禁止订阅 `dc.*` 原始数据流、ACL client 方法名禁止写动词、E 的 ToolRegistry 拒绝注册非只读工具。任一失败即拒绝启动（fail-fast）。最坏情况是"没检索出来"，不会产生写副作用。

**Q：单进程里 Neo4j、ChromaDB、MySQL、Redis 共存，一个崩了会不会全拖垮？**
A：不会。每条存储链路在 `shared/persistence/` 与各路线 `infrastructure/` 间用 Port 隔离，lifespan 启动期做就绪探测，任一不可用按路线降级--Neo4j 崩只让 A 返回 503，ChromaDB 崩只让 B 返回 503，其他路线照常。故障域不扩散是硬约束，不是运维自觉。

**Q：B 文档型为什么选 ChromaDB 而不是 PGVector / Milvus？**
A：三个前提：车间 ToB 文档量小（数千文档/数十万 chunk 以内）、工艺路线查询强制带 `route_version`（版本过滤退化成等值，ChromaDB 的 `where` 能做且 pre-filter）、求开发简（嵌入式零额外服务、LlamaIndex 集成最成熟、少装一套 PG+pgvector+asyncpg）。核心是 chunk 不可变--写入后不改，工艺升版追加新版本 chunk 而非翻转老 chunk，查询带 `route_version` 天然隔离，直接绕开 ChromaDB 多记录翻转无事务的弱点。代价是 HA/备份弱，用 MinIO 原始文件可重建兜底。PGVector 的 SQL 过滤/同库事务优势在"等值版本过滤 + chunk 不可变"场景下不再决定性，ChromaDB 的简大于其弱。

**Q：ChromaDB 没有事务，工艺升版时老 SOP 状态翻转怎么办？**
A：不翻转。chunk 不可变--老版本 chunk 的 `route_version` 一直是 v3，写入后不改；工艺升版到 v4 时追加 v4 的新 chunk，查询带 `route_version=v3` 天然只召回 v3、带 v4 只召回 v4。ChromaDB 里根本没有"批量翻转"这个操作，多记录事务弱点直接消失。文档撤回是单条 upsert 改 `state`（单条原子，ChromaDB 能做），区别于"批量翻转"（不做）。

**Q：ChromaDB 数据丢了怎么办？没 PITR。**
A：可重建兜底。原始文档文件一直在 MinIO，chunk 切分策略是确定性的，重索引事件幂等（`event_id` + `doc_id+route_version+chunk_seq` 去重）--从 MinIO 原始文件 + chunk 策略 + 事件回放就能重建整个向量库。chunk 不可变让重建幂等（不会产生重复 chunk）。再加 Parquet 定期备份。车间文档量小，重建成本可接受。

**Q：迁移期怎么不破坏既有路线文档？**
A：三路线实现方案现在写成独立服务包结构，是"被取代的旧视角"。我按整体结构设计 §11 的迁移映射逐路线投影（B -> A -> E），每条路线投影后跑该路线评测集回归确认行为不变，并同步修订路线文档（补"投影到 §2/§11"、LangGraph 版本统一、决策 #3 联动 PUBLISHED、B 向量库 ChromaDB 改造）。决策 #3 与 ChromaDB 改造解耦--决策 #3 改 version 层状态机，ChromaDB 改存储层 chunk 不可变，两者独立可并行。

**Q：决策 #3 为什么把 B 审核流从"独立审核"反转为"联动 PUBLISHED"？**
A：工艺绑定型文档（SOP/检验标准）的生效边界应与工艺路线一致--工艺 `ProcessRouteActivated` 即文档生效，责任归工艺 owner。如果文档走独立人工审核，会出现"工艺已生效但 SOP 还在 SUBMITTED"的窗口，操作工可能拿到失效或未对齐的 SOP。联动 PUBLISHED 把这个窗口消除。通用知识型/设备绑定型文档仍走独立 DRAFT->PUBLISHED，因为它们不绑工艺版本。

---

## 14. 一句话定位

"rag-service 用单服务 + 共享内核承载 A/B/E 三路线，技术选型收敛到一张表解决三路线版本漂移；B 向量库选 ChromaDB（车间文档少 + 强制带 `route_version` + chunk 不可变绕开多记录事务弱点 + MinIO 重建兜底），落地按'共享内核先建 -> B -> A -> E 收口'灰度推进；只读红线靠 `ReadOnly*Gate` 启动断言兜底，多 DB 按 Port 隔离故障域，路线间走 Port/Adapter 保留零成本可拆性--把'现在单服务、将来可拆'从口号变成结构属性。"
