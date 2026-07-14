# data/ — FactoryRAG 模拟测试数据

车间 MES（SMT/PCBA + 整机组装）场景的模拟数据，用于在**零外部依赖**（无 docker / 无 API key / 无真实 ChromaDB·Neo4j·Embedding·LLM）下对 RAG 检索管线做端到端测试。

## 目录结构

```
data/
├── README.md                       # 本文件
├── manifest.json                   # B 文档元数据清单（doc_id/title/category/doc_type/bindings/version_*/file/state）
├── queries.json                    # B 期望查询集（query + 版本锚点过滤 + 期望命中/排除/关键词）
├── documents/                      # B 文档原文（markdown，与 IngestionService 解析路径一致）
│   ├── sop_smt_reflow_v3.md            # SMT 回流焊 SOP @ v3（PROCESS_BOUND）
│   ├── sop_smt_reflow_v4.md            # 同 SOP 升版 v4（验证版本隔离）
│   ├── sop_smt_reflow_v2_deprecated.md # v2 已废弃（验证 DEPRECATED 永不返回）
│   ├── sop_box_build_assembly_v2.md    # 整机组装 SOP @ v2（PROCESS_BOUND）
│   ├── manual_reflow_oven_eq001.md     # 回流焊炉维修手册（ASSET_BOUND @ v1，故障码 E001/E002）
│   ├── manual_pickplace_eq002.md       # 贴片机维护手册（ASSET_BOUND @ v1，故障码 E101/E102）
│   ├── standard_ipc_a610.md            # IPC-A-610 焊点验收标准（GENERAL @ RevH）
│   └── standard_esd.md                 # ESD 防静电规范（GENERAL @ v1）
└── trace/
    └── scenarios.json              # A 追溯 5M1E 子图场景（seed + TraceSubgraph + 期望断言）
```

## 数据如何变成 RAG 可检索对象

- **B（文档型）**：`tests/_mock_rag_infra.py::load_doc_chunks()` 读 `manifest.json` + 对应 markdown，
  用**真实** `ChunkStrategySelector.split()`（与 `IngestionService` 同路径）切分为 `DocumentChunk`，
  manifest 的 `state` 覆盖 chunk 状态（构造 DEPRECATED 样本）。
- **A（追溯型）**：`load_trace_scenarios()` 读 `scenarios.json`，每个场景的 `subgraph` 直接
  `TraceSubgraph.model_validate()`（与领域模型同构），`FakeGraphRetriever` 按 seed 返回对应子图。

## 伪造件（仅测试，替代真实基础设施）

| 真实组件 | 测试伪造件 | 说明 |
|----------|-----------|------|
| bge-m3 Embedding | `FakeEmbedder` | 复用 `Tokenizer` 的 hash 词袋 1024 维向量，同词文本 cosine 高（确定性） |
| ChromaDB collection | `FakeChromaCollection` | 内存存 chunks+embeddings，`.query()` 走 cosine + `where` 过滤（复用 ChunkFilter 语义） |
| bge-reranker | `FakeReranker` | 透传保序（精排阶段非本测试重点） |
| DeepSeek LLM（B） | `_StubDocLLM` | 拼接 top chunk 摘要返回 `.content` |
| Neo4j 图检索器（A） | `FakeGraphRetriever` | 按 seed 返回 mock `TraceSubgraph`，`version` 过滤 Method 快照节点 |
| DeepSeek LLM（A） | `_StubTraceLLM` | 从子图 prompt 抽取真实 node_id 作证据，返回 JSON 假设（禁实体幻觉） |
| Redis / subgraph_repo | 内存 / `FakeSubgraphRepo` | 缓存与持久化置空或内存版 |

**A→B 跨路线**：A 的 `doc_rag` 注入**真实** `InProcessDocRagAdapter` 包裹**真实** B
`DocumentRetrievalService`（真实 BM25 over mock SOP），验证版本一致性三段链第一段：
图锁定 v3 → A 透传 ROUTE 锚点 v3 → B 仅召回 v3 SOP。

## 跑测试

```bash
cd FactoryRAG
uv run pytest tests/test_mock_data_rag.py    tests/test_mock_data_trace.py -v
# 或全量
uv run pytest
```

无需 `docker-compose up`，无需 `.env` 配真实 key。BM25 依赖 `rank-bm25` + `jieba`（已在 `pyproject.toml`）。

## 加数据

- **加 B 文档**：往 `documents/` 放 markdown，在 `manifest.json` 加一条（`doc_type` 决定切分策略：
  SOP 步骤切 / MANUAL 标题切 / STANDARD 按句切），按需在 `queries.json` 加期望查询。
- **加 A 场景**：在 `trace/scenarios.json` 加一个场景（seed + 完整 5M1E 子图 + expected）。

## 边界（模拟数据测不到的）

- B：真实 ChromaDB 持久化、真实 bge-m3 向量质量（用 hash 词袋近似）。
- A：真实 Neo4j Cypher 多跳执行、Kafka 投影建图管线。
- 这些是基础设施层，与现有"重依赖懒导入、单测不触外部"口径一致；领域/应用层逻辑被完整覆盖。
