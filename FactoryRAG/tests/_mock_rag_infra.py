"""模拟数据加载器 + 测试伪造件（B 文档型 / A 追溯型 RAG）。

设计原则：
- **真实组件最大化**：切分用真实 ``ChunkStrategySelector``、稀疏检索用真实 ``Bm25Index``+
  ``jieba``、混合检索用真实 ``HybridRetriever``（RRF）、跨路线用真实 ``InProcessDocRagAdapter``。
- **伪造件只替代基础设施**：Embedding/ChromaDB/Neo4j/LLM/Reranker/Redis 用确定性内存版，
  满足各自 Port 契约，使应用/领域层逻辑被完整覆盖，且**零外部依赖**可跑。
- 与现有"重依赖懒导入、单测不触外部"口径一致：本模块 import 不触发 chromadb/neo4j/langchain。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.routes.document.application.chunking import ChunkStrategySelector
from app.routes.document.application.hybrid_retriever import HybridRetriever
from app.routes.document.application.retrieval_service import DocumentRetrievalService
from app.routes.document.domain.chunk import DocumentChunk
from app.routes.document.domain.document import DocumentCategory, DocType
from app.routes.document.infrastructure.bm25.bm25_index import Bm25Index
from app.routes.document.infrastructure.bm25.bm25_retriever import Bm25Retriever
from app.routes.document.infrastructure.bm25.tokenizer import Tokenizer
from app.routes.document.infrastructure.chromadb.retriever import VectorRetriever
from app.routes.traceability.domain.seed import Seed
from app.routes.traceability.domain.subgraph import TraceSubgraph
from app.shared.embedding.port import EmbeddingPort, RerankerPort
from app.shared.events.version_contract import VersionAnchor, VersionKind
from app.shared.tenant.context import TenantContext

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ──────────────────────────────────────────────────────────────────
# B 文档型：数据加载
# ──────────────────────────────────────────────────────────────────
def load_doc_manifest() -> list[dict[str, Any]]:
    """读 ``data/manifest.json`` 的文档清单。"""
    return json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))["documents"]


def load_doc_queries() -> list[dict[str, Any]]:
    """读 ``data/queries.json`` 的期望查询集。"""
    return json.loads((DATA_DIR / "queries.json").read_text(encoding="utf-8"))["queries"]


def load_doc_chunks() -> tuple[list[DocumentChunk], dict[str, dict[str, Any]]]:
    """manifest + markdown -> 真实切分 -> ``list[DocumentChunk]``。

    与 ``IngestionService`` 同路径（``ChunkStrategySelector.split``）；manifest 的 ``state``
    覆盖 chunk 状态以构造 DEPRECATED 样本（split 默认 PUBLISHED）。
    返回 (chunks, doc_id -> manifest entry)。
    """
    selector = ChunkStrategySelector()
    chunks: list[DocumentChunk] = []
    entries: dict[str, dict[str, Any]] = {}
    for entry in load_doc_manifest():
        text = (DATA_DIR / entry["file"]).read_text(encoding="utf-8")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        anchor = _anchor_from_entry(entry)
        doc_chunks = selector.split(
            text=text,
            doc_type=DocType(entry["doc_type"]),
            doc_id=entry["doc_id"],
            version_id=entry["version_id"],
            tenant_scope=entry["tenant_scope"],
            version_anchor=anchor,
            file_content_hash=content_hash,
        )
        state = entry.get("state", "PUBLISHED")
        for c in doc_chunks:
            c.state = state  # 覆盖状态（构造 DEPRECATED 样本）
        chunks.extend(doc_chunks)
        entries[entry["doc_id"]] = entry
    return chunks, entries


def _anchor_from_entry(entry: dict[str, Any]) -> VersionAnchor | None:
    """从 manifest entry 的 version_kind/version_ref_id/version 构造版本锚点。"""
    vk = entry.get("version_kind")
    ver = entry.get("version")
    if not vk or not ver:
        return None
    try:
        return VersionAnchor(
            kind=VersionKind(vk),
            ref_id=entry.get("version_ref_id", ""),
            version=ver,
        )
    except ValueError:
        return None


# ──────────────────────────────────────────────────────────────────
# B 伪造件：Embedding / ChromaDB / Reranker / LLM
# ──────────────────────────────────────────────────────────────────
class FakeEmbedder:
    """确定性 hash 词袋 embedder（满足 ``EmbeddingPort``）。

    复用真实 ``Tokenizer``（jieba 优先）分词，每个 token 哈希到 1024 维某一维并累加词频。
    同词文本 cosine 相似度高 -> 稠密路能按词项重叠召回（与 BM25 语义一致，足以驱动真实 RRF 融合）。
    """

    DIM = 1024

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self._tok = tokenizer or Tokenizer()

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one_sync(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed_one_sync(text)

    def _embed_one_sync(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in self._tok.tokenize(text):
            dim = int.from_bytes(hashlib.sha256(tok.encode("utf-8")).digest()[:4], "big") % self.DIM  # hashlib 而非 hash()：str hash 按进程随机(PYTHONHASHSEED)致稠密召回 flaky
            vec[dim] += 1.0
        return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _where_matches(meta: dict[str, Any], where: dict[str, Any]) -> bool:
    """模拟 ChromaDB ``where`` 过滤语义（与 ``ChunkFilter.to_where`` 对偶）。

    值为标量 -> 等值；值为 ``{"$in": [...]}`` -> 属于集合。
    """
    for k, v in where.items():
        val = meta.get(k)
        if isinstance(v, dict) and "$in" in v:
            if val not in v["$in"]:
                return False
        elif val != v:
            return False
    return True


class FakeChromaCollection:
    """内存 ChromaDB collection（满足 ``VectorRetriever`` 调用契约）。

    ``query`` 走 cosine 相似度 + ``where`` 过滤，返回结构与 ChromaDB 一致
    （``ids/documents/metadatas/distances`` 均为嵌套 list，距离 = 1 - cosine）。
    """

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._embeddings: list[list[float]] = []
        self._documents: list[str] = []
        self._metadatas: list[dict[str, Any]] = []

    async def add(self, chunks: list[DocumentChunk], embedder: FakeEmbedder) -> None:
        embeddings = await embedder.embed_batch([c.text for c in chunks])
        for c, emb in zip(chunks, embeddings):
            if c.chunk_id in self._ids:
                continue
            self._ids.append(c.chunk_id)
            self._embeddings.append(emb)
            self._documents.append(c.text)
            self._metadatas.append(c.to_metadata_dict())

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        qvec = query_embeddings[0]
        cand: list[tuple[float, int]] = []
        for i, emb in enumerate(self._embeddings):
            if where is not None and not _where_matches(self._metadatas[i], where):
                continue
            cand.append((_cosine(qvec, emb), i))
        cand.sort(reverse=True)
        cand = cand[:n_results]
        return {
            "ids": [[self._ids[i] for _, i in cand]],
            "documents": [[self._documents[i] for _, i in cand]],
            "metadatas": [[self._metadatas[i] for _, i in cand]],
            "distances": [[1.0 - sim for sim, _ in cand]],
        }


class FakeReranker:
    """透传精排（满足 ``RerankerPort``）：保序返回 (原索引, 1.0)。"""

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        return [(i, 1.0) for i in range(min(top_k, len(docs)))]


class _LLMResult:
    def __init__(self, content: str) -> None:
        self.content = content


class StubDocLLM:
    """B 桩 LLM：拼接 top chunk 摘要返回 ``.content``（满足 ``achat`` 契约）。"""

    async def achat(self, prompt: list[dict[str, str]]) -> Any:
        user = next((m["content"] for m in prompt if m["role"] == "user"), "")
        return _LLMResult(f"依据文档作答：{user[:80]}...")


# ──────────────────────────────────────────────────────────────────
# B 检索器/服务 构造
# ──────────────────────────────────────────────────────────────────
async def build_bm25(chunks: list[DocumentChunk]) -> tuple[Bm25Index, Bm25Retriever]:
    """真实 BM25：索引 + 检索器（rank_bm25 + jieba）。仅 PUBLISHED 入索引。"""
    index = Bm25Index(Tokenizer())
    await index.build_from_chunks(chunks)
    return index, Bm25Retriever(index)


async def build_hybrid_retriever(chunks: list[DocumentChunk]) -> HybridRetriever:
    """真实 HybridRetriever：真实 BM25 稀疏 + 真实 VectorRetriever 稠密（FakeEmbedder+FakeCollection）。"""
    published = [c for c in chunks if c.state == "PUBLISHED"]
    embedder = FakeEmbedder()
    collection = FakeChromaCollection()
    await collection.add(published, embedder)
    dense = VectorRetriever(collection=collection, embedder=embedder)
    _, sparse = await build_bm25(chunks)
    return HybridRetriever(dense=dense, sparse=sparse, rrf_k=60, candidate_k=50)


def build_doc_svc(retriever: Any, *, top_k: int = 20, top_n: int = 5) -> DocumentRetrievalService:
    """真实 ``DocumentRetrievalService``：真实 retriever + 透传 reranker + 桩 LLM + 无缓存。"""
    from unittest.mock import MagicMock

    return DocumentRetrievalService(
        retriever=retriever,
        reranker=FakeReranker(),
        llm=StubDocLLM(),
        redis=None,
        cache_ttl=300,
        obs=MagicMock(),
        top_k=top_k,
        top_n=top_n,
    )


# ──────────────────────────────────────────────────────────────────
# A 追溯型：数据加载 + 伪造件
# ──────────────────────────────────────────────────────────────────
def load_trace_scenarios() -> list[dict[str, Any]]:
    """读 ``data/trace/scenarios.json``。每场景含 seed/subgraph/expected。"""
    return json.loads((DATA_DIR / "trace" / "scenarios.json").read_text(encoding="utf-8"))["scenarios"]


class FakeGraphRetriever:
    """A 图检索器伪造件：按 seed 返回 mock ``TraceSubgraph``。

    满足 ``TraceRetrievalService`` 依赖的 ``expand_5m1e`` 契约；``version`` 非空且 kind=route
    时过滤 Method 维度快照节点（模拟按版本历史回溯）。真实 Neo4j Cypher 多跳执行不在覆盖范围。
    """

    def __init__(self, scenarios: list[dict[str, Any]] | None = None) -> None:
        self._scenarios = {f"{s['seed']['kind']}:{s['seed']['value']}": s for s in (scenarios or load_trace_scenarios())}

    async def expand_5m1e(
        self, seed: Seed, as_of: Any, tenant: TenantContext, *,
        version: str | None = None, version_kind: str | None = None,
    ) -> TraceSubgraph:
        key = f"{seed.kind.value}:{seed.value}"
        scenario = self._scenarios.get(key)
        if scenario is None:
            raise KeyError(f"无 mock 场景: {key}")
        sub = TraceSubgraph.model_validate(scenario["subgraph"])
        if version and (not version_kind or version_kind == "route"):
            sub.clusters.method = [
                n for n in sub.clusters.method if n.props.get("route_version") == version
            ]
        return sub


class FakeSubgraphRepo:
    """内存子图持久化（仅满足 ``save`` 调用，不断言）。"""

    def __init__(self) -> None:
        self.saved: list[TraceSubgraph] = []

    async def save(self, subgraph: TraceSubgraph) -> None:
        self.saved.append(subgraph)


_NODE_ID_RE = re.compile(r"- [A-Za-z]+:(\S+)")


class StubTraceLLM:
    """A 桩 LLM：从子图 prompt 抽取**真实** node_id 作证据，返回 JSON 假设（禁实体幻觉）。

    按 defect_code 选根因维度：SW-001 锡桥 -> Material；SW-002 立碑 -> Method。
    statement 含"回流焊/锡膏"词项，使 A->B 跨路线拉 SOP 时能命中工艺绑定型 SOP。
    ``confidence`` 可调（默认 0.8 不转人工；设 0.3 触发低置信转人工）。
    """

    def __init__(self, confidence: float = 0.8) -> None:
        self._confidence = confidence

    async def achat(self, prompt: list[dict[str, str]]) -> Any:
        user = next((m["content"] for m in prompt if m["role"] == "user"), "")
        node_ids = list(dict.fromkeys(_NODE_ID_RE.findall(user)))  # 去重保序
        defect_match = re.search(r"defect_code['\"]?\s*:\s*['\"]?(SW-\d{3})", user)
        defect_code = defect_match.group(1) if defect_match else "SW-001"

        if defect_code == "SW-002":
            category, statement = "Method", "回流焊立碑缺陷，疑似工艺参数与贴片偏移问题"
        else:
            category, statement = "Material", "回流焊锡桥缺陷，疑似锡膏批次工艺参数问题"

        evidence = [f"node_id={nid}" for nid in node_ids[:2]]
        if not evidence:
            evidence = [f"defect_code={defect_code}"]
        data = {
            "summary": f"{defect_code} 根因假设（基于 5M1E 子图）",
            "confidence": self._confidence,
            "hypotheses": [
                {
                    "category": category,
                    "rank": 1,
                    "statement": statement,
                    "evidence": evidence,
                    "suggested_action": "核查批次与工艺参数",
                }
            ],
        }
        return _LLMResult(json.dumps(data, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────
# A 服务 + 跨路线 A->B 构造
# ──────────────────────────────────────────────────────────────────
def build_trace_svc(
    *,
    retriever: FakeGraphRetriever,
    llm: StubTraceLLM | None = None,
    doc_rag: Any = None,
    seed_resolver: Any = None,
) -> "TraceRetrievalService":
    """真实 ``TraceRetrievalService``：FakeGraphRetriever + 真实 SeedResolver + 桩 LLM + 内存 repo。

    SeedResolver 用真实实现（正则路径不触 Neo4j/Embedding/LLM）；driver/embedder/llm 传
    MagicMock 仅满足构造，正则命中时不被调用。``doc_rag`` 注入真实 B Port 时启用跨路线富化。
    """
    from unittest.mock import MagicMock

    from app.routes.traceability.application.seed_resolver import SeedResolver
    from app.routes.traceability.application.trace_retrieval_service import TraceRetrievalService

    if seed_resolver is None:
        seed_resolver = SeedResolver(llm=MagicMock(), embedder=MagicMock(), driver=MagicMock())
    return TraceRetrievalService(
        retriever=retriever,
        seed_resolver=seed_resolver,
        llm=llm or StubTraceLLM(),
        subgraph_repo=FakeSubgraphRepo(),
        redis=None,
        cache_ttl=300,
        doc_rag=doc_rag,
        obs=MagicMock(),
    )


async def build_doc_rag_port():
    """真实 B DocRagPort：``InProcessDocRagAdapter`` 包裹真实 ``DocumentRetrievalService``（hybrid over mock SOP）。

    用于 A->B 跨路线测试：A 经此 Port 拉 SOP 片段，验证版本一致性三段链第一段。
    """
    from app.shared.acl.adapters import InProcessDocRagAdapter

    chunks, _ = load_doc_chunks()
    retriever = await build_hybrid_retriever(chunks)
    svc = build_doc_svc(retriever)
    return InProcessDocRagAdapter(svc)
