# FactoryRAG 实现进度

> rag-service（单服务 + 共享内核承载 A/B/E 三路线 RAG）的代码实现进度。
> 依据：[`RAG服务/rag-service-技术选型和实现方案.md`](../RAG服务/rag-service-技术选型和实现方案.md) +
> [`RAG服务/rag-service-整体结构设计.md`](../RAG服务/rag-service-整体结构设计.md) +
> 三路线详细设计/实现方案。

最后更新：2026-07-13

---

## 一、已完成 ✅

### 1. 项目脚手架
- `pyproject.toml`（依赖收敛口径见技术选型 §1.1/§1.3，含 [A]/[B]/[E] 标注）
- `Dockerfile`（python:3.11-slim + gunicorn uvicorn worker）
- `docker-compose.yml`（rag-service + neo4j/mysql/redis/minio/kafka/bge-inference，ChromaDB 嵌入式挂卷）
- `.env.example`（前缀 `RAG_`、嵌套 `__`，含路线级开关）
- `.gitignore`

### 2. shared/ 共享内核（10 子包，全部完成）
| 子包 | 关键文件 | 状态 |
|------|---------|------|
| `ai/` | `port.py`(LlmPort)、`observable_chat_model.py`(ChatResult+ObservableChatModel，**inner 懒构造**)、`llm_factory.py`(provider 无关) | ✅ |
| `embedding/` | `port.py`(EmbeddingPort)、`bge_client.py`(BgeClient sidecar+本地兜底、BgeReranker) | ✅ |
| `obs/` | `context/port/tracing/metrics/logging_ctx/redactor/observability`（OTel+prometheus+structlog，失败不反噬业务） | ✅ |
| `config/` | `base.py`(BaseSettings+9 子配置)、`rag_settings.py`(RagSettings+路线开关+子配置) | ✅ |
| `kafka/` | `domain_event/consumer_group/idempotency_repo/offset_repo/projection_handler`（手动 ack+位点落 MySQL+双重幂等） | ✅ |
| `acl/` | `base_client`(BaseReadonlyAclClient)、`ports`(TraceRagPort/DocRagPort)、`adapters`(InProcess/Http)、`gates`(ReadOnlyAclGate+StartupAssertionError)、`mes_clients`(ProcessManagement/Checkpoint+MesClients) | ✅ |
| `persistence/` | `base`(DeclarativeBase)、`models`(IndexIdempotency/IndexOffset @rag_shared)、`db`(DbEngines：mysql/neo4j/chroma/redis 懒初始化+probe) | ✅ |
| `tenant/` | `context/dependency/propagation`（TenantContext + 跨服务传递协议一处定义） | ✅ |
| `web/` | `container`(DI 组合根)、`lifespan`(断言→探测→装配→schema→consumer)、`health`(/health /ready /metrics) | ✅ |
| `events/` | `version_contract`(VersionKind/VersionAnchor/ReindexRequest，版本一致性三段链第一段) | ✅ |

