"""三路线模块（各含 application/domain/infrastructure）。

- ``traceability/``：A 追溯型（GraphRAG 全链路追溯，5M1E 根因串联）
- ``document/``：B 文档型（SOP/手册/标准/8D 向量检索 + 事件驱动重索引）
- ``agentic/``：E Agentic RAG（L0 收口入口，意图路由到 A/B + 委托 agent-service L1/L2）

路线间禁止直接 import 对方 application/domain，一律走 ``shared/acl/`` 的 Port。
"""
