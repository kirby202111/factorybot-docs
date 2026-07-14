"""A 追溯型（5M1E 根因分析）：mock 子图 + data/ 场景 + 真实 DeepSeek LLM 的端到端运行器。

零 docker / 零外部基础设施（无 Neo4j/MySQL/Redis/Kafka）：
- 子图：FakeGraphRetriever 按 seed 返回 data/trace/scenarios.json 的 mock TraceSubgraph
        （与领域模型同构）；version 从 Method 维度 RouteVersion 快照节点取（非当前 ACTIVE）。
- LLM：真实 DeepSeek（llm_factory 走 .env 的 RAG_LLM__* 密钥），替换测试桩 StubTraceLLM。
        服务层 _synthesize 直接 json.loads(result.content)，真实 LLM 常带 ```json``` 围栏会
        撑爆解析 -> 用 JsonLlm wrapper 强化 prompt + 清洗 .content，不改动服务契约。
- 跨路线 A->B：复用 build_doc_rag_port()（B 桩检索）拉 v3/v4 SOP 片段富化 suggested_action，
        验证版本一致性三段链第一段（图锁版本 -> B 按版本过滤召回）。

用法：
  uv run python scripts/run_mock_trace.py                  # 跑 data/trace/scenarios.json 全量
  uv run python scripts/run_mock_trace.py SN-2024-001      # 单个 SN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

# Windows 控制台默认 GBK，强制 stdout/stderr 走 UTF-8，避免中文打印乱码。
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from _mock_rag_infra import (  # noqa: E402
    FakeGraphRetriever,
    build_doc_rag_port,
    build_trace_svc,
    load_trace_scenarios,
)
from app.config import load_settings  # noqa: E402
from app.routes.traceability.domain.answer import TraceAnswer  # noqa: E402
from app.routes.traceability.domain.seed import Seed, SeedKind, TraceQuery  # noqa: E402
from app.shared.ai.llm_factory import llm_factory  # noqa: E402
from app.shared.ai.observable_chat_model import ChatResult  # noqa: E402
from app.shared.tenant.context import TenantContext  # noqa: E402

TENANT = TenantContext(tenant_id="t-mock", tenant_scopes=["workshop:PCBA", "line:SMT-1"])

# 追加给 system 消息的硬约束：枚举值 + 纯 JSON + 证据格式（服务层 prompt 不便改，在 wrapper 注入）。
_TRACE_HINT = (
    "\n硬性约束："
    "1) category 必须取以下之一：Man/Machine/Material/Method/Measurement/Environment（大小写敏感）；"
    "2) evidence 必须是字符串数组 list[str]，每项形如 node_id=<子图中真实出现的节点id> 或 "
    "defect_code=<码>；即使只有一项也必须写成数组（如 [\"node_id=InventoryBatch:B-77\"]），"
    "严禁编造子图中不存在的节点（禁实体幻觉）；"
    "3) 只输出一个纯 JSON 对象，禁止 markdown 代码围栏、禁止任何解释文字。"
)


class JsonLlm:
    """包真实 ObservableChatModel：强化 prompt + 清洗 .content，保证 json.loads 稳定。

    A 追溯服务 ``_synthesize`` 直接 ``json.loads(result.content)``；真实 LLM 常以
    ```` ```json ... ``` ```` 包裹或夹带解释，会撑爆解析。本 wrapper：
    ① 给 system 消息追加枚举/纯 JSON/证据格式约束；
    ② 去围栏、抽取首个 ``{...}`` 块。
    不改动服务层契约（仍 achat -> .content 字符串）。
    """

    _JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def achat(self, messages: list[Any], **kwargs: Any) -> ChatResult:
        result = await self._inner.achat(self._augment(messages), **kwargs)
        return ChatResult(
            content=self._sanitize(result.content),
            total_tokens=result.total_tokens,
            model=result.model,
            raw=result.raw,
        )

    @staticmethod
    def _augment(messages: list[Any]) -> list[Any]:
        out: list[Any] = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                out.append({**m, "content": m["content"] + _TRACE_HINT})
            else:
                out.append(m)
        return out

    @classmethod
    def _sanitize(cls, text: str) -> str:
        text = (text or "").strip()
        # 去 markdown 代码围栏（首尾）
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
        # 若非纯 JSON（夹带前后解释），抽取首个 {...} 块
        if not text.startswith("{"):
            m = cls._JSON_RE.search(text)
            if m:
                text = m.group(0)
        return cls._repair(text)

    @classmethod
    def _repair(cls, json_str: str) -> str:
        """修常见 LLM 形状错误，保证服务层 json.loads + model_validate 通过。失败原样返回。

        - evidence 标量 -> 单元素数组（LLM 单条证据常退化成字符串）
        - category 大小写归一到枚举值（Man/Machine/Material/Method/Measurement/Environment）
        """
        try:
            data = json.loads(json_str)
        except Exception:
            return json_str
        valid_cats = {"Man", "Machine", "Material", "Method", "Measurement", "Environment"}
        for h in data.get("hypotheses", []) or []:
            ev = h.get("evidence")
            if isinstance(ev, str):
                h["evidence"] = [ev]
            elif ev is None:
                h["evidence"] = []
            cat = h.get("category")
            if isinstance(cat, str) and cat not in valid_cats:
                cap = cat.capitalize()
                if cap in valid_cats:
                    h["category"] = cap
        return json.dumps(data, ensure_ascii=False)


def _build_real_llm() -> Any:
    settings = load_settings()
    llm = settings.llm
    if not llm.api_key or llm.api_key == "changeme":
        raise SystemExit("未配置 DeepSeek API key：请在 FactoryRAG/.env 设置 RAG_LLM__API_KEY 后重试。")
    print(f"[LLM] provider={llm.provider} model={llm.model_name} base_url={llm.base_url}")
    return llm_factory(llm, obs=MagicMock())


async def build_svc() -> Any:
    """FakeGraphRetriever + 真实 DeepSeek(JsonLlm 包) + 跨路线 B doc_rag 的 TraceRetrievalService。"""
    real_llm = _build_real_llm()
    json_llm = JsonLlm(real_llm)
    doc_rag = await build_doc_rag_port()  # B 桩检索：拉 v3/v4 SOP 片段富化 suggested_action
    return build_trace_svc(retriever=FakeGraphRetriever(), llm=json_llm, doc_rag=doc_rag)


def _evidence_node_ids(answer: TraceAnswer) -> list[str]:
    """从 evidence 抽 node_id=xxx 的节点 id（用于与 expected 比对）。"""
    ids: list[str] = []
    for h in answer.hypotheses:
        for ev in h.evidence:
            if ev.startswith("node_id="):
                ids.append(ev.split("=", 1)[1])
    return ids


def _print_scenario(idx: int, scenario: dict[str, Any], answer: TraceAnswer) -> None:
    exp = scenario["expected"]
    seed_val = scenario["seed"]["value"]
    print(f"\n{'=' * 72}")
    print(f"[{idx}] SN={seed_val}  缺陷={exp['defect_code']} {exp['defect_name']}")
    print(f"    seed={scenario['seed']['kind']}:{seed_val}  "
          f"locked version={answer.version} ({answer.version_kind})  (期望 {exp['version_locked']}) "
          f"{'✅' if answer.version == exp['version_locked'] else '❌'}")
    print(f"    subgraph_ref={answer.subgraph_ref}")
    print(f"    summary: {answer.summary}")
    print(f"    confidence={answer.confidence}  needs_human_review={answer.needs_human_review}")
    if not answer.hypotheses:
        print("    hypotheses: <空>（LLM 综合失败或未产出有效假设）")
        return
    actual_ids = _evidence_node_ids(answer)
    exp_ids = exp["evidence_node_ids"]
    hit = [nid for nid in exp_ids if nid in actual_ids]
    # LLM 常给出多维度假设，期望类别是“首要根因”，只要出现在任一假设中即算命中。
    exp_cat = exp["root_cause_category"]
    cat_ranks = [h.rank for h in answer.hypotheses if h.category.value == exp_cat]
    cat_mark = f"出现于 rank={cat_ranks} ✅" if cat_ranks else "未出现 ❌"
    print(f"    期望根因类别 {exp_cat}: {cat_mark}")
    print(f"    evidence 命中期望节点: {hit}/{exp_ids}  实际={actual_ids}")
    for h in answer.hypotheses:
        print(f"    [rank={h.rank}] category={h.category.value}")
        print(f"      statement: {h.statement}")
        print(f"      evidence: {h.evidence}")
        print(f"      suggested_action: {h.suggested_action}")


async def run_scenarios(svc: Any, only: str | None) -> None:
    scenarios = load_trace_scenarios()
    for i, sc in enumerate(scenarios, 1):
        if only and sc["seed"]["value"] != only:
            continue
        exp = sc["expected"]
        question = f"{sc['seed']['value']} 出现 {exp['defect_name']}，请基于 5M1E 子图分析根因"
        req = TraceQuery(
            question=question,
            seed=Seed(kind=SeedKind(sc["seed"]["kind"]), value=sc["seed"]["value"]),
        )
        answer = await svc.query(req, TENANT)
        _print_scenario(i, sc, answer)


def main() -> None:
    parser = argparse.ArgumentParser(description="A 追溯型 5M1E 根因分析：mock 子图 + 真实 DeepSeek LLM")
    parser.add_argument("sn", nargs="?", help="指定单个 SN（如 SN-2024-001）；省略则跑全量场景")
    args = parser.parse_args()

    svc = asyncio.run(build_svc())
    asyncio.run(run_scenarios(svc, args.sn))


if __name__ == "__main__":
    main()
