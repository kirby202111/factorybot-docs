"""ACL 客户端装配：构建全部 ACL client（mock 模式共享 fixtures）。

composition root 调用 build_acl_clients() 拿到所有 client 单例。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import httpx

from app.infrastructure.acl.device_data import (
    DeviceDataAclClient, EquipmentAssetLedgerAclClient,
)
from app.infrastructure.acl.doc_rag import DocRagAclClient
from app.infrastructure.acl.equipment_telemetry import EquipmentTelemetryAclClient
from app.infrastructure.acl.material import MaterialAclClient
from app.infrastructure.acl.pass_execution import (
    PassExecutionAclClient, PassExecutionWriteAclClient,
)
from app.infrastructure.acl.process_management import (
    ProcessManagementAclClient, ProcessWriteAclClient,
)
from app.infrastructure.acl.quality import QualityAclClient
from app.infrastructure.acl.rag import RagAclClient
from app.infrastructure.acl.rework import ReworkAclClient, ReworkWriteAclClient
from app.infrastructure.acl.tooling import ToolingAclClient
from app.infrastructure.acl.work_order import WorkOrderManagementAclClient
from app.infrastructure.mock.fixture_loader import FixtureLoader, get_fixtures


def build_acl_clients(
    mock: bool = True,
    fixtures: Optional[FixtureLoader] = None,
    http: Optional[httpx.AsyncClient] = None,
    confirmation_store=None,
) -> SimpleNamespace:
    """构建全部 ACL client。mock 模式共享 fixtures；real 模式共享 httpx。"""
    fixtures = fixtures or get_fixtures()
    http = http or (None if mock else httpx.AsyncClient(timeout=3.0))

    return SimpleNamespace(
        # 只读
        pass_execution=PassExecutionAclClient(http, "/api", fixtures, mock),
        work_order=WorkOrderManagementAclClient(http, "/api", fixtures, mock),
        process=ProcessManagementAclClient(http, "/api", fixtures, mock),
        material=MaterialAclClient(http, "/api", fixtures, mock),
        device_data=DeviceDataAclClient(http, "/api", fixtures, mock),
        asset_ledger=EquipmentAssetLedgerAclClient(http, "/api", fixtures, mock),
        tooling=ToolingAclClient(http, "/api", fixtures, mock),
        telemetry=EquipmentTelemetryAclClient(http, "/api", fixtures, mock),
        rework=ReworkAclClient(http, "/api", fixtures, mock),
        quality=QualityAclClient(http, "/api", fixtures, mock),
        rag=RagAclClient(http, "", fixtures, mock),
        doc_rag=DocRagAclClient(http, "", fixtures, mock),
        # 受限写（编排，带 confirmation_store）
        pass_write=PassExecutionWriteAclClient(http, "/api", fixtures, mock),
        process_write=ProcessWriteAclClient(http, "/api", fixtures, mock),
        rework_write=ReworkWriteAclClient(http, "/api", fixtures, mock, confirmation_store=confirmation_store),
    )
