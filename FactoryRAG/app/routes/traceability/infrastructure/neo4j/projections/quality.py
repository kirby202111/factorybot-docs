"""质量上下文投影 handler。

``QualityVerdictIssued``：MERGE QualityVerdict，JUDGED_BY/UNDER_RULE{rule_version}/CITES_DEFECT 边。
``QualityGateRuleActivated``：MERGE QualityGateRule{ACTIVATED}，老规则置 DEPRECATED。
``DefectCatalogDefined``：MERGE DefectCatalog{name_embedding}（bge-m3 向量，供缺陷语义入口）。
"""
from __future__ import annotations

from app.routes.traceability.infrastructure.neo4j.projections.base import ProjectionHandlerBase
from app.shared.kafka.domain_event import DomainEvent

_MERGE_VERDICT = """
MERGE (qv:QualityVerdict {verdict_id: $verdict_id})
SET qv.business_verdict = $business_verdict, qv.tenant_scope = $tenant_scope,
    qv.occurred_at = $occurred_at
"""

_LINK_VERDICT = """
MATCH (qv:QualityVerdict {verdict_id: $verdict_id}), (t:TestResult {test_id: $test_id})
MERGE (t)-[:JUDGED_BY]->(qv)
"""

_MERGE_DEFECT = """
MERGE (dc:DefectCatalog {defect_code: $defect_code})
SET dc.name = $name, dc.severity = coalesce($severity, dc.severity),
    dc.name_embedding = $embedding
"""

_MERGE_GATE_RULE = """
MERGE (qgr:QualityGateRule {rule_id: $rule_id, rule_version: $rule_version})
SET qgr.status = 'ACTIVATED', qgr.tenant_scope = $tenant_scope
"""


class QualityProjectionHandler(ProjectionHandlerBase):
    """质量上下文投影。"""

    bounded_context = "质量"
    event_type = "QualityVerdictIssued"
    event_types = ["QualityVerdictIssued", "QualityGateRuleActivated", "DefectCatalogDefined"]
    cypher_templates = [_MERGE_VERDICT, _LINK_VERDICT, _MERGE_DEFECT, _MERGE_GATE_RULE]

    async def handle(self, event: DomainEvent, tx) -> None:
        p = event.payload or {}
        tenant = event.tenant_scope or ""
        if event.event_type == "QualityVerdictIssued":
            await self._run(
                _MERGE_VERDICT,
                verdict_id=p.get("verdict_id", ""),
                business_verdict=p.get("business_verdict", ""),
                occurred_at=p.get("occurred_at", event.occurred_at.isoformat()),
                tenant_scope=tenant,
            )
            if p.get("test_id"):
                await self._run(_LINK_VERDICT, verdict_id=p["verdict_id"], test_id=p["test_id"])
        elif event.event_type == "QualityGateRuleActivated":
            await self._run(
                _MERGE_GATE_RULE,
                rule_id=p.get("rule_id", ""),
                rule_version=p.get("rule_version", ""),
                tenant_scope=tenant,
            )
        elif event.event_type == "DefectCatalogDefined":
            embedding = await self._embedder.embed_one(p.get("name", "")) if self._embedder else []
            await self._run(
                _MERGE_DEFECT,
                defect_code=p.get("defect_code", ""),
                name=p.get("name", ""),
                severity=p.get("severity"),
                embedding=embedding,
            )