### 3. routes/document/（B 文档型）✅
- **domain**：`document.py`(KnowledgeDocument AR/DocumentVersion/DocumentBinding+枚举，**决策#3 状态机无 SUBMITTED**、**决策#2 rule_id+rule_version 双轨**)、`chunk.py`(**chunk 不可变**+ChunkLocator+to_metadata_dict/from_chroma)、`answer.py`(DocQuery/DocSearch/DocAnswer/DocCitation/ChunkHit/IngestCommand)、`projection.py`(ReindexHandler+ReadOnlyIngestionGate)
- **application**：`ingestion_service`(SHA256 查重+MinIO+切分+embed+MySQL事务+ChromaDB upsert)、`retrieval_service`(**强制 route_version 红线**+DEPRECATED 泄漏兜底+rerank+LLM 综合+低置信转人工)、`reindex_coordinator`(决策#3 联动 PUBLISHED)、`chunking`(SOP/MANUAL/STANDARD 三策略)
- **infrastructure**：`chromadb/`(schema/chunk_repo/retriever where pre-filter/document_repo MySQL rag_doc)、`minio_/object_store`、`parser`(unstructured+pypdf+python-docx 降级)、`handlers/`(process_route 联动PUBLISHED / reindex 从MinIO重建 / quality 预留)、`acl/`

### 4. routes/traceability/（A 追溯型）✅
- **domain**：`seed.py`(Seed/SeedKind/TraceQuery/ExpandRequest)、`subgraph.py`(TraceNode/TraceEdge/FiveM1ECluster/TraceSubgraph+**subgraph_ref**+route_version_locked)、`answer.py`(RootCauseHypothesis/TraceAnswer)、`projection.py`(GraphProjector+**ReadOnlyProjectionGate**(禁DELETE/REMOVE)+**RawDataTopicGate**(禁 dc.*))
- **application**：`seed_resolver`(正则→bge-m3缺陷匹配→LLM兜底)、`trace_retrieval_service`(seed→缓存→expand→LLM综合+**经DocRagPort拉SOP**+on_route_upgraded 发 rag.reindex.request)
- **infrastructure**：`neo4j/schema`(约束+索引+向量索引 DDL 幂等)、`neo4j/retriever`(WIP_5M1E Cypher，**SNAPSHOT_OF_ROUTE{route_version} 版本锁定**)、`neo4j/projections/`(checkpoint/process_route/material/quality 4 handler + registry，全 MERGE 无 DELETE)、`neo4j/subgraph_repo`(rag_trace.subgraph_audit)、`acl/`(Material/Quality A专属)、`rag/graph_index`(LlamaIndex 预留)

### 5. routes/agentic/（E Agentic）✅
- **domain**：`intent.py`(IntentCategory 5 类)、`answer.py`(AgentAnswer/AnswerSource/ChatRequest/AnswerAuditView)、`tool.py`(ToolDescriptor/ToolRegistry+**ReadOnlyToolGate**+build_default 注册 A/B 工具)
- **application**：`intent_router`(规则优先+LLM兜底)、`gateway_service`(缓存→意图→LangGraph(recursion_limit=6,timeout 70s)→审计+缓存，**traceparent 生成**，超时/越界转人工)
- **infrastructure**：`ai/route_graph_builder`(StateGraph: router→{tool|delegate|converge}→END，LangGraph 不可用降级)、`ai/tool_executor`、`ai/delegator`(L1/L2)、`acl/l1_delegation+l2_delegation`(httpx+traceparent透传，决策#1)、`persistence/models+audit_repo`(rag_agentic.answer_audit/route_trace)、`redis_/query_cache`

### 6. api 层 ✅
`deps.py`(get_*_svc+tenant)、`middleware.py`(TenantMiddleware+RequestLogMiddleware)、`errors.py`(ValueError→400/Exception→500)、`register.py`(按路线开关注册+health 始终注册)、`v1/{trace,doc,chat}_router.py`

### 7. app 入口 ✅
`config.py`(load_settings)、`main.py`(create_app：lifespan+middleware+register_routers+exception_handlers，模块级 `app = create_app()`)

### 8. 验证（已通过）✅
- `python -m compileall app` → **128 文件全部编译通过**（exit 0）
- 核心依赖（pydantic/pydantic-settings/fastapi/sqlalchemy/httpx/structlog）安装后：
  - **34 个 shared/domain 模块导入全部 OK**
  - **46 个 application/infrastructure/api 模块导入全部 OK**（重依赖 chromadb/neo4j/langgraph/minio/aiokafka 均函数内懒导入，模块导入不触发）
  - `app.main` 导入成功，FastAPI 构造成功，OpenAPI 路由注册正确（B 开启时 `/rag/docs/{query,search,ingest}` + `/health /ready /metrics`）

---

## 二、待完成 ⏳

### 1. Alembic 迁移（高优先）
- [ ] `alembic.ini`（脚本位置 `alembic/versions/`，数据库 URL 占位）
- [ ] `alembic/env.py`（**async + asyncmy**，加载 `RagSettings`，`target_metadata = Base.metadata`，import 全部 model 模块以注册到 metadata）
- [ ] `alembic/script.py.mako`
- [ ] `alembic/versions/0001_initial.py`：建 4 schema + 表
  - `rag_shared`：`index_idempotency`、`index_offset`（已在 `shared/persistence/models.py`）
  - `rag_trace`：`subgraph_audit`（在 `traceability/infrastructure/neo4j/subgraph_repo.py`）
  - `rag_doc`：`knowledge_document`、`document_version`（在 `document/infrastructure/chromadb/document_repo.py`）
  - `rag_agentic`：`answer_audit`、`route_trace`（在 `agentic/infrastructure/persistence/models.py`）
  - 含 `upgrade()` + `downgrade()`
- [ ] Neo4j 用 `SchemaInitializer`（已实现，非 Alembic）、ChromaDB 用 `ChromaCollectionInitializer`（已实现，非 Alembic）

### 2. 修复跨路线 domain 直 import 违规（高优先，设计硬约束）
**问题**：设计要求"路线间禁止直接 import 对方 application/domain，一律走 `shared/acl/` Port"。当前两处违规：
- `app/routes/traceability/application/trace_retrieval_service.py` 的 `_enrich_suggested_action`：`from app.routes.document.domain.answer import DocQuery` + `from app.routes.document.domain.document import DocumentCategory`，构造 B 的 DocQuery 后传给 `DocRagPort.query`
- `app/routes/agentic/domain/tool.py` 的 `build_default`：`from app.routes.traceability.domain.seed import ExpandRequest, SeedKind` + `from app.routes.document.domain.answer import DocSearch` 等，构造 A/B 的请求 DTO

**推荐修法**（二选一）：
- (a) **Port 方法改收原语**：`TraceRagPort.expand(kind, value, as_of, route_version, tenant)`、`DocRagPort.search(query, route_version, doc_types, tenant)`；InProcess Adapter 内部构造各路线 DTO。调用方（A/E）只传原语，零跨路线 import。
- (b) **shared/acl 定义 Port DTO**：在 `shared/acl/ports.py` 旁加 `dto.py` 定义跨路线契约模型（TraceQueryDto/DocSearchDto），路线 domain 映射。代价是双倍模型。
- 推荐 (a)：改动小，调用方更干净。需同步改 `shared/acl/ports.py`、`adapters.py`（InProcess+Http）、A 的 `_enrich_suggested_action`、E 的 `tool.py` 与 `tool_executor.py`。

### 3. 单元测试 `tests/`
- [ ] `tests/test_gates.py`：5 个 Gate 断言
  - `ReadOnlyAclGate`：构造含 `create_xxx` 方法名的 client → 断言 `StartupAssertionError`
  - `ReadOnlyProjectionGate`：构造含 `DELETE`/`REMOVE` 的 cypher_templates handler 类 → 断言抛错
  - `RawDataTopicGate`：订阅 `dc.equipment.raw`（非白名单）→ 断言抛错；白名单 3 主题通过
  - `ReadOnlyIngestionGate`：handler.handle 源码含 `.post(` + `mes` → 断言抛错
  - `ReadOnlyToolGate`：注册 `read_only=False` 工具 → 断言抛错
- [ ] `tests/test_chunk_immutability.py`：B chunk 不可变
  - 升版追加新 chunk，老 chunk metadata 不变
  - `DocumentChunk.to_metadata_dict()` 字段完备
  - `VectorRetriever._build_where` 强制带 `state=PUBLISHED`+`route_version` 等值
- [ ] `tests/test_version_contract.py`：`VersionAnchor`/`ReindexRequest`、`parse_anchor` 缺失版本抛错
- [ ] `tests/test_port_adapter.py`：InProcessTraceRagAdapter/InProcessDocRagAdapter 直调 svc（mock svc）
- [ ] `tests/test_retrieval_enforce_version.py`：B `DocumentRetrievalService.query` 对 PROCESS_BOUND 缺 route_version → `ValueError`
- [ ] `tests/test_route_graph.py`：E `_FallbackGraph` 路径（langgraph 不可用时）按意图走 tool/delegate/converge

### 4. README.md
- [ ] 架构定位（单服务+共享内核+三路线，与设计文档对齐）
- [ ] 目录结构说明
- [ ] 启动：`pip install -e .` + `docker-compose up` + 路线开关 env
- [ ] Alembic 迁移命令
- [ ] 测试命令
- [ ] 路线灰度顺序 B→A→E
- [ ] 只读红线 / chunk 不可变 / 版本一致性三段链 说明

### 5. 最终一致性核查
- [ ] 全量 `python -m compileall app tests`
- [ ] 装齐依赖后 `pytest` 通过
- [ ] `gunicorn app.main:app` 实际启动 lifespan（需 mysql/redis/chroma/minio 就绪）

---

## 三、已知小问题（待清理，非阻塞）

1. **`app/routes/traceability/__init__.py` 的 `_wire_trace_consumer`** 访问 `container.engines._neo4j_driver`（私有属性）。建议在 `DbEngines` 加 `async def neo4j_driver()` 公共访问器，或缓存到 container。
2. **`_wire_trace_consumer` 的 `tx_provider`** 内 `from neo4j import AsyncGraphDatabase` 是无用导入（A 的 tx 返回 driver，handler 自行开 session）。删除该 import。
3. **`tool_executor.py` 的 `_invoke`** 对 DOC_LOOKUP 传 `route_version=""`（空串）——B 检索入口对 PROCESS_BOUND 会拒绝。生产应由 E 从问题/上下文解析出 route_version 再传入；当前是脚手架占位。
4. **`audit_repo.py` 的 `RouteTraceRepo._save`** `audit_id=""`（MVP 简化，未回填关联）。生产应在 `AnswerAuditRepo.record` 后回填或改顺序：先建 audit 占位再记 trace。
5. **`probe` MySQL 探测** 已用 `text("SELECT 1")` 修正（早期版本占位有误）。
6. **`metrics.py`** `prometheus_client` 缺失时降级 `_NoopMetric`，但 `Counter(..., ['model'])` 的 label 维度在 no-op 路径被忽略——仅观测丢失，不影响业务。

---

## 四、设计落地点速查（已实现）

| 设计要求 | 实现位置 |
|---------|---------|
| 单服务+共享内核，路线间走 Port/Adapter | `shared/acl/ports.py`+`adapters.py`，`container.py` 注入 InProcess |
| 只读红线 ReadOnly*Gate 启动断言 | `shared/acl/gates.py`、各路线 domain/projection.py、`lifespan.py` 分预/后装配两阶段 |
| 多 DB 共存按路线降级 | `shared/persistence/db.py` probe + `lifespan` 标记降级 |
| B chunk 不可变 + 强制 route_version | `domain/chunk.py`、`retrieval_service._enforce_route_version`、`chromadb/retriever._build_where` |
| B 决策#3 联动 PUBLISHED | `handlers/process_route.py` ProcessRouteActivatedHandler |
| B 决策#2 rule_id+rule_version 双轨 | `domain/document.py` DocumentBinding + `handlers/quality.py` 预留 |
| B ChromaDB 嵌入式 + MinIO 重建兜底 | `chromadb/schema.py`、`handlers/reindex.py` 从 MinIO 重建 |
| A SNAPSHOT_OF_ROUTE{route_version} 版本锁定 | `neo4j/retriever.py` Cypher + `_derive_edges` |
| A 发 rag.reindex.request 通知 B | `trace_retrieval_service.on_route_upgraded` + `events/version_contract.ReindexRequest` |
| E LangGraph ≥0.2 recursion_limit=6 | `ai/route_graph_builder.py`（不可用降级 _FallbackGraph） |
| E traceparent 全链路（决策#1） | `gateway_service._build_traceparent` + `l1/l2_delegation` 透传 |
| E InProcess 调 A/B（决策#4） | `container` 注入 InProcess Adapter，`tool_executor` 经 Port 调 |
| 版本一致性三段链第一段 | A 图快照边 + `TraceAnswer.route_version` 透传 |
| Alembic 管 MySQL 多 schema | ⏳ 待建（Neo4j/ChromaDB 已非 Alembic） |
| 租户跨服务传递一处定义 | `shared/tenant/propagation.py` |

---

## 五、接续工作的建议顺序

1. **修跨路线 import 违规**（第二节 #2）——设计硬约束，应最先修，避免扩散。
2. **Alembic 迁移**（第二节 #1）——补齐运维底座。
3. **单元测试**（第二节 #3）——锁住红线不变式。
4. **README**（第二节 #4）。
5. **小问题清理**（第三节）+ 最终核查。

> 注：本仓库是文档工作区，Python 依赖未全局安装；重依赖（chromadb/neo4j/langgraph/minio/aiokafka/langchain）均在函数内懒导入，模块导入不触发。运行时需 `pip install -e .` 装齐依赖。
