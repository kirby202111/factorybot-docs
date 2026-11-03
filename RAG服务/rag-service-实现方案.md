# rag-service 实现方案（文档知识服务）

> **本文与设计文档的分工**：[rag-service-整体设计.md](./rag-service-整体设计.md) 讲"为什么这样设计、边界在哪"；本文只讲"怎么落地"——依赖、契约代码、DDL、检索实现、Tool 适配、测试与实施顺序。两份文档冲突时以设计文档的约束为准，本文只负责实现。
>
> **范围**：文档知识服务。制造追溯不在本文范围内，见 [制造追溯-需求与实现效果.md](../制造追溯-需求与实现效果.md)。多步编排不在本文范围内，归 `agent-service`。
>
> **口径纪律**：本文是**规划态实现方案**，不是已上线代码的现状说明。框架与存储的核实结论见 [ADR-01](../架构设计v2/00-架构决策记录ADR.md)。

---

## 0. 实现原则

| 原则 | 落地约束 |
|---|---|
| 契约先行 | 先定 `SearchDocsRequest/Response` 与 `DocEvidence`，检索实现只负责填字段，不改契约 |
| 单一权威源 | MySQL 是版本状态权威源；ChromaDB 是可重建派生物，任何状态判断回查 MySQL |
| 前置过滤 | 版本、租户、文档类型、状态全部进 `where`，不允许"召回后再裁剪" |
| 不可变 chunk | 写入后不改 metadata；升版只追加 |
| 幂等优先 | 摄入、索引、事件消费三条路径都要幂等键，重放不产生副作用 |
| 失败显式 | 所有降级都体现在 `degraded` / `missing_evidence` / 错误码上，不静默返回空 |
| 适配层隔离 | 向量库、Embedding、Rerank、LLM、框架 RAG 抽象都在 adapter 后，业务代码不 import 具体实现 |

---

## 1. 技术选型与依赖

### 1.1 选型表

| 层 | 选型 | 说明 |
|---|---|---|
| 运行时 | Python 3.12+ | 与框架 `>=3.11` 兼容 |
| Web | FastAPI + uvicorn | lifespan 管理连接与启动断言 |
| 校验 | Pydantic v2 | 契约与配置统一 |
| Agent 框架 | AgentScope 2.0（**pin 小版本**） | `Model` / `Embedding` / `Toolkit` / `TracingMiddleware` |
| 向量库 | ChromaDB `>=0.5`（**自持 client**） | 嵌入式 persistent client；默认 `httpx` 客户端可切 |
| Embedding | bge-m3（本地 TEI / sentence-transformers） | 1024 维，cosine |
| 精排 | bge-reranker-v2-m3（cross-encoder） | 本地推理，`top_k → top_n` |
| 文档解析 | unstructured + pypdf + python-docx | 保留标题层级与表格 |
| 关系库 | MySQL 8 + SQLAlchemy 2.0(async) | 文档治理表与审计 |
| 迁移 | Alembic | 与 MES 库分离的独立 schema |
| 事件 | aiokafka | 只读消费组，位点与处理同事务落地 |
| 观测 | opentelemetry-sdk + prometheus-client | W3C `traceparent` 透传 |
| 测试 | pytest + pytest-asyncio + testcontainers | 契约测试 + 集成测试 |

### 1.2 依赖清单（`pyproject.toml`）

```toml
[project]
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
  "agentscope==2.0.11",            # 小版本 pin，升级需过 ADR-01 的核实清单
  "chromadb>=0.5,<0.6",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2",
  "alembic>=1.13",
  "aiokafka>=0.11",
  "httpx>=0.27",
  "unstructured>=0.15",
  "pypdf>=4.2",
  "python-docx>=1.1",
  "sentence-transformers>=3.0",     # bge-m3 / bge-reranker 本地推理
  "opentelemetry-sdk>=1.25",
  "opentelemetry-exporter-otlp>=1.25",
  "prometheus-client>=0.20",
]

[project.optional-dependencies]
dev = ["pytest>=8.2", "pytest-asyncio>=0.23", "testcontainers[mysql]>=4.7", "ruff>=0.5", "mypy>=1.10"]
```

> `agentscope` 只 pin 到小版本而不用 `~=` 通配，是因为 `rag/` 模块曾整体删除又回归（[ADR-01](../架构设计v2/00-架构决策记录ADR.md)）；升级动作必须走一次 API 漂移检查，而不是随 `pip install -U` 漂移。

### 1.3 本地编排（`docker-compose.yml` 节选）

```yaml
services:
  rag-service:
    build: .
    environment:
      RAG_MYSQL_DSN: mysql+asyncmy://rag:rag@mysql:3306/rag
      RAG_CHROMA_PATH: /data/chroma
      RAG_CHROMA_COLLECTION: document_chunks
      RAG_EMBEDDING_ENDPOINT: http://tei-embedding:8080
      RAG_RERANK_ENDPOINT: http://tei-reranker:8081
      RAG_KAFKA_BOOTSTRAP: kafka:9092
      RAG_SEARCH_TIMEOUT_MS: "2000"
    volumes: ["rag-data:/data"]
    depends_on: [mysql, kafka, tei-embedding, tei-reranker]

  tei-embedding:
    image: ghcr.io/huggingface/text-embeddings-inference:1.6
    command: ["--model-id", "BAAI/bge-m3", "--port", "8080"]

  tei-reranker:
    image: ghcr.io/huggingface/text-embeddings-inference:1.6
    command: ["--model-id", "BAAI/bge-reranker-v2-m3", "--port", "8081"]
```

