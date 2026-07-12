"""pytest 配置：确保项目根在 sys.path，工作目录为项目根（data/ 相对路径可解析）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # factorybot/
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 测试强制 mock 模式，不受项目 .env（可能配真实 provider）影响；env var 优先于 .env
os.environ["LLM_PROVIDER"] = "mock"
os.environ["LLM_API_KEY"] = ""
os.environ["RUN_MODE"] = "mock"

import pytest


@pytest.fixture(autouse=True)
def reset_container():
    """每个测试用全新的容器（清进程内仓库/缓存）。"""
    from app.container import reset_container
    reset_container()
    yield
    reset_container()
