"""成本优化横切（cost/）。

降本五层杠杆（优先级从严到宽）：
架构层（代码节点不调 LLM）> 模型层（分层路由）> 提示词层（prompt caching）
> 推理循环层（结果压缩/早停）> 缓存层（工具结果缓存）。
换便宜模型是最后一步，且必须过 EvalGate。

接入现状（2026-07-15）：
- ResultCompactor 已接入主流程：react_graph._compact_tool_history 在喂 LLM 前压缩
  history 中 tool 消息的 data（state.messages 仍存全文，护栏 _guard_no_evidence 与
  trace 读全文不受影响）。
- 其余 6 组件仍悬空：ModelRouter.route() 待 LLM 单例->多实例改造 + provider 模型映射
  + EvalGate 评测数据源就绪；CacheControl 强依赖 anthropic provider；EarlyStop 需 ReAct
  state 证据计数通道；PhaseToolBinder/ToolResultCache 默认关闭/灰度。详见优化与待办清单 #25。
"""
from app.infrastructure.cost.cache_control import CacheControl
from app.infrastructure.cost.early_stop import EarlyStopDetector
from app.infrastructure.cost.eval_gate import EvalGate
from app.infrastructure.cost.model_router import ModelRouter
from app.infrastructure.cost.phase_tool_binder import PhaseToolBinder
from app.infrastructure.cost.result_compactor import ResultCompactor
from app.infrastructure.cost.tool_result_cache import ToolResultCache

__all__ = [
    "CacheControl", "EarlyStopDetector", "EvalGate", "ModelRouter",
    "PhaseToolBinder", "ResultCompactor", "ToolResultCache",
]