---

## 2. 契约实现

### 2.1 共享契约包

`DocEvidence`、`VersionAnchor`、`MissingEvidence`、错误码枚举来自 monorepo 的共享契约包（`agent-service` / `rag-service` / `mes-eval` 共用），**不在各服务内复制 DTO**。原因：`agent-service` 需要把文档证据与追溯证据并进同一证据集，字段一旦分叉，证据集就无法统一回查。

### 2.2 契约代码

```python
# rag_service/api/schemas.py
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from contracts.evidence import VersionAnchor, DocEvidence, MissingEvidence

class SearchDocsRequest(BaseModel):
    context_id: str = Field(min_length=8)
    query: str = Field(min_length=1, max_length=512)
    version_anchor: VersionAnchor | None = None
    evidence_set_id: str | None = None
    doc_types: list[str] | None = None
    doc_category: Literal["PROCESS_BOUND", "ASSET_BOUND", "GENERAL"] = "GENERAL"
    asset_id: str | None = None
    top_k: int = Field(default=20, ge=1, le=50)
    rerank_top_n: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _anchor_or_evidence_set(self) -> "SearchDocsRequest":
        if self.doc_category == "PROCESS_BOUND":
            if not (self.version_anchor or self.evidence_set_id):
                raise ValueError("VERSION_ANCHOR_REQUIRED")
        if self.version_anchor and self.evidence_set_id:
            raise ValueError("ANCHOR_AND_EVIDENCE_SET_EXCLUSIVE")
        return self

class SearchDocsResponse(BaseModel):
    evidences: list[DocEvidence]
    anchor_used: VersionAnchor | None
    missing_evidence: list[MissingEvidence]
    degraded: bool
    trace_id: str
```

要点：

- **拒绝而不容错**：`PROCESS_BOUND` 缺锚点直接 422 + `VERSION_ANCHOR_INVALID`，不做"默认最新"的隐式补全。
- **禁止字段**：`tenant_id` / `sql` / `collection` / `where_raw` 通过 `model_config = ConfigDict(extra="forbid")` 直接拒绝。静默忽略会掩盖越权尝试。
- **锚点三段校验**：`VersionAnchor` 在共享契约包里已用 `min_length=1` 约束三段，缺段无法构造。

### 2.3 错误码映射

```python
# rag_service/api/errors.py
ERROR_HTTP = {
    "VERSION_ANCHOR_INVALID": 422,
    "VERSION_ANCHOR_MISMATCH": 409,
    "TENANT_FORBIDDEN": 403,
    "NO_MATCH": 200,          # 空结果 + missing_evidence，不是错误
    "INDEX_UNAVAILABLE": 503,
    "BUDGET_EXCEEDED": 429,
}
```

> `NO_MATCH` 返回 200 是刻意选择：调用方（agent）需要把"检索无命中"当作**证据缺口**继续编排，而不是当作异常中断——缺口是一等公民（[ADR-11](../架构设计v2/00-架构决策记录ADR.md) 第 5 条）。

---

## 3. 工程骨架

### 3.1 目录结构

```text
rag-service/
├── pyproject.toml
├── alembic/
├── rag_service/
│   ├── app/
│   │   ├── api/                 # 路由、schema、异常、依赖
│   │   │   ├── docs_search.py
│   │   │   ├── docs_query.py
│   │   │   ├── documents.py
│   │   │   ├── answers.py
│   │   │   └── explain.py
│   │   ├── application/         # 用例：摄入、发布、重绑定、检索
│   │   │   ├── ingestion_service.py
│   │   │   ├── publish_service.py
│   │   │   ├── reindex_coordinator.py
│   │   │   └── retrieval_service.py
│   │   ├── domain/              # 聚合、状态机、策略（不依赖框架）
│   │   │   ├── document.py
│   │   │   ├── version.py
│   │   │   ├── binding.py
│   │   │   ├── chunk.py
│   │   │   └── policies.py
│   │   ├── infrastructure/
│   │   │   ├── vector/chroma_store.py
│   │   │   ├── embedding/bge_client.py
│   │   │   ├── rerank/bge_reranker.py
│   │   │   ├── parser/selector.py
│   │   │   ├── chunking/strategies.py
│   │   │   ├── persistence/{models,mappers,repositories}.py
│   │   │   ├── events/{kafka_consumer,handlers}.py
│   │   │   └── llm/agentscope_model.py
│   │   ├── config.py
│   │   ├── observability.py
│   │   └── main.py
│   └── tests/{unit,integration,contract}/
└── Dockerfile
```

依赖方向：`api → application → domain`，`infrastructure` 实现 domain 定义的 Port。`domain` 不 import FastAPI、chromadb、agentscope。

### 3.2 配置

```python
# rag_service/app/config.py
from pydantic_settings import BaseSettings

class RagSettings(BaseSettings):
    mysql_dsn: str
    chroma_path: str = "/data/chroma"
    chroma_collection: str = "document_chunks"
    embedding_endpoint: str
    rerank_endpoint: str
    kafka_bootstrap: str
    consumer_group: str = "rag-service-doc"
    search_timeout_ms: int = 2000
    max_top_k: int = 50
    deprecated_leak_alert_threshold: int = 0
    model_config = {"env_prefix": "RAG_"}
```

### 3.3 启动与断言

