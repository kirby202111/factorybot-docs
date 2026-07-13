"""5 个只读红线 Gate 的启动断言。

锁住"只读旁路从约定变成结构属性"：任一 Gate 命中写动作即 fail-fast 拒绝启动。
最坏情况是"没检索出来"，不会产生写副作用。
"""
from __future__ import annotations

import pytest

from app.routes.agentic.domain.tool import ReadOnlyToolGate, ToolDescriptor, ToolRegistry
from app.routes.document.domain.projection import ReadOnlyIngestionGate
from app.routes.traceability.domain.projection import (
    RawDataTopicGate,
    ReadOnlyProjectionGate,
)
from app.shared.acl.gates import ReadOnlyAclGate, StartupAssertionError


# ── ReadOnlyAclGate：MES 只读客户端方法名禁止写动词 ──


class _WriteAclClient:
    """含写动词方法名 ``create_route`` 的非法只读客户端。"""

    def create_route(self, route_id: str) -> None:  # noqa: D401  (write verb)
        ...

    def fetch_route_version(self, route_id: str) -> dict:
        ...


class _ReadonlyAclClient:
    """方法名均为读动词前缀（fetch/get）的合法只读客户端。"""

    def fetch_route_version(self, route_id: str) -> dict:
        ...

    def fetch_checkpoints(self, sn: str) -> list:
        ...


def test_read_only_acl_gate_rejects_write_verb_method():
    with pytest.raises(StartupAssertionError, match="create_route"):
        ReadOnlyAclGate().assert_readonly([_WriteAclClient()])


def test_read_only_acl_gate_allows_readonly_methods():
    # 不抛即通过
    ReadOnlyAclGate().assert_readonly([_ReadonlyAclClient()])


# ── ReadOnlyProjectionGate：图投影 Cypher 禁止 DELETE/REMOVE ──


class _DeleteProjectionHandler:
    cypher_templates = ["MATCH (n) DETACH DELETE n"]


class _MergeProjectionHandler:
    cypher_templates = ["MERGE (n:RouteVersion {id:$id}) SET n.status=$status"]


def test_read_only_projection_gate_rejects_delete():
    with pytest.raises(StartupAssertionError, match="DELETE"):
        ReadOnlyProjectionGate().assert_on([_DeleteProjectionHandler])


def test_read_only_projection_gate_allows_merge():
    ReadOnlyProjectionGate().assert_on([_MergeProjectionHandler])  # 不抛


# ── RawDataTopicGate：禁止订阅 dc.* 原始数据流（白名单除外）──


def test_raw_data_topic_gate_rejects_non_allowlist_dc_topic():
    with pytest.raises(StartupAssertionError, match="dc.equipment.raw"):
        RawDataTopicGate().assert_on([["dc.equipment.raw"]])


def test_raw_data_topic_gate_allows_allowlist_and_non_dc():
    allowlist = ["dc.identity.sn.minted", "dc.equipment.runtime", "dc.equipment.alarm.raw"]
    RawDataTopicGate().assert_on([allowlist])  # 白名单 3 主题通过
    RawDataTopicGate().assert_on([["mes.checkpoint.lifecycle"]])  # 非 dc.* 通过


# ── ReadOnlyIngestionGate：摄入 handler 禁止写 MES 调用 ──


class _WriteMesHandler:
    async def handle(self, event, tx):  # noqa: D401
        await self.mes.post("/api/write")  # .post( + mes -> 命中


class _ReadonlyIngestionHandler:
    async def handle(self, event, tx):
        data = await self.acl.fetch_route_version("R-1", "v3")  # 只读，无 mes


def _coordinator(handler) -> object:
    return type("C", (), {"handlers": {"x": handler}})()


def test_read_only_ingestion_gate_rejects_write_mes():
    with pytest.raises(StartupAssertionError, match="post"):
        ReadOnlyIngestionGate().assert_on(_coordinator(_WriteMesHandler()))


def test_read_only_ingestion_gate_allows_readonly():
    ReadOnlyIngestionGate().assert_on(_coordinator(_ReadonlyIngestionHandler()))  # 不抛


# ── ReadOnlyToolGate：ToolRegistry 拒绝注册 read_only=False 工具 ──


def test_read_only_tool_gate_rejects_non_readonly_tool():
    registry = ToolRegistry()
    with pytest.raises(StartupAssertionError):
        registry.register(
            ToolDescriptor(name="evil", description="", route="A", read_only=False)
        )


def test_read_only_tool_gate_allows_readonly_tool():
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(name="good", description="", route="A", read_only=True)
    )
    registry.validate_on_startup()  # 启动期断言通过
