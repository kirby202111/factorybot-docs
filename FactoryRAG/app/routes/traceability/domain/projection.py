"""A 图投影 handler 协议 + GraphProjector 协调器 + 只读门控。

只读红线（A）：
- ``ReadOnlyProjectionGate``：图投影 handler 禁止 ``DELETE``/``REMOVE``/历史覆盖性 ``SET``；
- ``RawDataTopicGate``：消费者组禁止订阅 ``dc.*`` 原始数据流（高频采集不全量入图）。

门控在 lifespan 启动期扫描，任一失败即拒绝启动（fail-fast）。最坏情况是"没检索出来"，
不会产生写副作用。
"""
from __future__ import annotations

import re

from app.shared.acl.gates import StartupAssertionError
from app.shared.kafka.projection_handler import ProjectionHandler  # noqa: F401  (re-export)


# Cypher 禁止动词（图投影只允许 MERGE/SET/MATCH）。
# 硬规则：禁 DELETE/REMOVE/DETACH DELETE（历史快照边/节点永不删）。
# 软规则：历史覆盖性 SET 由 coalesce($x, prop) 模式 + 评审保证（只对新值非空才覆盖），
# RouteVersion 状态翻转（ACTIVATED->DEPRECATED）属合法版本生命周期，不禁。
_FORBIDDEN_CYPHER = re.compile(r"\b(DELETE|REMOVE|DETACH\s+DELETE)\b", re.IGNORECASE)

# dc.* 原始数据流允许的语义子主题白名单（仅这三个允许，其余 dc.* 禁止）。
_RAW_TOPIC_ALLOWLIST = {"dc.identity.sn.minted", "dc.equipment.runtime", "dc.equipment.alarm.raw"}


class GraphProjector:
    """A 图投影协调器：持有投影 handler 字典 + 订阅主题。

    SRP：只管"事件 -> handler 路由 + 主题声明"；投影动作在 handler 内。
    供 ``ReadOnlyProjectionGate``/``RawDataTopicGate`` 启动期扫描。
    """

    def __init__(self, handlers: dict[str, ProjectionHandler], topics: list[str]) -> None:
        self._handlers = handlers
        self._topics = list(topics)

    @property
    def topics(self) -> list[str]:
        return list(self._topics)

    def handler_for(self, event_type: str) -> ProjectionHandler | None:
        return self._handlers.get(event_type)

    def handler_classes(self) -> list[type]:
        return [type(h) for h in self._handlers.values()]


class ReadOnlyProjectionGate:
    """启动断言：图投影 handler 的 Cypher 模板禁止 DELETE/REMOVE/覆盖性 SET。

    扫描 handler 类的 ``cypher_templates`` 类属性（静态，无需实例化即可扫描）。
    """

    def assert_on(self, handler_classes: list[type]) -> None:
        for cls in handler_classes:
            templates = getattr(cls, "cypher_templates", []) or []
            for tpl in templates:
                if _FORBIDDEN_CYPHER.search(tpl):
                    raise StartupAssertionError(
                        f"图投影 {cls.__name__} Cypher 含禁止动词 DELETE/REMOVE（违反只读投影红线）"
                    )


class RawDataTopicGate:
    """启动断言：消费者组禁止订阅 ``dc.*`` 原始数据流。

    高频采集不全量入图；仅三个语义子主题在白名单内。MVP 4 上下文无 dc.* 主题，
    此断言防未来回归。
    """

    FORBIDDEN_PREFIX = "dc."

    def assert_on(self, topic_lists: list[list[str]] | list[str]) -> None:
        topics = self._flatten(topic_lists)
        for t in topics:
            if t.startswith(self.FORBIDDEN_PREFIX) and t not in _RAW_TOPIC_ALLOWLIST:
                raise StartupAssertionError(
                    f"禁止订阅原始数据流 {t}（高频采集不全量入图）"
                )

    @staticmethod
    def _flatten(topic_lists: list[list[str]] | list[str]) -> list[str]:
        out: list[str] = []
        for item in topic_lists:
            if isinstance(item, str):
                out.append(item)
            else:
                out.extend(item)
        return out