```python
# rag_service/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .config import RagSettings
from .infrastructure.vector.chroma_store import ChromaStore
from .infrastructure.persistence.session import build_engine
from .application.publish_service import PublishService

@asynccontextmanager
async def lifespan(app: FastAPI):
    s = RagSettings()
    engine = build_engine(s.mysql_dsn)
    store = ChromaStore(path=s.chroma_path, collection=s.chroma_collection, dim=1024)
    await store.assert_ready()                      # IndexGate：存在 / 维度 1024 / cosine
    assert_publisher_gate(store)                    # 启动时禁止 bulk flip 的写路径注册
    app.state.engine = engine
    app.state.store = store
    app.state.publish = PublishService(engine, store)
    yield
    await store.close()
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
```

`assert_ready()` 检查四项：collection 存在、`hnsw:space=cosine`、维度 1024、`count() > 0`（或允许空库但记 warning）。任一项不满足直接启动失败——**空索引却对外提供检索，会被 agent 误判为"没有相关文档"**。

---

## 4. 摄入实现

### 4.1 指令与流程

```python
# application/ingestion_service.py
class IngestCommand(BaseModel):
    doc_id: str
    doc_type: Literal["SOP", "MANUAL", "STANDARD", "TRAINING"]
    title: str
    category: Literal["PROCESS_BOUND", "ASSET_BOUND", "GENERAL"]
    tenant_scope: list[str]
    file_path: str
    version_no: str | None = None        # None 表示创建新版本
    anchors: list[BindingRef] = []       # route / bom / rule / asset

async def ingest(cmd: IngestCommand) -> IngestResult:
    content_hash = sha256_of(cmd.file_path)
    if await repo.exists_hash(cmd.doc_id, content_hash):
        return IngestResult(status="SKIPPED_DUPLICATE", version_no=await repo.version_of_hash(cmd.doc_id, content_hash))
    parsed = await parser.parse(cmd.file_path)                 # 保留标题层级 / 表格
    chunks = chunk_selector(cmd.doc_type, parsed)              # 按类型选策略
    vectors = await embedding.embed([c.text for c in chunks])  # 批量，1024 维
    version = await repo.create_version(                       # MySQL 事务
        cmd, state="DRAFT", file_content_hash=content_hash,
        bindings=cmd.anchors, chunk_count=len(chunks),
    )
    await repo.save_staging_chunks(version.version_id, chunks, vectors)
    return IngestResult(status="STAGED", version_no=version.version_no, version_id=version.version_id)
```

- **摄入只落 `DRAFT`**：不直接进检索集合。发布是独立动作，带审核语义。
- **先算 hash 再解析**：重复上传同一个文件不做解析与向量化，省 token 与算力。
- 暂存 chunk 与向量写 MySQL 暂存表（文本 + 向量 blob），发布时再灌 ChromaDB——这样发布失败可以重试，不必重新解析。

### 4.2 切分策略选择器

```python
STRATEGY = {
    "SOP": StepwiseChunker(max_tokens=512, overlap=0.10, keep_step_title=True),
    "MANUAL": FaultCodeChunker(max_tokens=512, overlap=0.10),
    "STANDARD": SectionChunker(max_tokens=384, overlap=0.10, keep_tables_whole=True),
    "TRAINING": SemanticChunker(max_tokens=384, overlap=0.15),
}
```

`section_type` 由 chunker 打标：`STEP` / `FAULT_CODE` / `PARAM` / `NOTE` / `GENERAL`。该字段是检索过滤与引用展示的关键——"E027 怎么修"要能只查 `FAULT_CODE`。

### 4.3 治理表 DDL

