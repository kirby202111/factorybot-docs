"""shared/web -- FastAPI 公共底座。

- ``container``：DI 容器，注册 LLM/Embedding/各 Port 的 Adapter 绑定（组合根）；
- ``lifespan``：启动断言 -> 存储就绪探测 -> 按路线开关启停 consumer/router；
- ``health``：``/health`` / ``/ready`` / ``/metrics`` 三端点。

口径见《rag-service-整体结构设计》§3.9、《技术选型和实现方案》§2.9/§4。
"""
from app.shared.web.container import Container
from app.shared.web.health import HealthRouter
from app.shared.web.lifespan import lifespan

__all__ = ["Container", "lifespan", "HealthRouter"]
