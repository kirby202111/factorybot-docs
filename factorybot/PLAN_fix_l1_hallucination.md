# 修复:L1 诊断对未知 serial_no 的幻觉

## 根因(两层)
1. **mock 数据冒充**:`fixture_loader.lookup` 在 key 未命中时回退到 `_default`,把 `SN-DEFAULT` 的良性数据当作 Q123 的结果返回给 LLM。
2. **提示无护栏**:`L1_SYSTEM_PROMPT` 没有"证据不足"规则,LLM 从 question 里的"焊接不良"反推不良存在,并编造 `SN-2026-001234`/`B-2026-0701` 等工具未返回的具体值。

## Layer 1 — mock 数据诚实(根因)
- `app/infrastructure/mock/fixture_loader.py` `lookup`:加 `allow_default: bool = True` 参数。`allow_default=False` 且 key 未命中时返回 `None`(不再回退 `_default`、不再返回整文件)。
- `app/infrastructure/acl/base.py` `_get`:加 `allow_default: bool = False`(读默认不冒充,镜像真实 REST 404),透传给 `lookup`。`_post`(写)不变,仍用 `_default` 返回成功响应。
- `app/infrastructure/acl/views.py` `to_view`:`dto is None` 时返回空视图(必填 `str` 字段填 `""`,其余走默认)——集中处理所有单视图客户端的未命中。
- list 型客户端 None 兜底:
  - `pass_execution.py` 两处 `else dto` -> `else (dto or [])`
  - `rag.py` `fetch_subgraph_nodes` 的 `nodes=dto` -> `nodes=(dto or [])`
  - `doc_rag.py` `docs = dto` -> `docs = dto or []`(它用 `fixture_key="_default"` 实际不会 None,仅防御)

> 显式以 `_default` 为 key 的客户端(doc_rag / equipment_telemetry / quality)不受影响——key 命中,不走 fallback。

## Layer 2 — LLM 提示护栏(纵深防御)
- `app/infrastructure/ai/graph_builder.py` `L1_SYSTEM_PROMPT`:加规则 6——工具返回无 BLOCK/FAIL/不良节点等异常证据时,`summary` 须写"证据不足,未发现异常",`hypotheses=[]`,`confidence=0.0`,`needs_human_review=true`;严禁从 question 反推不良,严禁编造 serial_no/批次号/设备号。

## Layer 3 — 回归测试
- `tests/test_l1_diagnosis.py`:加 ACL 数据诚实测试——未知 SN 返回空(节点 `[]`、过点记录 `[]`),不冒充 `SN-DEFAULT`。纯 ACL,无 LLM,CI 可跑。

## 影响面
- 现有测试全用真实 key(`SN-2026-001234`/`ASSET-01`/`WO-2026-0701`),不受 `allow_default=False` 影响。
- `MockChatModel` 不读 prompt 规则、不依赖 `_default`,prompt 改动不影响 mock 测试。
- 显式 `_default` key 的客户端不受影响。

## 验证
1. `pytest` 全绿。
2. 重启服务,curl `{"question":"Q123 焊接不良根因","serial_no":"Q123"}` -> 预期 `summary` 声明证据不足、`hypotheses=[]`、`confidence=0.0`、`needs_human_review=true`,不再出现 `SN-2026-001234`/`B-2026-0701`。