```sql
-- 文档
CREATE TABLE knowledge_document (
  document_id     VARCHAR(64)  PRIMARY KEY,
  doc_type        VARCHAR(32)  NOT NULL,
  title           VARCHAR(512) NOT NULL,
  category        VARCHAR(24)  NOT NULL,
  tenant_scope    JSON         NOT NULL,
  current_version VARCHAR(64)  NULL,
  created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_doc_type_category (doc_type, category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 版本
CREATE TABLE document_version (
  version_id         VARCHAR(64) PRIMARY KEY,
  document_id        VARCHAR(64) NOT NULL,
  version_no         VARCHAR(32) NOT NULL,
  state              VARCHAR(24) NOT NULL,       -- DRAFT/SUBMITTED/PUBLISHED/PENDING_REBIND/DEPRECATED/ARCHIVED
  index_state        VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING/READY/FAILED
  file_ref           VARCHAR(1024) NOT NULL,
  file_content_hash  CHAR(64) NOT NULL,
  chunk_count        INT NOT NULL DEFAULT 0,
  effective_at       DATETIME(3) NULL,
  deprecated_at      DATETIME(3) NULL,
  created_at         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_doc_version (document_id, version_no),
  KEY idx_state (state),
  KEY idx_index_state (index_state),
  CONSTRAINT fk_version_doc FOREIGN KEY (document_id) REFERENCES knowledge_document(document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 绑定
CREATE TABLE document_binding (
  binding_id      VARCHAR(64) PRIMARY KEY,
  version_id      VARCHAR(64) NOT NULL,
  binding_type    VARCHAR(16) NOT NULL,          -- route/bom/rule/asset/standard
  version_kind    VARCHAR(16) NOT NULL,
  version         VARCHAR(32) NOT NULL,
  version_ref_id  VARCHAR(64) NOT NULL,
  inherited       TINYINT(1)  NOT NULL DEFAULT 0,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_binding (version_id, binding_type, version_kind, version, version_ref_id),
  KEY idx_target (version_kind, version, version_ref_id),
  CONSTRAINT fk_binding_version FOREIGN KEY (version_id) REFERENCES document_version(version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 幂等与位点
CREATE TABLE index_idempotency (
  idem_key   VARCHAR(160) PRIMARY KEY,           -- doc:version:chunk_seq:strategy_ver
  version_id VARCHAR(64) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE index_offset (
  consumer_group VARCHAR(64) NOT NULL,
  topic          VARCHAR(128) NOT NULL,
  partition_id   INT NOT NULL,
  offset_value   BIGINT NOT NULL,
  updated_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (consumer_group, topic, partition_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审计
CREATE TABLE rag_audit (
  audit_id        VARCHAR(64) PRIMARY KEY,
  trace_id        VARCHAR(64) NOT NULL,
  context_id      VARCHAR(64) NOT NULL,
  endpoint        VARCHAR(64) NOT NULL,
  anchor_used     JSON NULL,
  filters         JSON NULL,
  hit_chunk_ids   JSON NULL,
  degraded        TINYINT(1) NOT NULL DEFAULT 0,
  latency_ms      INT NOT NULL,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_trace (trace_id),
  KEY idx_context_created (context_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 4.4 发布（切块 → 索引）

```python
# application/publish_service.py
async def publish(version_id: str, operator: str) -> None:
    async with uow.begin() as tx:
        v = await tx.versions.lock(version_id)
        if v.state not in ("DRAFT", "SUBMITTED"):
            raise StateConflict(v.state)
        v.state = "PUBLISHED"
        v.effective_at = utcnow()
        await tx.versions.save(v)
        # 同文档同类绑定的旧版本 -> DEPRECATED（不删）
        await tx.versions.deprecate_previous(v.document_id, v.version_no, v.bindings)
        await tx.bus.publish("DocumentVersionPublished", {...})   # Outbox 同事务
    await index_version(version_id)      # 异步：写 chunk state=PUBLISHED + upsert ChromaDB
```

`index_version` 的三条硬约束：

1. `chunk.state` 写入即 `PUBLISHED` 且**永不修改**；
2. 幂等键 `{doc}:{version}:{seq}:{strategy_ver}` 先落库再 upsert，重放不重复；
3. 全部 upsert 成功才置 `index_state=READY`，失败置 `FAILED` 并告警，版本对外不可检索。

---

## 5. 重索引实现

### 5.1 事件消费

```python
# infrastructure/events/handlers.py
HANDLERS = {
    "ProcessRouteActivated":   on_route_activated,
    "ProcessRouteDeprecated":  on_route_deprecated,
    "BomActivated":            on_bom_activated,
    "QualityGateRuleActivated": on_rule_activated,
    "rag.reindex.request":     on_reindex_request,
}

async def on_route_activated(evt, tx) -> None:
    affected = await tx.bindings.find_by_target(kind="route", version_ref_id=evt.route_id, version=evt.old_version)
    for b in affected:
        await tx.versions.mark_pending_rebind(b.version_id)          # 不自动改绑
        await tx.notifications.notify_owner(b.version_id, reason="route_activated")

async def on_route_deprecated(evt, tx) -> None:
    affected = await tx.bindings.find_by_target(kind="route", version_ref_id=evt.route_id)
    for b in affected:
        await tx.versions.deprecate(b.version_id)                     # 不删，保留回溯
```

- `ProcessRouteActivated` **只把文档推入 `PENDING_REBIND`，不自动继承**：`inherited` 必须由 owner 确认。
- `ProcessRouteDeprecated` 直接 `DEPRECATED`：工艺已废止，其 SOP 不应再被默认检索。
- `rag.reindex.request` 来自外部证据侧（制造追溯），只按传入锚点触发重索引，不解析锚点。

### 5.2 位点与幂等

```python
async def consume_loop():
    async for msg in consumer:
        async with uow.begin() as tx:
            if await tx.idempotency.seen(msg.key, group):
                await tx.offsets.save(msg.topic, msg.partition, msg.offset)
                continue
            await HANDLERS[msg.type](msg.payload, tx)
            await tx.idempotency.mark(msg.key, group)
            await tx.offsets.save(msg.topic, msg.partition, msg.offset)
        await consumer.commit()
```

位点与业务变更**同一事务**落库，再提交 offset：这样崩溃重放不会重复改状态，也不会丢处理。消费组与 MES 侧隔离，不共用 group（[ADR-08](../架构设计v2/00-架构决策记录ADR.md) 舱壁）。

### 5.3 DEPRECATED 泄漏兜底

```python
async def drop_deprecated_hits(evidences: list[DocEvidence], repo) -> tuple[list[DocEvidence], int]:
    version_ids = {e.version_id for e in evidences}
    states = await repo.version_states(version_ids)
    kept, leaked = [], 0
    for e in evidences:
        if states.get(e.version_id) == "PUBLISHED":
            kept.append(e)
        else:
            leaked += 1
    if leaked:
        metrics.rag_doc_deprecated_leak_total.inc(leaked)
        logger.error("deprecated chunk leaked through where-filter", extra={"count": leaked})
    return kept, leaked
