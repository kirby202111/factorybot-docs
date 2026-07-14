"""可观测底座（横切 · obs/）。

五层可观测模型：基础设施 -> 链路(trace/span) -> 指标(prometheus)
-> 业务视图(MySQL 平铺证据链) -> 评测质量。
Trace 双存储：Tempo/Jaeger（SRE 火焰图）+ MySQL tool_call_trace（工程师 UI 证据链），
同源 trace_id 串联。观测是只读旁路，失败不反噬业务。
"""
