"""FastAPI 路由层。"""
from app.api.diagnosis_router import router as diagnosis_router
from app.api.draft_router import router as draft_router
from app.api.l3_router import router as l3_router

__all__ = ["diagnosis_router", "draft_router", "l3_router"]
