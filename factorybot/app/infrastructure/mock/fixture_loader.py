"""Fixture 加载器：从 data/ 读 JSON，供 ACL 客户端在 mock 模式下查询。

fixture 文件按类别放在 data/ 子目录下（rest/rag/kafka/l3），每个文件是一个
dict（按自然 id 索引）或 list。ACL 客户端用 lookup(rel, key) 取单条，raw(rel) 取整文件。

示例：
    fixtures.lookup("rest/pass_records", "SN-2026-001234")
    fixtures.lookup("rag/subgraphs", "SUB-A1")
    fixtures.raw("kafka/process_route_activated")
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings


class FixtureLoader:
    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir or get_settings().data_dir)
        self._cache: dict[str, Any] = {}

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def _load(self, rel: str) -> Any:
        """加载 data/<rel>.json（rel 如 'rest/pass_records'）。"""
        if rel in self._cache:
            return self._cache[rel]
        path = self._data_dir / f"{rel}.json"
        if not path.exists():
            raise FileNotFoundError(f"fixture 不存在: {path}")
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        self._cache[rel] = data
        return data

    def raw(self, rel: str) -> Any:
        """取整个文件内容。"""
        return self._load(rel)

    def lookup(self, rel: str, key: Optional[str] = None,
               default: str = "_default") -> Any:
        """取单条；key 缺失时回退 default，再不行回退整文件。"""
        data = self._load(rel)
        if key is None:
            return data
        if isinstance(data, dict):
            if key in data:
                return data[key]
            if default in data:
                return data[default]
        return data

    def has(self, rel: str) -> bool:
        return (self._data_dir / f"{rel}.json").exists()


# 单例（进程内）
_fixtures: FixtureLoader | None = None


def get_fixtures() -> FixtureLoader:
    global _fixtures
    if _fixtures is None:
        _fixtures = FixtureLoader()
    return _fixtures
