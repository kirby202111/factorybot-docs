"""走项目原生路径跑 5M1E 排序：get_container -> diagnosis_service.diagnose。

RUN_MODE=mock（ACL/存储走 fixture）+ LLM_PROVIDER=deepseek（真实 DeepSeek）。
真实 LLM 驱动 ReAct 循环调 mock 工具取证 -> 5M1E 排序 -> 项目 _parse_report + _guard_no_evidence。

注意：不导入 conftest（conftest 会强制 LLM_PROVIDER=mock 覆盖 .env）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# CWD 切到 factorybot/，使 .env 与 data/ fixtures 可解析（同 conftest 做法）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # factorybot/
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# 真实 DeepSeek 多轮 ReAct 比 mock 慢，放宽整体超时（默认 60s 太紧）
os.environ.setdefault("DIAGNOSIS_SESSION_TIMEOUT", "300")


async def main() -> None:
    from app.container import get_container

    c = get_container()
    s = c.settings

    # ---- 1. 确认配置真实生效（不是 mock）----
    print("=" * 70)
    print("【配置确认】")
    print("=" * 70)
    print(f"  LLM_PROVIDER = {s.llm_provider}")
    print(f"  LLM_MODEL    = {s.llm_model}")
    print(f"  LLM_BASE_URL = {s.llm_base_url}")
    print(f"  LLM_API_KEY  = {'set(len=' + str(len(s.llm_api_key)) + ')' if s.llm_api_key else 'EMPTY'}")
    print(f"  RUN_MODE     = {s.run_mode}   is_mock={s.is_mock}")
    llm_name = getattr(c.llm, "name", "?")
    print(f"  c.llm.name   = {llm_name}")
    real = "deepseek" in llm_name.lower() or s.llm_provider == "deepseek"
    print(f"  >>> 真实 LLM? {'YES (DeepSeek)' if real else 'NO (mock!)'}")
    if not real:
        print("  !! LLM 未走真实 provider，中止。检查 .env / 环境变量覆盖。")
        return

    # ---- 1.5 暂时去掉图数据的使用（仅本脚本运行，不动 app 代码；恢复时删除此块）----
    # 图数据 = query_traceability_graph 工具 + 系统提示里"先调追溯图"指令。
    # 1) 注销追溯图工具：model_node 每步经 registry.tools_for() 取工具，pop 后 LLM 看不到它。
    c.diagnosis_registry._descriptors.pop("query_traceability_graph", None)
    # 2) 覆盖系统提示：diagnosis_prompt 每步重读 DIAGNOSIS_SYSTEM_PROMPT 模块全局，
    #    去掉"先调追溯图"指令，改为按需调只读 REST 取证。
    from app.infrastructure.ai import graph_builder as _gb
    _gb.DIAGNOSIS_SYSTEM_PROMPT = _gb.DIAGNOSIS_SYSTEM_PROMPT.replace(
        "2. 先调追溯图（query_traceability_graph）建立全链路视图，再按需降级查只读 REST。\n",
        "2. 按需调只读 REST 工具取证（过点记录 / 测试结果 / 工艺 / 物料 / 设备 等）。\n",
    )
    print("  [临时] 已注销 query_traceability_graph，图数据不参与本次诊断")

    # ---- 2. 跑诊断（真实 DeepSeek + mock fixture 证据）----
    tenant = c.default_tenant()
    print("\n" + "=" * 70)
    print("【运行】diagnosis_service.diagnose(SN-2026-6544)")
    print("  真实 DeepSeek 驱动 ReAct -> 调 mock 工具取证 -> 5M1E 排序 -> 护栏")
    print("=" * 70 + "\n")

    report = await c.diagnosis_service.diagnose(
        "单件 SN-2026-6544 焊接不良根因", tenant, serial_no="SN-2026-6544",
    )

    # ---- 3. 工具调用痕迹（证明 ReAct 真的调了 mock 工具）----
    traces = c.tool_trace_repo._rows if hasattr(c.tool_trace_repo, "_rows") else []
    print(f"[工具调用] 共 {len(traces)} 次:")
    for t in traces:
        args = getattr(t, "input_payload", None) or {}
        ctx = getattr(t, "bounded_context", "")
        print(f"  - {t.tool_name}  [{ctx}]  args={json.dumps(args, ensure_ascii=False)}")

    # ---- 4. 最终报告 ----
    d = report.model_dump()
    print("\n" + "=" * 70)
    print("【最终 DiagnosisReport】")
    print("=" * 70)
    print(json.dumps(d, ensure_ascii=False, indent=2))

    # ---- 5. 5M1E 排序汇总 ----
    print("\n" + "=" * 70)
    print("【5M1E 排序（按 rank）】")
    print("=" * 70)
    hyps = sorted(d.get("hypotheses", []), key=lambda h: h.get("rank", 99))
    if not hyps:
        print("  (无假设--报告被护栏清空或 LLM 未给出)")
    for h in hyps:
        print(f"  rank={h['rank']}  [{h['category']}]")
        print(f"    假设: {h['statement']}")
        print(f"    证据: {h.get('evidence')}")
        print(f"    建议: {h.get('suggested_action')}")
    print(f"\n  summary: {d.get('summary')}")
    print(f"  confidence={d.get('confidence')}  needs_human_review={d.get('needs_human_review')}")


if __name__ == "__main__":
    asyncio.run(main())
