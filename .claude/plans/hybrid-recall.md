# 粗排召回 (Hybrid Search) 实现方案

## 目标
在 B 路线（文档型 RAG）第一阶段召回中，用 **混合检索 (Hybrid Retrieval)** 取代当前纯稠密检索：
- **稀疏检索 (BM25)**：关键词精确匹配，解决专有名词（设备型号、故障码、工艺参数名）。
- **稠密检索 (Embedding)**：语义向量，解决同义词与模糊表达。
- **RRF (Reciprocal Rank Fusion)** 融合两路排名 → 统一候选集，交给既有 rerank 精排。

## 设计原则（贴合既有架构）
- 遵循 DDD / Ports & Adapters / 组合根单点装配；`from __future__ import annotations`、中文 docstring、惰性 import、`async`。
- 新增 `RetrieverPort`（Protocol）显式化检索契约（ISP/DIP），`VectorRetriever`/`Bm25Retriever`/`HybridRetriever` 均满足之；`DocumentRetrievalService` 仅把 `retriever: Any` 收紧为 `RetrieverPort`（鸭子类型，向后兼容）。
- **过滤一致性硬约束**：稠密走 ChromaDB `where` pre-filter，稀疏走内存谓词。两者必须语义一致（state/route_version/asset_id/tenant_scope/doc_type）。抽取共享 `ChunkFilter`，同时产出 ChromaDB `where` dict 与 Python 谓词，消除漂移。
- **BM25 索引一致性**：chunk 不可变，但存在单条软删（state=DEPRECATED）。BM25 索引作为 `ChunkRepo` 的**只读投影**，由 `ChunkRepo`（ChromaDB 写入唯一 chokepoint）在 upsert/soft_delete/delete_by_version 时同步 add/remove —— 保证两套存储永不分叉。禁用时该协作为 `None`，纯稠密路径零影响。

## 模块布局（新增/修改）
```
routes/document/
  domain/
    retriever_port.py        # 新增：RetrieverPort Protocol
  application/
    hybrid_retriever.py      # 新增：HybridRetriever（RRF 融合），满足 RetrieverPort
    retrieval_service.py     # 改：retriever 类型标注 -> RetrieverPort（纯加法）
  infrastructure/
    chunk_filter.py          # 新增：build_where() + build_predicate() 共享（DRY）
    chromadb/
      retriever.py           # 改：_build_where 委托 chunk_filter（行为不变）
      chunk_repo.py          # 改：可选 bm25_index 协作（upsert→add, soft_delete/delete→remove）
    bm25/                    # 新增包
      __init__.py
      tokenizer.py           # jieba 分词，缺失时降级正则
      bm25_index.py          # Bm25Index（rank_bm25 包装 + chunk 元数据 + add/remove）
      bm25_retriever.py      # Bm25Retriever，满足 RetrieverPort
  __init__.py                # 改：按 hybrid_recall_enabled 选 Hybrid/Dense
shared/config/rag_settings.py # 改：DocSettings += 混合召回字段
pyproject.toml              # 改：+ rank-bm25, jieba（[B] 路线依赖）
.env.example                # 改：+ 混合召回配置样例
tests/
  test_tokenizer.py          # 新增
  test_bm25_index.py         # 新增
  test_hybrid_retriever.py   # 新增
  test_chunk_filter.py       # 新增（稠密/稀疏谓词一致性）
```

## 关键实现细节

### 1. RetrieverPort（domain/retriever_port.py）
```python
class RetrieverPort(Protocol):
    async def retrieve(self, *, query, tenant, route_version=None,
                       asset_id=None, doc_types=None, top_k=20) -> list[ChunkHit]: ...
```
与现 `VectorRetriever.retrieve` 签名一致 → 零行为改动。

### 2. ChunkFilter（infrastructure/chunk_filter.py）
- `build_where(tenant, route_version, asset_id, doc_types) -> dict`：与现 `_build_where` 等价（state=PUBLISHED + 等值 + $in）。
- `build_predicate(...) -> Callable[[dict], bool]`：对 chunk metadata dict 判定同一组条件。
- 单测断言两路对同一组参数产出一致命中集。

