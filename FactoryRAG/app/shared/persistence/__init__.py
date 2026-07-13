"""shared/persistence -- 多存储 Engine 工厂 + DeclarativeBase + Alembic。

单进程同时持有 Neo4j driver + ChromaDB client + MySQL asyncmy + Redis client，
连接池分别配额，lifespan 启动期做就绪探测，任一不可用按路线降级（§3.3）。

口径见《rag-service-整体结构设计》§3.7、《技术选型和实现方案》§2.7。
"""
from app.shared.persistence.base import Base
from app.shared.persistence.db import DbEngines
from app.shared.persistence.models import IndexIdempotency, IndexOffset

__all__ = ["Base", "DbEngines", "IndexIdempotency", "IndexOffset"]
