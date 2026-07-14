"""Mock 基础设施 + data/ 测试数据 + 真实 DeepSeek LLM 的端到端 RAG 运行器。

零 docker / 零外部基础设施（无 MySQL/Neo4j/ChromaDB/Redis/Kafka/MinIO）：
- 检索：真实 BM25（rank_bm25+jieba）稀疏 + FakeEmbedder/FakeChromaCollection 稠密
        + HybridRetriever（RRF 融合）—— 复用 tests/_mock_rag_infra.py 的构建器。
- 数据：data/documents + manifest.json（与 IngestionService 同路径 ChunkStrategySelector 切分）。
- LLM：真实 DeepSeek（llm_factory 走 .env 的 RAG_LLM__* 密钥），替换测试桩 StubDocLLM。
        ObservableChatModel.achat -> ChatResult{.content} 与桩 LLM 签名兼容，服务层零改动。

用法：
  uv run python scripts/run_mock_rag.py                       # 跑 data/queries.json 全量
  uv run python scripts/run_mock_rag.py "回流焊峰值温度设多少"  # 单条自定义查询
  uv run python scripts/run_mock_rag.py "..." --category PROCESS_BOUND --route-version v3
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# 复用 tests/ 下的 mock 构建器（pytest 之外需手动把 tests/ 与仓库根加进 sys.path）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

# Windows 控制台默认 GBK，强制 stdout/stderr 走 UTF-8，避免中文打印乱码。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from _mock_rag_infra import (  # noqa: E402
    FakeReranker,
    build_hybrid_retriever,
    load_doc_chunks,
    load_doc_queries,
)
from app.config import load_settings  # noqa: E402
from app.routes.document.application.retrieval_service import DocumentRetrievalService  # noqa: E402
from app.routes.document.domain.answer import DocAnswer, DocQuery  # noqa: E402
from app.routes.document.domain.document import DocumentCategory  # noqa: E402
from app.shared.ai.llm_factory import llm_factory  # noqa: E402
from app.shared.tenant.context import TenantContext  # noqa: E402

# 全车间租户：PCBA + BOX 全可见（tenant_scope 过滤不误伤任何 mock 文档）。
TENANT = TenantContext(tenant_id="t-mock", tenant_scopes=["workshop:PCBA", "workshop:BOX", "line:SMT-1"])


def _build_real_llm() -> Any:
    """按 .env 的 LlmSettings 构造真实 DeepSeek ObservableChatModel。

    obs 用 MagicMock：满足 llm_span（上下文管理器）+ record_llm 调用契约，
    与 tests/_mock_rag_infra.py 的桩构造口径一致；不引入真实可观测后端。
    """
    settings = load_settings()
    llm = settings.llm
    if not llm.api_key or llm.api_key == "changeme":
        raise SystemExit(
            "未配置 DeepSeek API key：请在 FactoryRAG/.env 设置 RAG_LLM__API_KEY 后重试。"
        )
    print(f"[LLM] provider={llm.provider} model={llm.model_name} base_url={llm.base_url}")
    return llm_factory(llm, obs=MagicMock())


async def build_svc() -> DocumentRetrievalService:
    """mock 检索 + 真实 LLM 的 DocumentRetrievalService（索引只建一次，复用查询）。"""
    chunks, _ = load_doc_chunks()
    print(f"[data] 切分出 {len(chunks)} 个 chunk（PUBLISHED 入检索索引）")
    retriever = await build_hybrid_retriever(chunks)
    real_llm = _build_real_llm()
    return DocumentRetrievalService(
        retriever=retriever,
        reranker=FakeReranker(),
        llm=real_llm,
        redis=None,          # 无缓存：每次都走真实检索 + 真实 LLM
        cache_ttl=300,
        obs=MagicMock(),
        top_k=20,
        top_n=5,
    )


def _print_answer(idx: int, q: dict[str, Any], answer: DocAnswer) -> None:
    print(f"\n{'=' * 72}")
    print(f"[{idx}] Q: {q['query']}")
    print(f"    category={q.get('doc_category')} route_version={q.get('route_version')} "
          f"asset_id={q.get('asset_id')}")
    print(f"    A: {answer.answer}")
    print(f"    confidence={answer.confidence} route_version_filter={answer.route_version_filter} "
          f"needs_human_review={answer.needs_human_review}")
    if answer.citations:
        print("    citations:")
        for c in answer.citations:
            print(f"      - doc={c.document_id} v={c.version_no} :: {c.quoted_text}")


async def run_queries(svc: DocumentRetrievalService) -> None:
    for i, q in enumerate(load_doc_queries(), 1):
        req = DocQuery(
            question=q["query"],
            doc_category=DocumentCategory(q["doc_category"]),
            route_version=q.get("route_version"),
            asset_id=q.get("asset_id"),
            top_k=20,
            top_n=5,
        )
        answer = await svc.query(req, TENANT)
        _print_answer(i, q, answer)


async def run_single(svc: DocumentRetrievalService, args: argparse.Namespace) -> None:
    req = DocQuery(
        question=args.question,
        doc_category=DocumentCategory(args.category),
        route_version=args.route_version,
        asset_id=args.asset_id,
        top_k=20,
        top_n=5,
    )
    answer = await svc.query(req, TENANT)
    _print_answer(1, {"query": args.question, "doc_category": args.category,
                      "route_version": args.route_version, "asset_id": args.asset_id}, answer)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock 基础设施 + 真实 DeepSeek LLM 的 RAG 运行器")
    parser.add_argument("question", nargs="?", help="自定义查询；省略则跑 data/queries.json 全量")
    parser.add_argument("--category", default="GENERAL",
                        choices=[c.value for c in DocumentCategory],
                        help="文档类别（PROCESS_BOUND 需 --route-version，ASSET_BOUND 需 --asset-id）")
    parser.add_argument("--route-version", default=None, help="工艺版本（PROCESS_BOUND 必填）")
    parser.add_argument("--asset-id", default=None, help="设备 ID（ASSET_BOUND 必填）")
    args = parser.parse_args()

    svc = asyncio.run(build_svc())
    if args.question:
        asyncio.run(run_single(svc, args))
    else:
        asyncio.run(run_queries(svc))


if __name__ == "__main__":
    main()
