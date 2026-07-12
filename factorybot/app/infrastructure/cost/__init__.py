"""成本优化横切（cost/）。

降本五层杠杆（优先级从严到宽）：
架构层（代码节点不调 LLM）> 模型层（分层路由）> 提示词层（prompt caching）
> 推理循环层（结果压缩/早停）> 缓存层（工具结果缓存）。
换便宜模型是最后一步，且必须过 EvalGate。
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