```

这一步是**兜底而不是主防线**：主防线是 `where` 前置过滤。兜底命中说明元数据与 MySQL 状态不一致（例如 upsert 失败后状态已改），必须按 P1 排查。

### 5.4 重建

```text
1. 按 MySQL 取所有 PUBLISHED 版本 + 暂存 chunk
2. 写入 document_chunks_rebuild_<ts> collection
3. 校验 count 与 checksum
4. 原子切换 collection 别名 -> document_chunks
5. 保留旧 collection 一个周期后删除
```

重建期间对外读旧 collection；重建任务本身只读 MySQL 与文件，不触发任何领域事件。

---

## 6. 检索实现

### 6.1 VectorRetriever

```python
# infrastructure/vector/chroma_store.py
class ChromaStore:
    def __init__(self, path: str, collection: str, dim: int):
        self._client = chromadb.PersistentClient(path=path)
        self._name = collection
        self._dim = dim

    async def assert_ready(self) -> None:
        col = self._client.get_or_create_collection(
            name=self._name, metadata={"hnsw:space": "cosine", "dim": self._dim},
        )
        assert col.metadata.get("hnsw:space") == "cosine", "INDEX_GATE: space != cosine"
        assert col.metadata.get("dim") == self._dim, "INDEX_GATE: dim mismatch"

    async def query(self, vector: list[float], where: dict, top_k: int) -> list[dict]:
        col = self._client.get_collection(self._name)
        res = col.query(
            query_embeddings=[vector],
            n_results=min(top_k, self._max_top_k),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return _to_rows(res)
```

> **`where` 只接受契约字段**：由 `build_where()` 统一构造，禁止把请求体中的任意字典透传给 ChromaDB。ChromaDB 的 `where` 是字符串精确匹配，不做模糊查询，构造时需保证 `version`、`tenant_scope`、`state` 的类型一致。

### 6.2 过滤构造与锚点校验

```python
# application/retrieval_service.py
def build_where(req: SearchDocsRequest, tenant: TenantContext) -> dict:
    where: dict = {"state": "PUBLISHED"}
    if req.doc_category == "PROCESS_BOUND":
        anchor = req.version_anchor or anchor_from_evidence_set(req.evidence_set_id)
        where["version_kind"] = anchor.version_kind
        where["version"] = anchor.version
        where["version_ref_id"] = anchor.version_ref_id
    elif req.doc_category == "ASSET_BOUND":
        if not req.asset_id:
            raise ContractError("ASSET_ID_REQUIRED")
        where["version_kind"] = "asset"
        where["version_ref_id"] = req.asset_id
    where["tenant_scope"] = {"$in": tenant.scopes} if len(tenant.scopes) > 1 else tenant.scopes[0]
    if req.doc_types:
        where["doc_type"] = {"$in": req.doc_types}
    return where

async def search(req: SearchDocsRequest, ctx) -> SearchDocsResponse:
    tenant = await context_service.resolve(req.context_id)
    where = build_where(req, tenant)
    anchor = resolve_anchor(req, tenant)                 # 冲突即 VERSION_ANCHOR_MISMATCH
    vec = await embedding.embed_one(req.query)
    rows = await store.query(vec, where, req.top_k)
    if not rows:
        return SearchDocsResponse(evidences=[], anchor_used=anchor,
                                  missing_evidence=[{"reason": "NO_MATCH", "where": summarize(where)}],
                                  degraded=False, trace_id=current_trace_id())
    ranked = await reranker.rank(req.query, rows, top_n=req.rerank_top_n)
    evidences, leaked = await drop_deprecated_hits(to_evidence(ranked, anchor), repo)
    return SearchDocsResponse(evidences=evidences, anchor_used=anchor,
                              missing_evidence=leak_gap(leaked), degraded=leaked > 0,
                              trace_id=current_trace_id())
```

三个关键点：

1. `anchor_from_evidence_set` 只读 agent 侧传入的**已固化锚点**，不重新解析；
2. `tenant_scope` 单值时用标量而不是 `$in`：ChromaDB 对单值 `$in` 的匹配在大 collection 上更慢；
3. `where` 摘要写入 `missing_evidence` 便于 agent 判断"是没搜到还是被过滤光了"，但**不暴露内部字段名细节**（对外只给 reason 与用户可理解的范围描述）。

### 6.3 精排与引用组装

```python
class BgeReranker:
    async def rank(self, query: str, rows: list[dict], top_n: int) -> list[dict]:
        pairs = [(query, r["document"]) for r in rows]
        scores = await self._client.score(pairs)                 # cross-encoder
        for r, s in zip(rows, scores):
            r["rerank_score"] = s
        return sorted(rows, key=lambda r: r["rerank_score"], reverse=True)[:top_n]

def to_evidence(rows, anchor) -> list[DocEvidence]:
    out = []
    for r in rows:
        m = r["metadata"]
        out.append(DocEvidence(
            evidence_id=f"doc:{m['doc_id']}:{m['version_id']}:{m['chunk_seq']}",
            document_id=m["doc_id"], version_id=m["version_id"], version_no=m["version"],
            title=m["title"], section_type=m.get("section_type"),
            locator=json.loads(m["locator"]), quoted_text=r["document"],
            score=float(r["rerank_score"]), version_anchor=anchor,
        ))
    return out
```

`locator` 必须能定位回原文：PDF 给页码 + 段内偏移，Word 给标题路径 + 段落序号。**引用不可定位等同于没有引用**，评测里 `locator_resolvable` 是硬指标。

### 6.4 RETRIEVAL_ONLY 与生成模式

| 模式 | 触发方 | 行为 |
|---|---|---|
| `RETRIEVAL_ONLY` | agent（默认） | 只返回证据，不调 LLM |
| 生成模式 | 工位 UI 直连 `/rag/docs/query`（单步） | 证据 + LLM 组织答案，强制引用 |

生成模式的提示词骨架：

```text
你是车间文档助手。只能依据 <evidence> 中的片段回答，不得引入外部知识。
每条结论后必须标注来源编号，例如 [1]；无依据时输出"未找到相关文档"。
不得给出工艺参数修改值，不得判断设备是否安全运行。
<evidence>
{numbered_evidences}
</evidence>
```

生成后校验：答案里出现的引用编号必须在证据集中存在，否则重试一次；仍失败则返回证据 + 人工复核标记。`confidence < 0.6` 不展示给操作工。

---

## 7. Tool 适配层

### 7.1 两侧分工

| 侧 | 职责 |
|---|---|
| `agent-service` | 定义 `Tool`（名字、描述、args_schema），注入 `context_id`，扣预算，把返回证据入册 EvidenceRegistry |
| `rag-service` | 实现 HTTP 端点，校验 `context_id`、锚点、租户、预算，保证幂等与超时 |

> 工具描述放在 agent 侧，端点实现放在 rag 侧：这样新增调用方不需要改 rag-service，而改工具描述（影响模型行为）必须过 agent 侧的评测门禁。

### 7.2 agent 侧注册示例

```python
# agent_service/toolkit/rag_tools.py
from agentscope.tool import Tool, Toolkit
import httpx, uuid

class SearchDocsTool(Tool):
    name = "search_docs"
    description = (
        "检索车间文档（SOP / 作业指导书 / 检验标准 / 设备手册）并返回带定位引用的证据。"
        "工艺绑定型文档必须携带 version_anchor（由 version_resolve 或 trace_lookup 产出），"
        "或携带已有 evidence_set_id；两者不得同时缺省。锚点缺失会被拒绝，不要自行猜测版本。"
    )
    args_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 512},
            "doc_category": {"enum": ["PROCESS_BOUND", "ASSET_BOUND", "GENERAL"]},
            "version_anchor": {"$ref": "#/$defs/VersionAnchor"},
            "evidence_set_id": {"type": "string"},
            "doc_types": {"type": "array", "items": {"type": "string"}},
            "asset_id": {"type": "string"},
        },
        "required": ["query", "doc_category"],
        "additionalProperties": False,
    }
    read_only = True
    required_tenant_scopes = True

    def __init__(self, base_url: str, budget: "BudgetPolicy"):
        self._base, self._budget = base_url, budget

    async def __call__(self, ctx, **kwargs) -> dict:
        self._budget.consume("tool_call")                     # 远程调用扣同一预算
        payload = {**kwargs, "context_id": ctx.context_id}   # context_id 由服务端注入
        async with httpx.AsyncClient(timeout=3.0) as cli:
            r = await cli.post(f"{self._base}/rag/docs/search", json=payload,
                               headers={"traceparent": ctx.traceparent,
                                        "X-Request-Id": str(uuid.uuid4())})
        if r.status_code == 422:
            return {"ok": False, "gap": r.json()["code"], "hint": "回溯 version_resolve 重新取锚点"}
        if r.status_code == 503:
            return {"ok": False, "gap": "INDEX_UNAVAILABLE", "degraded": True}
        r.raise_for_status()
        data = r.json()
        ctx.evidence_registry.absorb(data["evidences"])       # 入册，签发 evidence_set_id
        return {"ok": True, **data}