### 3. Bm25Index（infrastructure/bm25/bm25_index.py）
- 内部 `rank_bm25.BM25Okapi` + 并行存储 `chunk_id[]`、`tokens[]`、`meta[]`（含 text、locator、state、route_version、tenant_scope、doc_type、binding_asset_id、version_id、doc_id、section_type）。
- `build(chunks: list[DocumentChunk])`：全量构建（启动期从 `collection.get(where={"state":"PUBLISHED"})` 拉取）。
- `add(chunks)` / `remove(chunk_ids)` / `remove_by_version(version_id)`：增量维护（重建底层 BM25Okapi；MVP 语料万级可接受，后续可换增量 Okapi）。
- `search(query_tokens, *, predicate, top_k) -> list[(chunk_id, bm25_score)]`：先按 predicate 过滤候选，再 BM25 打分截断。
- 线程安全：查询/写入加 `asyncio.Lock`（BM25Okapi 非线程安全 + 重建期间不可读）。

### 4. Tokenizer（infrastructure/bm25/tokenizer.py）
- 优先 `jieba.cut_for_search`（中文 SOP/工艺文档必需）；`jieba` 不可用时降级为 `\w+` 正则（中英数字词）+ 小写。
- 停用词过滤（内置小停用表）。
- 单测：中文分词、英文术语保留、降级路径。

### 5. Bm25Retriever（infrastructure/bm25/bm25_retriever.py）
- 持有 `Bm25Index` + `Tokenizer`。
- `retrieve()`：复用 `ChunkFilter.build_predicate` 过滤 → `index.search` → 映射 `ChunkHit`（`score = bm25_score`，归一化到 [0,1] 仅供观察，融合只用排名）。

### 6. HybridRetriever（application/hybrid_retriever.py）
- 持有 `dense: RetrieverPort` + `sparse: RetrieverPort` + 融合参数。
- `retrieve()`：
  1. 两路并发 `asyncio.gather`，各自过取 `recall_candidate_k`（默认 50，> top_k）。
  2. RRF：`score(d) = dense_weight/(rrf_k + rank_dense(d)) + bm25_weight/(rrf_k + rank_bm25(d))`，单路命中按 0 计另一项。
  3. 按融合分降序，截断 `top_k`；`ChunkHit.score` = RRF 分。
  4. 任一路异常 → 降级为另一路（可观测记录），不整体失败。

### 7. 组合根装配（routes/document/__init__.py）
```
if settings.document.hybrid_recall_enabled:
    bm25_index = Bm25Index(tokenizer)
    await bm25_index.build_from_collection(container.chroma_collection)  # 全量拉 PUBLISHED
    chunk_repo = ChunkRepo(collection, bm25_index=bm25_index)            # 投影协作
    dense = VectorRetriever(collection, embedder)
    sparse = Bm25Retriever(bm25_index, tokenizer)
    retriever = HybridRetriever(dense, sparse, rrf_k, dense_w, bm25_w, candidate_k)
else:
    chunk_repo = ChunkRepo(collection)                  # 无投影
    retriever = VectorRetriever(collection, embedder)   # 现状
```
注意：`chunk_repo` 需在 ingestion/reindex 之前构造完成（已是现状顺序），确保后续写入同步进 BM25 投影。

### 8. 配置（DocSettings）
```python
hybrid_recall_enabled: bool = True
dense_weight: float = 1.0
bm25_weight: float = 1.0
rrf_k: int = 60              # RRF 常数（标准 60）
recall_candidate_k: int = 50 # 每路过取数，融合后截断到 top_k
```
`.env.example` 增补对应 `RAG_DOCUMENT__*` 样例。

### 9. 依赖（pyproject.toml）
```
"rank-bm25>=0.2",   # [B] BM25 稀疏检索
"jieba>=0.42",      # [B] 中文分词
```

## 不做的事（边界）
- 不引入 Elasticsearch/外部全文检索服务（保持嵌入式、车间网隔离友好；MVP 内存索引足够）。
- 不改动 rerank / LLM 综合链路（混合召回仅替换第一阶段）。
- 不改动 A/E 路线。
- 不做 BM25 索引落盘持久化（启动重建即可；ChromaDB 是唯一真相源）。

## 验收
- `pytest tests/` 全绿（含新增 4 个测试文件 + 既有用例不回归）。
- `hybrid_recall_enabled=false` 时行为与现状完全一致（纯稠密）。
- `hybrid_recall_enabled=true` 时：专有名词查询命中提升（BM25 贡献）、同义表达命中不丢（稠密贡献）。
- 稠密/稀疏过滤谓词一致性单测通过（防 DEPRECATED/版本/租户泄漏）。
- BM25 索引随 ingestion/soft_delete/delete_by_version 同步（投影一致性单测）。
