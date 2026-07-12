"""防腐层（ACL）：对 MES 14 个限界上下文 + RAG 服务的只读 / 受限写客户端。

边界即工具边界：每个 ACL client 对应一个限界上下文暴露的只读/受限写接口。
- 只读 client：httpx GET，自动注入 X-Tenant-* + W3C traceparent。
- 受限写 client：必须带 confirmation token，header X-Confirmation-Token + X-Confirmed-By。
mock 模式下全部从 data/ fixtures 读，无需真实 MES/RAG。
"""