async def build_rag_toolkit(base_url: str, budget) -> Toolkit:
    return Toolkit(tools=[SearchDocsTool(base_url, budget)])
```

要点：

- `context_id` **服务端注入**，不在 `args_schema` 里暴露——模型不能自造租户或预算。
- 工具的 `description` 里显式写“缺锚点会被拒绝、不要猜版本”：描述是行为引导的第一层，但真正的拦截依然在服务端。
- 422 / 503 返回**结构化缺口**而不是抛异常：让编排层可以继续补证据或降级，而不是整个请求失败。
- 远程调用扣同一预算，避免"本地工具限额、远程工具不限额"的预算洞口。

### 7.3 超时、重试与幂等

| 项 | 取值 | 说明 |
|---|---|---|
| 连接超时 | 300ms | 车间内网，连接不应慢 |
| 读超时 | `RAG_SEARCH_TIMEOUT_MS`（默认 2000ms） | 与设计文档 §1.2 的检索 P95 ≤ 1.0s 对齐，留一倍余量 |
| 重试 | 仅对 502/503 重试 1 次 | 422/409 不重试（错在入参） |
| 幂等 | `X-Request-Id` + `idem_key = hash(query+anchor+where+top_k)` | 重试不重复入册证据 |
| 连接复用 | 进程级 `httpx.AsyncClient` | 不为每次调用新建连接 |

### 7.4 Tool 定义版本化

工具描述被静默改写属于注入面（[ADR-09](../架构设计v2/00-架构决策记录ADR.md) 第 3 条）：`SearchDocsTool` 的 `description` / `args_schema` 带 `tool_version`，变更需过评测门禁；agent 侧启动时校验 `tool_version` 与预期一致，不一致告警。

---

## 8. 安全与只读

### 8.1 鉴权与上下文

```python
# api/deps.py
async def resolve_context(x_context_id: str = Header(...), token: str = Depends(bearer)) -> TenantContext:
    claims = verify_service_token(token, audience="rag-service")
    ctx = await context_store.get(x_context_id)
    if ctx is None or ctx.tenant_id != claims.tenant_id:
        raise ContractError("TENANT_FORBIDDEN")
    if ctx.expires_at < utcnow():
        raise ContractError("CONTEXT_EXPIRED")
    return ctx
