"""B 路线 ACL。

B 对 MES 的只读访问（工艺版本查询）走 shared ``MesClients``（ProcessManagementAclClient/
CheckpointAclClient 已上移共享）。本包无路线专属客户端，仅为包结构完整保留。
"""
from app.shared.acl.mes_clients import CheckpointAclClient, ProcessManagementAclClient

__all__ = ["ProcessManagementAclClient", "CheckpointAclClient"]
