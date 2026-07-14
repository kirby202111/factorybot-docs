"""长程任务横切能力。

核心机制：interrupt(value=card) 保存 checkpoint -> 抛 GraphInterrupt（控制流信号，非错误）
-> ainvoke 等待；Command(resume=token) 从 checkpoint 加载 -> 反序列化 state -> 续跑。
Pod 被 OOM Kill / 滚动更新后，新 Pod 同一 thread_id 调 ainvoke(Command(resume=…)) 即可跨进程恢复。

不引入 Celery（太重，且 interrupt 需同进程协程语义）。

实现分布：
- GateManager：app/orchestration/code_nodes/gate.py
- FailureTracker：app/orchestration/code_nodes/barrier.py
- ConfirmationStore：app/infrastructure/redis_/confirmation_store.py
- ActionCardDispatcher：app/application/action_card_dispatcher.py
- OrchestrationService：app/application/orchestration_service.py
- SqlSaver/MemorySaver：app/infrastructure/persistence/checkpointer.py
"""
from app.infrastructure.longtask.session_manager import SessionManager

__all__ = ["SessionManager"]