```

- `context_id` 由 `agent-service` 签发并短时有效（默认 30 分钟），服务端据此还原租户、scopes、剩余预算与证据集。
- 请求体里出现 `tenant_id` / `scopes` / `sql` / `collection` 等字段：`extra=forbid` 直接 422，不静默忽略。
- 文档管理端点走 RBAC + 双人复核（发布动作），审计记 `operator`、`before/after` 状态、锚点差异。

### 8.2 启动 Gate

| Gate | 断言 | 时机 |
|---|---|---|
| `IndexGate` | collection 存在、维度 1024、space=cosine | 启动 |
| `TenantGate` | `context_store` 可用、签名密钥已加载 | 启动 |
| `ReadOnlyGate` | 出站白名单内无写方法 | 启动 |
| `RawDocGate` | 原始文档目录只读挂载 | 启动 |
| `PublishGate` | 不存在批量翻转 chunk 状态的代码路径 | CI 静态检查 |

### 8.3 注入面防护

- 文档文本是**不可信输入**：切块后最多保留正文，剔除脚本、宏、隐藏文本；提示词里用标签隔离，不复述文档中的"指令性文字"。
- 出参脱敏：不返回文件绝对路径、不返回其他文档 ID 列表。
- `quoted_text` 长度上限（默认 1200 字符），避免超大片段挤占上下文。

---

## 9. 可观测与工具 SLA

### 9.1 埋点

```python
SEARCH_LATENCY = Histogram("rag_search_latency_seconds", buckets=[.05,.1,.25,.5,1,2,3])
SEARCH_TOTAL = Counter("rag_search_total", ["mode", "degraded"])
ANCHOR_REJECT = Counter("rag_anchor_reject_total", ["reason"])
DEPRECATED_LEAK = Counter("rag_doc_deprecated_leak_total")
CITATION_MISSING = Counter("rag_citation_missing_total")
REINDEX_PENDING = Gauge("rag_reindex_pending_versions")
INGEST_LAG = Gauge("rag_ingest_lag_seconds")
```

### 9.2 SLA

| 路径 | P95 | P99 | 备注 |
|---|---|---|---|
| `/rag/docs/search`（`RETRIEVAL_ONLY`） | ≤ 1.0s | ≤ 2.0s | 不含生成；重排占比最大 |
| `/rag/answers/{id}` | ≤ 200ms | ≤ 500ms | 纯 DB 回查 |
| `/rag/documents/*` 发布 | ≤ 5s（同步部分） | — | 索引异步 |

### 9.3 限流与背压

- 按 `context_id` 的租户维度限流（默认 20 QPS / 租户），超限返回 429 + `BUDGET_EXCEEDED`。
- ChromaDB 并发查询用信号量限流（默认 8），防止重排前排队把 P99 拉满。
- Kafka 消费除 pause 背压外，`index_state=PENDING` 积压超阈值时告警，不阻塞在线检索。

---

## 10. 测试与契约测试

### 10.1 分层

| 层 | 范围 | 关键用例 |
|---|---|---|
| 单测 | domain 状态机、`build_where`、锚点校验、切块器 | 方案 B 继承绑定；`PENDING_REBIND` 不可检索；三段缺一拒绝 |
| 集成 | 摄入→发布→检索（testcontainers: MySQL + 临时 Chroma 目录） | 升版后旧版本不召回；重建后结果一致 |
| 契约 | `search_docs` 与 agent 的联调 | 缺锚点 422；跨租户 403；`evidence_set_id` 回查一致 |
| 安全 | 越权与注入 | 请求体带 `tenant_id` 被拒；文档内嵌指令不影响引用 |

### 10.2 必过用例（硬门禁）

1. `PROCESS_BOUND` + 无锚点 → 422 `VERSION_ANCHOR_INVALID`，且**不**返回任何证据；
2. 锚点 `v3` 检索 → 结果全部 `version=v3`，`v4` 命中数为 0；
3. 文档 DEPRECATED 后检索 → 不召回；直接按 chunk id 回查 → 仍可取（回溯允许）；
4. 跨租户请求 → 403，且审计落库；
5. ChromaDB 宕机 → 503 `INDEX_UNAVAILABLE`，不返回空列表；
6. 同一文件重复摄入 → `SKIPPED_DUPLICATE`，chunk 数不变；
7. 同一事件重放 → 版本状态与 chunk 数不变（幂等）；
8. 答案引用编号全部可在证据集中找到。

### 10.3 与追溯、agent 的联调

- 追溯侧提供带三段锚点的证据集；本服务用 `evidence_set_id` 取锚点检索，验证两侧锚点一致。
- 锚点冲突用例：证据集 `v3` + 显式 `v4` → 409 `VERSION_ANCHOR_MISMATCH`，agent 侧必须停下面不是选一个。

---

## 11. 实施步骤

| # | 步骤 | 产物 | 依赖 |
|---|---|---|---|
| 1 | 共享契约包落地（`VersionAnchor` / `DocEvidence` / 错误码） | 契约包 v1 + schema 快照 | 无 |
| 2 | 治理表 DDL + 仓储 + Alembic | 迁移脚本 | 1 |
| 3 | 解析与切分选择器 | 四类 chunker + 单测 | 2 |
| 4 | ChromaDB 客户端 + `IndexGate` + 重建工具 | `ChromaStore` | 2 |
| 5 | 摄入 → 发布 → 索引闭环 | `/rag/documents/*` | 3、4 |
| 6 | 检索（过滤、重排、证据组装） | `/rag/docs/search` | 5 |
| 7 | 事件消费与重索引 | Kafka handlers + 幂等 | 2 |
| 8 | Tool 适配 + agent 联调 | `SearchDocsTool` + 契约测试 | 6 |
| 9 | 观测、限流、降级演练 | dashboards + 演练记录 | 6 |
| 10 | 评测集接入与门禁 | pytest 门禁 + 指标报告 | 6、8 |

> 第 1-6 步不依赖追溯与 agent，可并行推进；第 8 步才需要 agent 侧提供 `context_id` 与证据集接口。

---

## 12. 约束落地检查清单

**契约**

- [ ] `search_docs` 入参 `extra=forbid`，`tenant_id`/`sql`/`collection` 无法传入
- [ ] `PROCESS_BOUND` 必需锚点或 `evidence_set_id`，二选一互斥
- [ ] `NO_MATCH` 返回 200 + `missing_evidence`，不是异常

**版本**

- [ ] `where` 包含 `state="PUBLISHED"` + 三段锚点 + `tenant_scope`
- [ ] 锚点缺失不回落当前版本（代码中无此类分支，CI 静态检查）
- [ ] `deprecated_leak` 掊截存在且打点

**数据**

- [ ] chunk 写入后无 UPDATE 路径（发布/重建除外，重建写新 collection）
- [ ] 幂等键覆盖摄入、索引、事件三条路径
- [ ] 位点与业务变更同事务

**安全**

- [ ] 出站无写方法；启动 `ReadOnlyGate` 通过
- [ ] 文档管理端点 RBAC + 发布审计
- [ ] 不返回文件绝对路径与其他文档 ID 列表

**可观测**

- [ ] `rag_*` 指标全部上报，`deprecated_leak` 有告警
- [ ] traceparent 透传，`/rag/explain` 可还原判定过程

---

## 13. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| ChromaDB 批量 upsert 无事务 | 部分 chunk 成功、元数据不一致 | MySQL 权威 + 可重建 + `deprecated_leak` 掊截 |
| 框架 `rag/` 模块再次缺位 | 解析/切块抽象不可用 | 适配层隔离 + 自持 client + 升级评审门禁 |
| 切块参数与真实文档不匹 | 召回低、引用跨页 | P0 阶段用真实文档标定，参数随评测版本化 |
| 文档语料随时间膨胀 | 检索变慢、噪声上升 | `top_k` 上限 + 重排 + 文档类型分层检索 |
| 文档 owner 长期不确认 `PENDING_REBIND` | 工艺已升版但文档不可检索 | 积压告警 + 超时升级提醒（不自动继承） |
| 锚点传递链路过长（追溯→agent→rag） | 模型抄错版本 | `evidence_set_id` 直取锚点 + 服务端校验 |

---

## 14. 面试 Q&A（工程层）

**Q1：为什么不把版本信息放在 chunk 文本里让模型自己注意？**
A：靠模型注意版本等于把一致性押在提示词上。实测嵌入相似度区分"被推翻的事实"与"重复表述"接近随机（ADR-06），所以版本必须是**结构字段 + 前置过滤 + 缺失拒答**。文本里写版本号只是给人看的，不参与判定。

**Q2：`where` 前置过滤和"召回后再过滤"的区别有多大？**
A：两个层面：正确性上，后过滤会让无效/越权片段进入模型上下文，即使删掉回答，片段也已经"看过"；性能上，后过滤需要放大 `top_k` 才能凑够结果，重排成本反而更高。所以过滤必须下推到向量库。

**Q3：发布失败怎么办？**
A：MySQL 是权威源，版本置 `index_state=FAILED` 且对外不可检索；暂存 chunk 和向量还在 MySQL，重试只重跑索引，不重新解析与向量化。全量重建走 collection 别名原子切换。

**Q4：事务边界怎么划？**
A：MySQL 内是事务（版本状态 + Outbox 事件 + 幂等记录 + 位点）；ChromaDB 不入事务。用"先落库、后派生、以状态字段表达派生进度"的最终一致：`index_state` 就是派生进度，检索只看 `READY`。

**Q5：怎么防止模型编造不存在的引用？**
A：三道：提示词只给编号；生成后做引用存在性校验；无引用或引用非法就重试一次，仍失败则降级为"证据 + 人工复核"。`confidence < 0.6` 不展示给操作工。

**Q6：为什么不直接用框架的 RAG 模块做端到端检索？**
A：已核实无 ChromaDB 适配器，且该模块在 2.0.0 被整体删除、2.0.5 才回归。把向量库依赖押在一个"历史上被删过"的抽象上不可接受，所以只在解析与切块环节用它，向量库自持 client。

---

## 15. 一句话

**MySQL 管状态、ChromaDB 管召回、适配层管可替换；锚点三段进 `where`，证据带 locator 出，降级一律显式；不猜版本，不静默空结果。**