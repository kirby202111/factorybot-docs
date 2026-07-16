# Kafka 集成全景与实现说明

> **定位**：本文是本 MES 项目 Kafka 集成的**全景审查文档**。以"业务场景与节点"为经、"Kafka 使用方式 / 架构闪光点 / 实施重难点"为纬，系统梳理三大服务（制造资源 / 设备管理 / 生产执行）14 个限界上下文 + 两个 Python 旁路服务（agent-service / rag-service）中**所有需要集成 Kafka 的具体业务场景**，并提炼每一类使用模式的架构闪光点与实施重难点。
>
> **与既有文档的关系（不重复造事实源）**：
> - **怎么做（How）**——业务事件的可靠投递机制（Outbox 表结构 / Publisher / 限流 / 幂等 / DLT / 重放）权威源是 [Outbox设计方案](业务事件/Outbox设计方案.md)；高频采集的管道实现（边缘网关 / 协议适配 / 断点续传 / 去重 / 乱序矫正 / 存储分层）权威源是 [MES高频数据方案](高频数据/MES高频数据方案.md)；Kafka 集群 / Spring Kafka 基础配置权威源是 [基础设施/Kafka配置说明](基础设施/Kafka配置说明.md)。
> - **在哪用、为什么用（What / Where / Why）**——即**本文**。本文不重复上述 How，只在引用时点明出处；本文的增量是**跨 14 上下文的场景普查、使用模式归纳、闪光点与重难点剖析**。
> - 领域契约（聚合根 / 事件 / 不变式编号）的权威源是各限界上下文的事件风暴与领域建模文档（`领域模型/`），本文只引用其 topic / 事件名。
>
> **适用边界**：覆盖两条 Kafka 路径——① 低频业务契约事件（走 Outbox）；② 高频采集数据（走边缘缓冲 + 直连）；以及两条 Python 旁路消费/生产路径（图投影 / Agent 主动触发 / 动作卡推送）。事务边界、一致性模型、可靠性语义沿用上述权威文档，本文不另行定义。
>
> **口径纪律**：本文出现的吞吐 / 限流 / 保留期数值均为**设计容量目标 + 假设**（参见 [项目亮点与指标卡片](../面试指南/项目亮点与指标卡片.md) §0），不是线上实绩。需按真实集群容量与车间实测量拍板的阈值用 🔴 标注，交还运维 / 架构决策。

---

## 0. 审查结论（TL;DR）

1. **Kafka 是本 MES 跨服务解耦的唯一事件骨干**。三大 Java 主体服务之间、以及 Java 主体与 Python 旁路服务之间，所有"跨进程协作"除少量低频同步 REST 查询外，全部经 Kafka。没有第二条消息中间件，没有点对点 RPC 编排。

2. **两条可靠性路径刻意分工，是整套设计的地基**。低频业务契约事件（过点 / 工单 / 维修 / 物料 / 质量 / 工艺 / 资产）走 **Transactional Outbox**（与业务状态同事务原子，至少一次 + `event_id` 幂等）；高频设备采集（秒级遥测 / 工艺采样 / 单件事件）走**边缘缓冲 + Kafka 直连**（不经 Outbox、不开 DB 事务，不丢不重 + `msg_id` 去重）。这条架构级排除是 Outbox 不被写爆、限流参数可定的根本前提（[Outbox §9.1](业务事件/Outbox设计方案.md)、[高频数据 §2](高频数据/MES高频数据方案.md)）。

3. **Kafka 在本系统承担五种角色**，本文据此组织场景分析：① 配置型主数据的"发布—缓存刷新"投影；② 业务流程的事件编排；③ 双向可逆状态重算；④ 资产生命周期与可用性聚合；⑤ 高频采集搬运。另有两种 Python 旁路角色：⑥ 只读图投影构建；⑦ Agent 主动触发与动作卡推送。

4. **场景规模**：全系统约 **13 个 topic 命名空间、近百个领域事件**落入 Kafka（见 §3 全景表）。其中走 Outbox 的低频契约事件约 8 成，走 dc.* 直连的高频采集占剩余，外加 `agent.*` 一个跨语言 topic。

5. **最值得讲的闪光点**：命名空间按业务语义隔离而非技术来源；`partition_key=聚合根ID` 保序 + 幂等 Producer；CQRS 读侧缓存投影 + 降级 REST 把过点压到 ≤200ms；版本快照不可变让历史追溯免疫后续变更；同服务强防错本地事务与跨服务异步事件的精细切分；跨语言 Python 旁路复用既有 Kafka 契约零新管道。

6. **最难啃的重难点**：消费幂等的正确性（幂等记录与业务同事务）；重复窗口（PUBLISHING 超时恢复重发）；缓存与权威源的一致性窗口；长链路事件编排的最终一致与中间态可观测；多源事件并发重算可用性的乐观锁竞争；事件 schema 演进；"幂等保证不重复执行 vs 重放想重复执行"的认知冲突。

---

## 1. Kafka 集成总览架构

### 1.1 一集群两路径三消费阵营

```text
┌─────────────────────────────────────────────────────────────────────┐
│  生产侧                                                              │
│  ┌───────────────────────────────┐   ┌────────────────────────────┐ │
│  │  Java 主体服务（14 上下文）     │   │  车间设备（A/B/C/D 类）      │ │
│  │  业务命令 -> 聚合根 -> Outbox   │   │  原始信号（SECS/OPC/MQTT…）  │ │
│  │  （低频契约事件，同事务原子）    │   │  -> 边缘网关缓冲/断点续传     │ │
│  └──────────────┬────────────────┘   └──────────────┬─────────────┘ │
└─────────────────┼───────────────────────────────────┼───────────────┘
                  │ 路径① Outbox+Kafka                 │ 路径② 直连
                  │ mes.*/wo.*/fai.*/rework.*/          │ dc.*（不经 outbox）
                  │ brework.*/schedule.*/material.*/    │
                  │ process.*/quality.*/                │
                  │ eam.*/pm.*/calibration.*/repair.*   │
                  ▼                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│            Kafka 集群（3 broker / RF=3 / min.insync=2）              │
│   命名空间隔离 + 集群级配额（业务 client-id 与 dc client-id 分离）    │
└──────────────┬───────────────────────────┬──────────────────────────┘
               │                           │
   ┌───────────┼───────────┐               │（dc.* 主要被台账/过点/质量消费）
   ▼           ▼           ▼               ▼
┌──────┐  ┌────────┐  ┌──────────┐   ┌──────────────┐
│ Java │  │ Java   │  │ Python   │   │ Python       │
│ 主体 │  │ 主体   │  │ rag-svc  │   │ agent-svc    │
│ 各上 │  │ 各上   │  │ Graph    │   │ 事件监听(主动│
│ 下文 │  │ 下文   │  │ Projector│   │ 触发) + 动作 │
│ 幂等 │  │ 幂等   │  │ -> Neo4j │   │ 卡生产       │
│ 消费 │  │ 消费   │  │ 只读投影  │   │ agent.*      │
└──────┘  └────────┘  └──────────┘   └──────────────┘
  消费阵营①：Java 跨上下文协作      消费阵营②③：Python 旁路只读/受限
```

### 1.2 两条路径的可靠性模型对照

| 维度 | 路径① 业务契约事件（Outbox） | 路径② 高频采集（直连） |
|---|---|---|
| 与业务状态原子 | 是（同本地事务） | 否（刻意降级） |
| 投递语义 | 至少一次 + `event_id` 幂等 | 至少一次 + `msg_id` 去重 + 消费幂等 |
| 不丢 | DB 事务原子 | 边缘缓冲 + 断点续传（先补后新） |
| 不重 | 消费端 `event_id+consumer_group` | 平台 `msg_id` 去重表 + 消费端 `msg_id` 幂等 |
| 顺序 | 分区内有序（`partition_key=聚合根ID`） | 分区内有序 + 乱序矫正窗口 |
| 单条价值 | 高（缺失=流程断裂） | 低（缺失可容忍，靠聚合兜底） |
| 适用 | 状态变化触发的低频契约 | 秒级持续观测流 |
| Topic 命名空间 | `mes/wo/fai/rework/brework/schedule/material/process/quality/eam/pm/calibration/repair.*` | `dc.*` |

> 详细对照见 [Outbox设计方案 §1](业务事件/Outbox设计方案.md) 与 [高频数据 §1.1/§2.2](高频数据/MES高频数据方案.md)。

### 1.3 Python 旁路服务的 Kafka 角色

| 服务 | 语言栈 | Kafka 角色 | topic | 边界 |
|---|---|---|---|---|
| rag-service | Python（aiokafka） | **只读图投影消费者**：订阅 `mes.*/process.*/material.*/quality.*` 增量构建 Neo4j 追溯图 | 复用既有领域事件 topic | 只读投影，`ReadOnlyProjectionGate` 禁止 DELETE/REMOVE；不订 `dc.*` 原始流 |
| agent-service | Python（aiokafka） | **主动触发消费者**：订阅 `ProcessRouteActivated` / `equipment.fault` / 不良率突增等只读事件，触发 L2 草拟 / L3 编排 | 复用既有领域事件 topic | 只读旁路，物理隔离不进过点主事务 |
| agent-service | Python（aiokafka） | **动作卡生产者**：L3 confirmation gate 的动作卡 WebSocket（实时）+ Kafka 持久兜底 | `agent.action_cards`（key=`session_id`） | 双通道，Kafka 兜底离线可达可回溯 |

---

## 2. 全景 Topic 命名空间与职责

统一命名 `<domain>.<aggregate>.<dimension>`，表达业务语义而非技术来源（[Outbox §7.1](业务事件/Outbox设计方案.md)）。

| 命名空间 | 发布上下文 | 所属服务 | 性质 | 典型 topic |
|---|---|---|---|---|
| `mes.*` | 在制品执行 | 生产执行 | 低频契约（Outbox） | `mes.checkpoint.lifecycle` `mes.routing.progress` `mes.workorder.progress` `mes.testresult.structured` `mes.fixture.used` `mes.malfunction.reported` `mes.unit.routed-to-rework` `mes.reentry.reworked` `mes.reentry.rejected` |
| `wo.*` | 工单管理 | 生产执行 | 低频契约 | `wo.order.*`（created/released/started/changed/paused/resumed/completed/closed/cancelled…） `wo.kit.status` |
| `wip.*` | 在制品执行（读侧投影对外） | 生产执行 | 低频契约 | `wip.position.changed` `wip.location.snapshot` |
| `fai.*` | 首件处理 | 生产执行 | 低频契约 | `fai.article.*` `fai.flow.*` `fai.unit.bound` |
| `rework.*` | 返修（单件） | 生产执行 | 低频契约 | `rework.task.*` `rework.diagnosis` `rework.action.performed` `rework.verification` |
| `brework.*` | 返工（批量） | 生产执行 | 低频契约 | `brework.order.*` |
| `schedule.*` | 排产 | 生产执行 | 低频契约 | `schedule.suggested/confirmed/rescheduled/released/rejected` |
| `material.*` | 物料 | 制造资源 | 低频契约 | `material.master/product/supplier/bom/substitute/inventory/kit.*` `material.changed` |
| `process.*` | 工艺管理 | 制造资源 | 低频契约 | `process.route.lifecycle` `process.operation.lifecycle` `process.parameter.template` |
| `quality.*` | 质量 | 制造资源 | 低频契约 | `quality.gate.lifecycle` `quality.standard.*` `quality.defect.catalog` `quality.inspection.verdict` `quality.anomaly.batch` `quality.iqc.lifecycle` |
| `eam.*` | 设备工装台账 | 设备管理 | 低频契约 | `eam.asset.lifecycle/availability/specification/metric/location/scrap` |
| `pm.*` | 点检保养 | 设备管理 | 低频契约 | `pm.inspection.*` `pm.maintenance.*` `pm.fixture.life/warning` `pm.rule.lifecycle` |
| `calibration.*` | 计量检定 | 设备管理 | 低频契约 | `calibration.rule.lifecycle` `calibration.plan.*` `calibration.task.*` `calibration.certificate.*` |
| `repair.*` | 维修 | 设备管理 | 低频契约 | `repair.order.*` `repair.diagnosis` `repair.parts.consumed` `repair.verification` `repair.escalation` `repair.external` `repair.scrap.recommendation` `repair.suggestion` |
| `dc.*` | 设备数据接入 | 设备管理 | **高频直连** | `dc.process.sample.raw` `dc.station.event.raw` `dc.identity.sn.minted` `dc.equipment.lifecycle/runtime/alarm.raw` `dc.material.event.raw` `dc.gateway.health` |
| `agent.*` | agent-service | Python 旁路 | 跨语言 | `agent.action_cards` |

> 前缀严格区分铁律：`wo.*`（正常工单）/ `brework.*`（批量返工）/ `rework.*`（单件返修）/ `repair.*`（设备维修）四者语义相近但严禁混用，靠前缀物理隔离避免事件串台。

---

## 3. 九大 Kafka 使用模式（场景分析）

> 以下按"Kafka 在该场景中扮演的角色"归纳为九类使用模式。每类给出：典型业务节点 -> Kafka 核心使用方式与作用 -> 架构闪光点 -> 实施重难点与技术挑战。具体 topic / 事件见各模式引用。

### 模式 A：配置型主数据的"发布—缓存刷新"投影

**典型业务节点**

- 工艺版本生效 `process.route.lifecycle`（`ProcessRouteActivated`/`Deprecated`）-> 在制品执行刷新 `ProcessRouteCache`
- 质量门禁规则生效 `quality.gate.lifecycle`（`QualityGateRuleActivated`/`Deprecated`）-> 在制品执行刷新 `QualityGateCache`
- BOM / 替代料生效 `material.bom.lifecycle` / `material.substitute.lifecycle` -> 在制品执行刷新物料防错缓存
- 设备可用性变更 `eam.asset.availability` -> 在制品执行刷新 `EquipmentAvailabilityCache`
- 齐套状态 `wo.kit.status` -> 在制品执行刷新 `KitStatusCache`
- 首件门禁 `fai.article.released` / `fai.article.blocked` -> 在制品执行首件门禁投影
- 资产生命周期 `eam.asset.lifecycle` -> 点检保养 / 计量检定 / 维修激活或停用对应规则

**Kafka 核心使用方式与作用**

配置型主数据"写少读多"，写入仅在版本发布时发生。生产侧（制造资源 / 设备台账）发事件，消费侧（生产执行过点引擎、点检调度引擎）订阅后刷新**本地缓存**作为读侧投影。过点时优先读缓存（≤200ms 硬要求，[领域总览 §4.1](../领域模型/领域总览.md)），缓存未命中降级 REST 查权威源（≤500ms）。Kafka 在此承担"配置变更的最终一致广播"角色，避免过点主事务引入跨服务同步调用。

**架构闪光点**

1. **CQRS 读侧缓存投影 + 降级兜底**：缓存是读优化投影不是事实源，丢失可从权威源重建（[高频数据 §7.1](高频数据/MES高频数据方案.md) 同构思想）。`EquipmentOnline ≠ AssetCommissioned`——通信视角与资产视角独立维护，缓存语义不混淆。
2. **版本快照不可变**：过点记录锁定 `routeVersion`，工艺变更事件只影响变更后首次过点的在制品，历史追溯免疫后续变更（[领域总览 §5.1](../领域模型/领域总览.md)）。图投影侧用 `[:SNAPSHOT_OF_ROUTE]` 边把版本一致性变成结构属性。
3. **配置生效走 Outbox 同事务**：`ProcessRouteActivated` 与 RouteVersion 状态变更（SUBMITTED->ACTIVATED）+ 旧版本 Deprecate 在同一本地事务，保证"新版本生效 + 旧版本失效 + 通知下游"原子（INV-CX-02）。
4. **`superseded_by` 区分取代与整体退役**：`QualityGateDeprecated(superseded_by=null)` 才通知工艺管理标记 DRAFT stale 并通知过点移除缓存；规则修订（`rule_id` 不变）不广播，避免噪声。

**实施重难点与技术挑战**

1. **缓存与权威源的一致性窗口**：事件最终一致意味着缓存有秒级滞后。若过点恰在"配置已发布、缓存未刷新"窗口内读到旧值，可能放行本应拦截的过点。缓解：缓存未命中降级 REST 兜底 + 关键变更后短暂双读校验 🔴。
2. **缓存未命中雪崩**：冷启动或缓存大面积失效时，过点降级 REST 打爆权威源。需缓存预热（新消费组 `auto-offset-reset=earliest` 补投影）+ 降级查询限流熔断。
3. **缓存失效粒度**：质量门禁规则弃用要精确移除缓存中该 `rule_id` 条目，不能整批失效。需 `rule_id+version` 粒度的缓存键设计。
4. **降级 REST 的版本对齐**：降级查询返回的必须是"当前生效版"，而图投影锁定的是"过点当时版"——两者口径不同，消费侧需明确区分。

---

### 模式 B：业务流程的事件编排（Event Choreography）

**典型业务节点**

- **工单下达链**：`wo.order.released` -> 在制品执行（缓存工单状态允许过点）+ 首件处理（触发首件判定）+ 排产（待排工单入积压生成 PENDING 建议）
- **过点—进度—完工链**：`mes.checkpoint.lifecycle`（首次过点 `CheckpointReleased`）-> 工单管理（RELEASED->IN_PROGRESS 自动转态）；`mes.workorder.progress`（`WorkOrderProgressAccrued`）-> 工单管理（完工判定与自动结案）
- **排产—齐套链**：`schedule.confirmed` -> 物料（齐套预占）+ 工单（回写 MES 排产快照）；`material.kit.reservation-failed` -> 排产（缺料重排）
- **返修/返工再入链**：`mes.unit.routed-to-rework` -> 返修（创建返修任务）；`rework.task.completed` -> 在制品（再入点校验 + RoutingProgress 推进）；`mes.reentry.rejected` -> 返修/返工（再入点重判）
- **质量判定链**：`mes.testresult.structured` -> 质量（异步业务判定 `QualityVerdictIssued`）；`quality.anomaly.batch`（`BatchQualityAnomalyDetected`，BATCH_SYSTEMIC）-> 返工（生成 `BatchReworkOrder`）
- **首件闭环链**：`wo.order.released` -> 首件处理（触发）；`fai.article.released/blocked` -> 在制品（解除/强制首件门禁）；`fai.flow.completed` -> 质量（首件检验结果回写）

**Kafka 核心使用方式与作用**

跨上下文流程推进**无中心编排器**，每个上下文自治响应上游事件、发布自身事件驱动下游。Kafka 承担"流程接力的可信传递带"：发件方只管发布事实（已下达 / 已过点 / 已完工），收件方按自身职责决定下一步。流程的"状态机"分散在各上下文内部，靠事件串联成端到端业务流。

**架构闪光点**

1. **再入点重判防死循环**：返修/返工再入点被在制品拒绝（`mes.reentry.rejected`）后，返修/返工上下文重新判定再入点，用 `reeval_count` 阈值（如 3 次）封顶，耗尽后走默认回退或报废建议，避免"拒绝->重判->再拒绝"无限循环。
2. **前缀物理隔离防串台**：`wo.*`（正常工单）/ `brework.*`（批量返工）/ `rework.*`（单件返修）/ `repair.*`（设备维修）语义相近，靠 topic 前缀物理隔离，消费方不会把返工事件错当返修处理。
3. **进度事件按状态变化发布**：`WorkOrderProgressAccrued` 仅在完工状态变化时发布，不是每次普通过点都发——避免过点高频场景下进度 topic 被打爆（与高频采集走 dc.* 的分流同源思想）。
4. **同步门禁 vs 异步判定职责切分**：过点执行做**同步门禁**（`QualityGateEvaluated`，权威，驱动放行/拦截）；质量上下文做**异步业务判定**（`QualityVerdictIssued`，记录性，供首件/SPC）。两者不要求强一致，异步 BLOCK 不回溯拦截已放行过点（INV-CX-04）。

**实施重难点与技术挑战**

1. **长链路最终一致的中间态可观测**：工单从下达到结案跨 6+ 上下文、十余个事件，任一环消费滞后或进 DLT 都会导致流程"卡在某步"。需 `correlation_id`（业务流程关联）+ `causation_id`（因果上游）贯穿全链，配合 `trace_id` 做端到端时序还原。这是事件编排相对同步编排最痛的运维点。
2. **乱序到达**：`schedule.confirmed` 与 `schedule.released` 若乱序，工单快照可能被错误覆盖。同一 `work_order_id` 用相同 partition key 落同分区保序（[Outbox §7.2](业务事件/Outbox设计方案.md)），但跨 topic 的事件无全局序，消费侧需靠状态机守卫（如已 CLOSED 拒收后续 released）。
3. **重复消费幂等**：每条事件消费必须 `event_id+consumer_group` 同事务幂等（[Outbox §8.3](业务事件/Outbox设计方案.md)）。工单首次过点转态只能发生一次，重复消费不能把 IN_PROGRESS 重复转或回退——靠状态机前置条件 + 幂等表双保险。
4. **事件版本演进**：`wo.order.released` 后续若新增字段必须可选或有默认，不删不改语义；重大不兼容用新 `event_version` 或新 `event_type`，消费方按类型+版本分发（[Outbox §7.7](业务事件/Outbox设计方案.md)）。

---

### 模式 C：双向可逆状态重算（以 KitStatus 为代表）

**典型业务节点**

物料上下文发布多源事件 -> 工单管理上下文重算 `KitStatus` -> 发布 `wo.kit.status` -> 下游消费：

- `material.bom.lifecycle`（`BomActivated`/`BomDeprecated`）-> 重算
- `material.inventory.changed`（`InventoryChanged`）-> 重算
- `material.substitute.lifecycle`（`SubstituteRuleActivated`/`Deactivated`）-> 重算
- `material.kit.reserved` / `material.kit.released` -> 重算
- 结果 `wo.kit.status`（`KitStatusChanged`）-> 在制品执行（首次过点齐套防错）+ 排产（齐套失效重排）

**Kafka 核心使用方式与作用**

齐套状态不是单一事件的直接映射，而是**多源物料事件汇聚后的重算结果**。工单管理上下文是 `KitStatus` 的唯一判定方（物料上下文不判定齐套）。状态双向可逆：`READY ↔ NOT_READY`。Kafka 在此承担"多源信号汇聚 -> 单点重算 -> 结果再广播"的扇入扇出枢纽。

**架构闪光点**

1. **判定权单一 + 多源汇聚**：物料上下文只发布物料事实（BOM 变了 / 库存变了 / 预占了），齐套判定权归工单管理。避免"谁都能改 KitStatus"的并发冲突，单一聚合根维护状态机。
2. **可逆状态机**：齐套不是单调推进的终态，库存补充可从 NOT_READY 回到 READY。事件驱动天然适配可逆——每次重算都基于当前完整物料视图，不依赖历史轨迹。
3. **重算幂等**：`work_order_id + source_event_id` 去重，同一物料事件不触发重复重算；重算结果无变化时不发布 `KitStatusChanged`（物料上下文 `InventoryChanged` 同理：无变化不发布）。

**实施重难点与技术挑战**

1. **重算风暴**：批量 BOM 导入或批量入库会短时产生大量 `material.*` 事件，每个都触发同一工单的 KitStatus 重算。需重算去抖（debounce，短时间内多个事件合并一次重算）或重算入队串行化，避免乐观锁冲突雪崩。
2. **重算顺序与一致性**：多个物料事件并发到达，重算必须基于一致的物料快照。可按 `work_order_id` 落同分区串行消费，保证该工单的物料事件按序处理。
3. **可逆状态抖动**：库存临界值附近波动可能让 KitStatus 在 READY↔NOT_READY 频繁翻转，下游排产反复重排。需状态滞回（hysteresis）或确认窗口，避免抖动传导。
4. **重算的读模型一致性**：重算依赖物料上下文的库存 / 预占读模型，这些读模型本身也是事件投影。若读模型滞后，重算结果会基于过期数据。需明确"重算读哪个时间点的物料视图"并接受其最终一致语义。

---

### 模式 D：资产生命周期与可用性聚合（设备管理服务核心）

**典型业务节点**

设备工装台账上下文是**设备状态唯一事实源**，消费四源结果事件综合重算可用性：

- 点检保养 `pm.inspection.completed`（FAIL->SuspendAsset）/ `pm.inspection.overdue`（MarkAssetUnavailable）/ `pm.maintenance.completed` / `pm.maintenance.overdue` / `pm.fixture.life`（寿命超限->SuspendAsset）
- 计量检定 `calibration.task.dispatched`（送检->SuspendAsset）/ `calibration.certificate.issued`（恢复）/ `calibration.task.failed` / `calibration.certificate.expired`
- 维修 `repair.order.completed`（恢复）/ `repair.scrap.recommendation`（触发报废审批）/ `mes.malfunction.reported`（产线报修->SuspendAsset）
- 设备数据接入 `dc.equipment.lifecycle`（`ChannelEscalated`->SuspendAsset 通信丢失）/ `dc.equipment.alarm.raw`（`EquipmentAlarmRaised` severity=CRITICAL->SuspendAsset）/ `dc.equipment.runtime`（运行时长累积）/ `dc.station.event.raw`（工装计数累积）
- 结果 `eam.asset.availability`（`AssetAvailabilityChanged` + blocking_reasons[]）-> 生产执行（派工准入）+ 排产（产能受损重排）
- 累积度量链：`dc.equipment.runtime`（`EquipmentRunHourAggregated`）-> 台账（`EquipmentRunHourAccrued` 发 `eam.asset.metric`）-> 点检保养（阈值判定 -> `pm.maintenance.due`/`pm.fixture.life`）-> 台账（SuspendAsset）

**Kafka 核心使用方式与作用**

设备可用性是过点准入与排产的**硬门禁**，但"可用"是多个独立维度（点检 / 保养 / 寿命 / 检定 / 维修 / 通信 / 报警）的**合取**。任一维度阻塞即不可用，全部解除才可恢复。台账上下文作为聚合点，消费各运维上下文的结果事件，综合重算可用性并广播。Kafka 承担"多运维域信号汇聚到单点可用性判定"的扇入通道。

**架构闪光点**

1. **三种阻塞机制的精细区分**：`SuspendAsset`（硬阻塞，状态变 SUSPENDED，停用资产）、`MarkAssetUnavailable`（条件性阻塞，状态保持 IN_SERVICE 但 availability=false）、`AddBlockingReason`（叠加阻塞，已 SUSPENDED 时追加原因）。不同运维异常对应不同阻塞强度，避免一刀切。
2. **可用性判定权单一**：点检 / 计量 / 维修上下文只发布事实（点检 FAIL / 检定过期 / 维修完成），**不直接指挥**台账恢复或停用（INV-CX-03）。台账综合所有阻塞维度后判定，避免多源并发恢复/停用冲突。
3. **强防错本地事务 vs 跨服务异步的精细切分**（核心设计）：
   - **同服务强防错走本地事务同步**：点检异常 / 点检超期 / 保养超期 / 保养完成 / 寿命超限 / CRITICAL 故障——这些与台账同属设备管理服务，由 `AssetAvailabilityCommandService` 在同一本地事务内编排并同步更新 `AvailabilityProjection`，**不依赖 `pm.*` 异步消息**作为内部一致性来源（INV-CX-06/08/09）。强防错场景绝不让事件最终一致的滞后窗口放过本应拦截的过点。
   - **跨服务异步走事件**：检定送检 / 检定通过 / 维修完成恢复 / 报废审批——这些跨服务或非强防错场景走事件解耦，台账保留可用性判定唯一权威。
   - 这条切分是整套设备管理设计的精华：**强防错用本地事务买确定性，跨服务协作用事件买解耦**，两者按"是否同服务 + 是否强防错"判定。
4. **累积度量的"增量上报 + 单点累加"分工**：设备数据接入只上报原始采集事件（`StencilUsageIncremented` / `TestFixtureCycleCounted` / `EquipmentRunHourAggregated`），累积度量（`FixtureCycleAccrued` / `EquipmentRunHourAccrued`）的维护权归台账。采集层不维护累积状态（INV-CX-01 只搬运不解释），台账按 `(fixture_id, accumulated_cycles)` 去重累加。
5. **`EquipmentOnline ≠ AssetCommissioned` 视角分离**：采集侧在线状态（通信视角）与资产投用状态（资产视角）独立维护、独立事件流（`dc.equipment.lifecycle` vs `eam.asset.lifecycle`），合并必生腐败（[设备数据接入 §0](../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md)）。

**实施重难点与技术挑战**

1. **多源事件并发重算可用性的乐观锁竞争**：点检 FAIL、检定过期、维修完成可能近乎同时到达，都触发同一资产的可用性重算。需 `asset_id + version` 乐观锁 + 重试，且重算必须基于"当前所有阻塞维度"而非单事件增量——否则会丢阻塞维度。
2. **阻塞维度叠加与解除的幂等**：同一资产可能同时有"点检超期 + 检定过期"两个阻塞原因，解除检定阻塞时不能误清点检阻塞。`blocking_reasons[]` 必须按原因维度独立增删，幂等按 `(asset_id, reason, source_event_id)`。
3. **同服务本地事务与跨服务事件的边界判定**：哪些运维异常走本地事务、哪些走事件，是设计时必须拍死的边界。判错会导致强防错场景出现一致性窗口（本该同步却走了异步），或低频场景过度同步拖累事务。判定准则：**同服务 + 强防错（停用直接影响过点准入）-> 本地事务；跨服务或非强防错 -> 事件**。
4. **累积度量去重与基准重置**：`accumulated_value - reset_baseline` 与阈值比较，`reset_baseline` 由台账维护（保养完成后 `MaintenanceCompleted(metric_reset=true)` -> `MetricBaselineReset`）。翻修 `FixtureRefurbished(life_reset=true)` 重置 accumulated_cycles=0。基准重置事件若丢失或重复，寿命判定会错。需按 `(fixture_id, 翻修序号)` 去重 + 重置后立即重算寿命消耗。
5. **通信故障的二级处理**：`ChannelEscalated` 不直接进维修，而是台账先 `SuspendAsset(reason=CommunicationLost)`，维修上下文订阅 `AssetSuspended(reason=CommunicationLost)` 后生成**草稿**故障报告待人工确认（避免通信抖动误报维修）。这条"先停用再评估"的两级处理，是通信视角与维修视角解耦的关键。

---

### 模式 E：高频采集直连管道（dc.* 命名空间）

**典型业务节点**

设备 -> 边缘网关（协议适配 `DecodingStrategy` + 打质量标 + 边缘缓冲 + 断点续传 + 大载荷卸载 MinIO）-> Kafka `dc.*` 直连（不经 Outbox、不开 DB 事务）-> 平台接入（`msg_id` 去重 + 乱序矫正）-> 落库 + 分发到下游 `dc.*` 主题。

- `dc.process.sample.raw`：工艺参数原始采样（印刷 / 波峰焊 / 老化环境），A/B/D 类窄流
- `dc.station.event.raw`：工位会话级事件 + 工装计数（过板 / 拧紧 / 烧录 / AOI / 测试），C 类单件事件
- `dc.identity.sn.minted`：镭雕 SN 落版（SN 主键源头，必须先到平台再被下游消费）
- `dc.equipment.lifecycle`：设备上线 / 离线 / 恢复 / 通道升级
- `dc.equipment.runtime`：运行时长周期聚合（班次级）
- `dc.equipment.alarm.raw`：设备报警归一化（`EquipmentAlarmRaised/Cleared`，含 severity）
- `dc.material.event.raw`：线边仓出入库 / MSD 事件
- `dc.gateway.health`：网关与通道健康度

**Kafka 核心使用方式与作用**

秒级持续流的可靠搬运。Kafka 在此不是"业务事件广播"，而是"观测事实的传递缓冲带 + 重放窗口"。可靠性模型从"同事务原子"刻意降级为"不丢不重"——单条价值低、可重传、靠聚合兜底，不追求与业务状态强一致（[高频数据 §2.2](高频数据/MES高频数据方案.md)）。下游（过点 / 质量 / 物料 / 台账）按 `msg_id` 幂等消费。

**架构闪光点**

1. **架构级排除（地基级闪光点）**：高频采集**不走 Outbox**。若走 Outbox，秒级持续流会把 `outbox_event` 写爆（写放大 + 行锁竞争 + 复制延迟），Outbox 限流的四类压力全部恶化。这条边界让 Outbox 始终面对低频业务事件，限流参数才好定（[Outbox §9.1](业务事件/Outbox设计方案.md)、[高频数据 §2.1](高频数据/MES高频数据方案.md)）。
2. **三大可靠性支柱叠加等效"不丢不重"**：① 边缘缓冲 + 断点续传（先补后新，INV-05）；② 平台 `msg_id` 去重（BIZ-02）；③ 消费端 `msg_id` 幂等。任何一环崩溃，下一环兜底。
3. **采集只搬运不解释（INV-CX-01）**：网关 / 平台不做 PASS/FAIL 判定、不推进过站、不停用资产。业务语义一律留下游上下文。这条硬约束让协议适配器免疫工艺变更——工艺一改不用改驱动，是最常见的腐败源防范。
4. **`.raw` 后缀强调未解释 + 命名空间隔离**：`dc.*` 与业务 `mes.*/eam.*/mfg.*` 隔离，差异化保留策略（采集 3~7 天 vs 业务 7~30 天）与配额（采集 `*-dc` client-id 独立配额），不混在同一 topic。
5. **`partition_key=equipment_id` 保序 + 乱序矫正**：同设备采集事件按 `source_ts` 有序落同分区；网络抖动 / 补传导致的乱序由平台 `BufferReorder` 矫正，迟到报文打 LATE 质量标补入（INV-04），不丢弃。

**实施重难点与技术挑战**

1. **边缘网关是采集链路单点**：网关宕机则所属设备全部断采（本地缓冲也随网关进程消失）。HA 部署（单网关+缓冲 / 主备 / 集群）🔴 待定，且 HA 不能破坏 BIZ-01（同 `equipment_id` 同时只能有一个活跃 `CollectionChannel`）——接管时必须先确认原通道已 CLOSED/DEGRADED。
2. **断点续传的"先补后新"游标单调性**：恢复后 `resuming=true` 期间新报文必须排在补传队列之后，由 `buffered_cursor`/`backfilled_cursor` 单调性保证。若 Kafka 已发送成功但网关宕机前未推进游标，恢复后会重复发送同一 `msg_id`，靠平台去重吸收——这是"不丢"换"可能重复"的取舍。
3. **`msg_id` 去重表保留期与布隆过滤器膨胀**：去重表保留期需 ≥ 最长补传窗口 + 安全余量（建议 ≥7 天）。布隆过滤器需定期重建或用计数布隆避免无限增长。去重存储选型（Redis 布隆+DB 唯一 / 仅 DB 唯一 / Redis SET+异步 DB）🔴 待定。
4. **乱序矫正窗口的权衡**：窗口过大增延迟，过小乱序矫正失效；超时放行阈值（窗口 2~3 倍）防缺失报文导致窗口永不推进。仅需严格时序的聚合（SPC / 运行时长）启用窗口，纯离散事件（过板 / 拧紧）按到达顺序处理即可 🔴。
5. **时钟漂移与 `source_ts ≤ ingest_ts`**：设备时钟漂移 >3s 时按 `ingest_ts` 修正并记 `clock_skew`（INV-11）。`source_ts ≤ ingest_ts` 始终成立（INV-04）。车间设备 NTP 同步是前提。
6. **死信回放窗口**：解码失败的原始字节保留 7 天滚动窗口（INV-07）可回放，存储成本需与运维确认 🔴。

---

### 模式 F：大载荷卸载到对象存储（MinIO + URI 引用）

**典型业务节点**

E 类大载荷（扭矩曲线数百点/枪、AOI 图像、烧录日志、振动频谱、固件文件）**不进 Kafka**（单消息默认 1MB 上限），走 MinIO 直传，主流 `DataPacket` 只承载 `object_uri + sha256`：

- 智能电批扭矩曲线 -> MinIO，`dc.station.event.raw` 主流只带峰值/最终值 + `curve_uri`
- AOI 图像 -> MinIO，主流带 `image_uri + sha256 + retain_until`
- 烧录日志 / 固件文件 -> MinIO，主流带 `log_uri` / 固件指纹校验

**Kafka 核心使用方式与作用**

Kafka 只承载"窄而快"的结构化流与大载荷的**引用**，"大而慢"的附件进对象存储。这是吞吐与成本的核心折衷，由领域模型 `PayloadKind`（STRUCTURED / OBJECT_STORAGE）固化。Kafka 在此承担"引用通知"角色，字节流走 MinIO。

**架构闪光点**

1. **对象先于引用存在**：网关侧先上传 MinIO + sha256 校验通过后才 `DataPacket.seal(object_uri, sha256)`，否则会出现"引用指向空对象"。上传失败不 seal，主流不发引用。
2. **浏览器 / 消费侧直连 MinIO**：预签名 GET URL 让浏览器直连 MinIO 拉取图像 / 曲线，流量不经过业务服务器，卸载带宽。
3. **预签名 PUT 直传 + multipart 续传**：网关向平台请求预签名 PUT URL（或 STS 临时凭证）直传 MinIO，大文件走 multipart 分片续传，断点可续。
4. **`PayloadKind` 固化分流**：分流规则不在事件载荷里"提前判断"，而是由领域模型 `PayloadKind` 枚举固化——结构化进 Kafka，对象存储进 MinIO，是 schema 级约定。

**实施重难点与技术挑战**

1. **引用一致性（空引用）**：若 MinIO 上传成功但 seal 前网关宕机，或上传失败但误 seal，会出现"主流有引用、MinIO 无对象"。靠"上传成功 + 校验通过才 seal"的严格时序 + 死信兜底。
2. **预签名 URL 生命周期**：预签名 URL 有有效期，过期后消费侧拉不到对象。需 URL 有效期 ≥ 消费侧最长处理延迟，或消费侧按需重新申请预签名 GET。
3. **MinIO bucket 规划与保留期**：`dc/{kind}/{equipment_id}/{yyyy-MM-dd}/{msg_id}.{ext}` 前缀规划，`curve/aoi-image/log/deadletter/` 分类保留期 🔴。大载荷保留期与 Kafka retention 解耦——Kafka 短期缓冲，MinIO 按审计要求长期保留。
4. **sha256 校验开销**：大文件计算 sha256 有 CPU 开销，网关侧需异步计算不阻塞采集主链路。校验失败需重传 + 告警。

---

### 模式 G：只读图投影构建（rag-service GraphProjector）

**典型业务节点**

rag-service 的 `GraphProjector` 订阅领域事件流，增量构建 / 更新 Neo4j 追溯图（5M1E 串联）：

- MVP 4 上下文：在制品执行（`mes.checkpoint.lifecycle` / `mes.routing.progress` / `wip.*`）+ 工艺管理（`process.route.lifecycle`）+ 物料（`material.bom.lifecycle` / `material.inventory.changed`）+ 质量（`quality.inspection.verdict` / `quality.anomaly.batch`）
- 后续扩展：设备（`eam.*`）、环境（`dc.*` 语义事件，非原始流）

**Kafka 核心使用方式与作用**

图不是凭空建模，是各上下文领域事件的**只读投影**——与在制品执行的 `ProcessRouteCache` / `EquipmentAvailabilityCache` 同构，只是投影目标是属性图而非键值缓存（[追溯型 RAG §2.3](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)）。Kafka 承担"事实流 -> 图增量写入"的投影通道。复用既有领域事件 envelope 与消费侧幂等模式，不造新契约。

**架构闪光点**

1. **图是只读投影，事实源是聚合根**：`ReadOnlyProjectionGate` 启动断言禁止 `DELETE`/`REMOVE`/历史覆盖性 `SET`。图库归 RAG 服务自有，从不回写 MES。跨语言物理边界天然强制只读。
2. **版本一致性结构性兜底**：`CheckpointRecord` 节点带 `route_version` + `[:SNAPSHOT_OF_ROUTE]` 边指向当时版本；工艺变更（`ProcessRouteActivated`）只新增版本节点、旧版本 `DEPRECATED` 不删，历史边不动（INV-CX-02）。把"版本一致性"从"LLM 自觉带版本"变成"图结构物理保证"。
3. **不进过点主事务，允许秒级最终一致**：图索引异步消费事件，过点 P99 ≤200ms 不受图索引影响（[领域总览 §5.3](../领域模型/领域总览.md)）。图挂了过点照常，只是追溯查询退化。
4. **高频原始流不全量入图**：`assert_no_raw_data_topic` 启动断言兜底，MVP 不订 `dc.*` 原始流（图要的是语义事件不是原始报文）。
5. **跨语言复用既有契约**：Python rag-service 用 aiokafka 订阅 Java 主体发布的同一套领域事件 envelope（`event_id`/`event_type`/`event_version`/`partition_key`），`event_id` 幂等 + consumer offset（MySQL）+ ACL 降级 REST，不造新管道。

**实施重难点与技术挑战**

1. **投影滞后与降级**：图允许秒级最终一致，但 L1 诊断若在投影滞后窗口内查询，会拿到不完整图。靠 L1 ACL 降级查询上下文只读 REST 补齐（如 `CONSUMED_BATCH` 边未投影时降级调 `GET /api/material/consumption`）。降级阈值 🔴 待定。
2. **`CONSUMED_BATCH` 边 gap**：批次反向扩展依赖 `CONSUMED_BATCH` 边，但该边的事件契约（物料消耗明细）尚不明确 🔴。MVP 用降级 REST 兜底，待物料上下文明确消耗明细事件后改投影。
3. **`event_id` 幂等与位点**：图投影侧 `event_id` 幂等表 + consumer offset 都落 MySQL，重启从断点续消费。幂等记录与图写入须同事务，否则重启会重复写图节点/边。
4. **图 schema 演进**：Neo4j 图 schema（节点标签 / 边类型 / 属性）随业务演进，已有节点 / 边不能破坏性变更。需图 schema 版本化 + 兼容性策略。
5. **跨语言消费的 envelope 一致性**：Java 侧 JSON 序列化与 Python 侧 Pydantic 反序列化必须字段对齐，否则投影静默失败。需共享 schema 定义（如 JSON Schema / AsyncAPI）。

---

### 模式 H：Agent 主动触发（agent-service 事件订阅）

**典型业务节点**

agent-service 订阅只读领域事件，把 Agent 从"被动问答"变"主动巡检 / 编排"：

- 订阅 `ProcessRouteActivated`（工艺升版 v4->v5）-> 触发 L3 `process_change` 编排（草拟新 SOP + 核对操作工资质 + 新工艺首件验证）/ 触发 L2 SOP 草拟
- 订阅 `equipment.fault`（设备故障，Kafka topic 或维修看板手动触发）-> 触发 L3 `fault_response` 故障复产编排
- 订阅不良率突增事件 -> 触发 L1 同批次诊断（`DefectRateSpikeListener`）

**Kafka 核心使用方式与作用**

Agent 不再等工程师提问，而是**事件驱动主动介入**。Kafka 承担"业务异常 / 变更 -> Agent 触发"的信号通道。agent-service 用 aiokafka 异步非阻塞消费，只订阅只读事件、不消费任何写命令——主动触发的是"诊断 / 草拟 / 编排"，写动作仍走人在回路 confirmation gate。

**架构闪光点**

1. **Agent 只读旁路物理隔离**：跨语言物理边界天然强制 Agent 不进过点主事务、不旁路应用服务写路径。最坏情况是"没诊断出来"，不会产生写副作用（[整体技术选型 §1.3](../整体技术选型与模块划分.md)）。
2. **事件驱动非问答主动触发**：复用既有领域事件契约，不造新管道。Agent 的触发源从"人点按钮"变成"业务事件"，覆盖"工程师还没注意到但系统已该介入"的场景。
3. **L3 代码+agent 混合编排的零 LLM 快路径**：换线全程 PASS 时 agent 节点根本不触发，LLM 调用为 0（[整体技术选型 §4.3](../整体技术选型与模块划分.md)）。事件触发后先走代码节点（plan / query+compare / gate），仅非确定分支才调 agent 能力——"代码能做的不交给 LLM"。
4. **跨语言 trace 串联**：Python 侧 httpx OTel instrumentation 自动注入 W3C `traceparent`，Java 侧 OTel agent 续接同一 trace；Kafka 消费侧同样透传 `trace_id`，实现"事件触发 -> Agent 推理 -> 受限写"全链路追踪。

**实施重难点与技术挑战**

1. **触发去重与风暴控制**：同一事件可能因 Kafka 重投多次到达，或短时间内多个相关事件同时触发同一场景。Agent 触发须幂等（按 `event_id` + scenario 去重），且并发触发需限流（按 tenant 信号量），避免突发把 LLM 调用打爆。
2. **Agent 长程任务与事件消费的背压**：L3 编排是长程任务（interrupt/resume，跨进程恢复），事件消费速率可能远超 Agent 处理速率。需消费端 pause 背压 + 任务入队，不能让事件积压拖垮 Kafka consumer（触发 rebalance）。
3. **跨语言事件消费幂等**：Python 侧 `event_id` 幂等表 + 手动 ack，与 Java 侧同构但独立实现。两套幂等表 schema 须对齐。
4. **触发条件配置化**：哪些事件触发哪个 Agent 场景、触发阈值（如不良率突增的"突增"定义）应配置化，不硬编码在监听器里。🔴 触发规则归属（Agent 侧配置 vs MES 侧事件契约）待定。

---

### 模式 I：动作卡双通道推送（agent.action_cards）

**典型业务节点**

L3 confirmation gate 节点产出的动作卡，经 `ActionCardDispatcher` 双通道推送：

- **WebSocket（实时）**：在线责任人即时收到
- **Kafka `agent.action_cards`（持久兜底）**：离线也能收到、可回溯；partition_key=`session_id`；带 `traceparent`

**Kafka 核心使用方式与作用**

人在回路的动作卡必须可靠送达责任人。WebSocket 解决"在线实时"，Kafka 解决"离线兜底 + 可回溯"。Kafka 在此承担"动作卡的持久化送达带"——即使责任人离线、Agent 重启、Pod 滚动更新，动作卡不丢，重连后可补送。

**架构闪光点**

1. **双通道互补**：WebSocket 的实时性 + Kafka 的持久性。实时通道失败（责任人未连 WS）不影响持久通道；持久通道消费侧（前端拉取 / 重连补送）兜底。
2. **Kafka 兜底离线 / 重连补送**：与 L3 长程任务的 interrupt/resume 同构——state 在 MySQL 不在进程内存，Pod 重启后同一 `thread_id` 续跑。动作卡的 Kafka topic 同样跨进程持久，责任人重连后从上次 offset 补送未读卡。
3. **跨语言 Python 生产**：agent-service（Python aiokafka）生产 `agent.action_cards`，是本系统唯一的 Python->Kafka 生产路径（其余 Python 服务都是消费）。partition_key=`session_id` 保证同一编排会话的动作卡有序。

**实施重难点与技术挑战**

1. **双通道消息顺序与去重**：WS 与 Kafka 可能重复送达同一张卡（WS 已推、Kafka 又补）。前端须按 `card_id` 去重，且两通道的卡片版本须一致。
2. **离线期间动作卡堆积与过期**：责任人长期离线，动作卡在 Kafka 堆积；gate 有 deadline（超时挂起）。需动作卡 TTL + 超时挂起机制，过期卡不再补送而是转超时处理。
3. **确认 token 与卡片的绑定**：动作卡携带 confirmation token，`POST /confirm` 用 token 续跑。token 与 card 的绑定须严格（`ConfirmationStore` Redis，TTL 30min），防伪造 / 重放。
4. **Kafka 消费方（前端）的拉取模式**：前端不是常驻 Kafka consumer，而是重连时拉取未读动作卡。需"按 `user_id` 过滤 + 从上次 offset 起拉"的查询模式，与常规 Kafka consumer 不同——可能需要独立读模型投影而非直接消费 Kafka。

---

## 4. 横向架构闪光点总结

> 以下闪光点跨越多个模式，是整套 Kafka 集成设计的"骨架级"亮点。

| # | 闪光点 | 体现 | 价值 |
|---|---|---|---|
| 1 | **两条可靠性路径刻意分工** | Outbox（强一致）vs dc.* 直连（不丢不重） | 架构级限流，Outbox 不被写爆，参数可定 |
| 2 | **命名空间按业务语义隔离** | 13 个前缀，非 `service-name.event` | topic 自解释，前缀物理隔离防串台（wo/brework/rework/repair） |
| 3 | **`partition_key=聚合根ID` 保序 + 幂等 Producer** | 所有 topic 统一 | 同聚合根事件落同分区有序；重试不乱序 |
| 4 | **统一 Envelope** | `event_id`/`event_type`/`event_version`/`trace_id`/`correlation_id`/`causation_id` | 幂等去重 / 监控聚合 / schema 演进 / 链路串联统一入口 |
| 5 | **至少一次 + 消费端幂等** | `event_id+consumer_group`（业务）/ `msg_id`（采集） | 等效一次处理，容忍重投 |
| 6 | **CQRS 读侧缓存投影 + 降级 REST** | 过点 5 大缓存（工艺 / 设备 / 质量门 / 齐套 / 工单） | 过点 ≤200ms，缓存丢失可重建 |
| 7 | **版本快照不可变** | `routeVersion` 锁定 + `SNAPSHOT_OF_ROUTE` 边 | 历史追溯免疫后续变更，版本一致性变结构属性 |
| 8 | **强防错本地事务 vs 跨服务异步精细切分** | 设备管理同服务本地事务 / 跨服务事件 | 强防错买确定性，跨服务买解耦，按需选择 |
| 9 | **采集只搬运不解释** | INV-CX-01，网关 / 平台不做业务判定 | 协议适配器免疫工艺变更，防腐败 |
| 10 | **跨语言 Python 旁路复用既有契约** | GraphProjector / Agent 监听器复用 Java 事件 | 零新管道，物理边界强制只读 |
| 11 | **限流四层 + 自适应背压** | 领取节流 / 全局令牌桶 / 按 topic 公平 / 在途上限 | 突发下保护 Kafka / DB / 下游不被击穿 |
| 12 | **限流与顺序正交** | 速率维度 vs 分区维度 | 限流不破坏分区内顺序 |

---

## 5. 横向重难点与技术挑战

> 以下挑战跨多个模式，是实施落地最易踩的坑。

### 5.1 消费幂等的正确性

**挑战**：幂等不是"加个去重表"那么简单。幂等记录必须与业务处理在**同一本地事务**提交（[Outbox §8.3](业务事件/Outbox设计方案.md)）：

```text
先 INSERT consumed_event
  -> 主键 (event_id, consumer_group) 冲突 => 已处理，直接 ack
  -> 插入成功 => 同事务内执行业务处理 => commit => ack
```

若幂等记录与业务分两个事务，"幂等插入成功但业务失败"会产生"标记已处理却没执行"的空洞。

**易错点**：跨上下文事件消费的幂等键设计各异——`work_order_id+source_event_id`（KitStatus 重算）、`sn+rework_task_id`（返修再入）、`(asset_id, accumulated_run_hours)`（运行时长累积）。每条事件须按其业务语义设计幂等键，不能一刀切用 `event_id` 了事（累积类事件的 `event_id` 不同但语义可能重复）。

### 5.2 重复窗口（PUBLISHING 超时恢复重发）

**挑战**：Outbox Publisher 发送 Kafka 成功后、标 `SENT` 前宕机，`PUBLISHING` 超时恢复为 `RETRYABLE`，会**重复发送同一 `event_id`**（[Outbox §7.8](业务事件/Outbox设计方案.md)）。采集侧同理：网关发送成功但未推进 `backfilled_cursor`，恢复后重复发送同一 `msg_id`。

**应对**：这是"不丢"换"可能重复"的固有取舍，靠消费端幂等吸收。**所有**业务事件消费者必须用 `event_id+consumer_group` 幂等，**所有**采集消费者必须用 `msg_id` 幂等——无例外。

### 5.3 缓存与权威源的一致性窗口

**挑战**：模式 A 的 5 大缓存都有秒级滞后窗口。过点恰在"配置已发布、缓存未刷新"窗口内读到旧值，可能放行本应拦截的过点。

**应对**：① 缓存未命中降级 REST 兜底（权威源是事实源）；② 关键变更后短暂双读校验 🔴；③ 接受最终一致语义——过点记录锁定 `routeVersion`，即使缓存滞后，历史追溯仍可还原"当时按哪版工艺判的"。

### 5.4 长链路事件编排的最终一致与中间态可观测

**挑战**：模式 B 的工单下达->过点->进度->完工链跨 6+ 上下文，任一环消费滞后或进 DLT 都会导致流程"卡在某步"，且无中心编排器可查"当前到哪了"。

**应对**：① `correlation_id`（业务流程关联）+ `causation_id`（因果上游）贯穿全链，配合 `trace_id` 做端到端时序还原；② 关键节点（工单状态、过点进度、齐套状态）建读模型投影供"当前状态"查询；③ DLT 必须有告警 + 人工处理流程，不静默丢弃（[Outbox §8.4](业务事件/Outbox设计方案.md)）。

### 5.5 多源事件并发重算的乐观锁竞争

**挑战**：模式 C（KitStatus）与模式 D（可用性）都是多源事件触发同一聚合根重算。并发到达会触发乐观锁冲突，重算须基于"当前所有维度"而非单事件增量，否则丢维度。

**应对**：① 按 `work_order_id` / `asset_id` 落同分区串行消费，保证同聚合根事件按序处理；② `version` 乐观锁 + 重试；③ 重算去抖（短时间内多事件合并一次重算）。

### 5.6 事件 schema 演进

**挑战**：近百个领域事件，字段会演进。不兼容变更会打破消费方。

**应对**（[Outbox §7.7](业务事件/Outbox设计方案.md)）：新增字段必须可选或有默认值；不删除已有字段、不改变语义与类型；重大不兼容变更用新 `event_version` 或新 `event_type`；每条事件必带 `event_type + event_version`，消费方据此分发。跨语言（Java->Python）还需共享 schema 定义（JSON Schema / AsyncAPI）防反序列化静默失败。

### 5.7 跨上下文 trace 串联

**挑战**：跨服务事件链路，`trace_id` 如何在 Kafka 消息中透传并续接？Java 发 -> Kafka -> Java 消 / Python 消，三段 trace 须同源。

**应对**：① 事件 envelope 的 `trace_id` 写入 Kafka header（用于路由 / 过滤 / 排障）+ payload（持久化）；② 消费侧从 header / payload 取 `trace_id` 续接 span；③ Python 侧 httpx OTel + Java 侧 OTel agent 续接同一 W3C `traceparent`（[整体技术选型 §1.3](../整体技术选型与模块划分.md)）；④ `correlation_id`（业务流程）与 `trace_id`（技术链路）分离，前者跨长业务流，后者单次请求链路。

### 5.8 同服务本地事务 vs 跨服务事件的边界判定

**挑战**：模式 D 的设备管理服务，哪些运维异常走本地事务、哪些走事件，判错会导致强防错场景出现一致性窗口，或低频场景过度同步拖累事务。

**判定准则**：**同服务 + 强防错（停用直接影响过点准入）-> 本地事务同步**（点检异常 / 超期、保养超期 / 完成、寿命超限、CRITICAL 故障）；**跨服务或非强防错 -> 事件解耦**（检定、维修完成恢复、报废审批）。这条边界须在设计时拍死并文档化，不能由开发临时判断。

### 5.9 DLT 治理与人工补偿

**挑战**：DLT 消息不能静默丢弃，但人工处理流程易缺位。DLT 堆积无人看 = 事件链路静默断裂。

**应对**（[Outbox §8.4](业务事件/Outbox设计方案.md)）：① DLT 必须有告警（`DEAD_LETTER > 0` 持续 5 分钟为高）；② DLT 消息保留原始 payload / headers / 异常摘要；③ DLT 后需人工处理或补偿流程；④ 生产侧死信可重放（`DEAD_LETTER` 经人工修复后重置为 `PENDING` 重新投递，消费端幂等吸收重复）。

### 5.10 "幂等保证不重复执行 vs 重放想重复执行"的认知冲突

**挑战**：Outbox + Kafka retention 使事件可重放，支撑读模型重建。但幂等保证"不重复执行"——单纯重放会被幂等表跳过，**不会重跑业务**。这是 Outbox 重放最易踩的坑（[Outbox §13](业务事件/Outbox设计方案.md)）。

**应对**：要"重复执行"必须主动绕过幂等——换新 `consumer_group` 从头消费（简单，推荐）；或引入"重处理标记 + 扩展幂等键"（`event_id + consumer_group + reprocess_epoch`，复杂）。关键认知：**幂等是"不重复执行"的保证，重放想"重复执行"必须主动绕过幂等**。

---

## 6. Topic 命名空间与全清单（速查）

> 完整事件清单见各限界上下文事件风暴 / 领域建模文档。本表只列 topic 级清单。

### 6.1 业务契约事件（走 Outbox）

| 服务 | 上下文 | 前缀 | 核心 topic |
|---|---|---|---|
| 生产执行 | 工单管理 | `wo.*` | `wo.order.{created,binding-locked,released,started,changed,paused,resumed,completed,closed,cancelled,...}` `wo.kit.status` |
| 生产执行 | 在制品执行 | `mes.*` / `wip.*` | `mes.checkpoint.lifecycle` `mes.routing.progress` `mes.workorder.progress` `mes.testresult.structured` `mes.fixture.used` `mes.malfunction.reported` `mes.unit.routed-to-rework` `mes.reentry.reworked` `mes.reentry.rejected` `wip.position.changed` `wip.location.snapshot` |
| 生产执行 | 首件处理 | `fai.*` | `fai.article.{requested,released,blocked,overridden}` `fai.flow.{composed,step-advanced,completed}` `fai.unit.bound` |
| 生产执行 | 返修（单件） | `rework.*` | `rework.task.{created,completed,scrapped}` `rework.diagnosis` `rework.action.performed` `rework.verification` |
| 生产执行 | 返工（批量） | `brework.*` | `brework.order.{created,released,completed,scrapped,progress,scrap_recommended}` |
| 生产执行 | 排产 | `schedule.*` | `schedule.{suggested,confirmed,rescheduled,released,rejected}` |
| 制造资源 | 物料 | `material.*` | `material.{master,product,supplier,bom,substitute}.lifecycle` `material.inventory.changed` `material.kit.{reserved,released,reservation-failed}` `material.changed` |
| 制造资源 | 工艺管理 | `process.*` | `process.route.lifecycle` `process.operation.lifecycle` `process.parameter.template` |
| 制造资源 | 质量 | `quality.*` | `quality.gate.lifecycle` `quality.standard.{lifecycle,fai}` `quality.defect.catalog` `quality.inspection.verdict` `quality.anomaly.batch` `quality.iqc.lifecycle` |
| 设备管理 | 设备工装台账 | `eam.*` | `eam.asset.{lifecycle,availability,specification,metric,location,scrap}` |
| 设备管理 | 点检保养 | `pm.*` | `pm.inspection.{due,completed,overdue,anomaly,plan,task}` `pm.maintenance.{due,completed,overdue,plan,task}` `pm.fixture.{life,warning}` `pm.rule.lifecycle` |
| 设备管理 | 计量检定 | `calibration.*` | `calibration.rule.lifecycle` `calibration.plan.{due,generated}` `calibration.task.{created,dispatched,failed,received}` `calibration.certificate.{issued,expiring,expired}` |
| 设备管理 | 维修 | `repair.*` | `repair.order.{lifecycle,completed}` `repair.diagnosis` `repair.parts.consumed` `repair.verification` `repair.escalation` `repair.external` `repair.scrap.recommendation` `repair.suggestion` `repair.verification.trial` |

### 6.2 高频采集（走直连）

| 上下文 | 前缀 | 核心 topic |
|---|---|---|
| 设备数据接入 | `dc.*` | `dc.process.sample.raw` `dc.station.event.raw` `dc.identity.sn.minted` `dc.equipment.{lifecycle,runtime,alarm.raw}` `dc.material.event.raw` `dc.gateway.health` |

### 6.3 跨语言（Python 旁路）

| 服务 | 前缀 | 核心 topic |
|---|---|---|
| agent-service | `agent.*` | `agent.action_cards`（生产） |
| rag-service / agent-service | （复用上述） | 消费 `mes.*`/`process.*`/`material.*`/`quality.*`/`eam.*`/`equipment.fault` 等 |

---

## 7. 跨上下文事件协作总览矩阵

> 行=发布方，列=消费方。✓=有事件协作。仅列跨上下文协作，上下文内 CQRS（如在制品执行写侧->`WipUnit` 读侧进程内订阅，不走 Kafka）不在此列。

| 发布方 \ 消费方 | 工单管理 | 在制品执行 | 首件处理 | 返修 | 返工 | 排产 | 物料 | 工艺管理 | 质量 | 台账 | 点检保养 | 计量检定 | 维修 | 设备数据接入 |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| 工单管理 `wo.*` | — | ✓ | ✓ | | | ✓ | | | | | | | | |
| 在制品执行 `mes.*` | ✓ | — | ✓ | ✓ | ✓ | | ✓（消耗） | | ✓（TestResult） | ✓（FixtureUsed） | | | ✓（Malfunction） | |
| 首件处理 `fai.*` | | ✓ | — | | | | | | ✓（结果回写） | | | | | |
| 返修 `rework.*` | | ✓ | | — | | | | | ✓（诊断回流） | | | | | |
| 返工 `brework.*` | ✓（只读投影） | ✓ | | | — | | ✓（报废回收） | | | | | | | |
| 排产 `schedule.*` | ✓（快照回写） | | | | | — | ✓（齐套预占） | | | | | | | |
| 物料 `material.*` | ✓（KitStatus 重算） | ✓（防错缓存） | ✓（换料触发） | | | ✓（缺料反馈） | — | | | | | | | |
| 工艺管理 `process.*` | | ✓（工艺缓存） | ✓（ECN 触发） | | | | | — | | | | | | |
| 质量 `quality.*` | | ✓（质量门缓存） | ✓（FA 标准/判定） | | ✓（批量异常->返工） | | ✓（IQC 入库） | ✓（规则弃用标记） | — | | | | | |
| 台账 `eam.*` | | ✓（可用性缓存） | | | | ✓（产能重排） | | ✓（规格引用） | | — | ✓（规则激活/停用） | ✓（规则激活/停用） | ✓（Suspended/Retired/scrap） | ✓（AssetRegistered 绑定） |
| 点检保养 `pm.*` | | | | | | | | | | ✓（结果->状态） | — | | ✓（RepairSuggested） | |
| 计量检定 `calibration.*` | | | | | | | | | | ✓（证书->状态） | | — | | |
| 维修 `repair.*` | | ✓（试产验证） | | | | | | | | ✓（完成->恢复/报废） | ✓（保养建议） | | — | |
| 设备数据接入 `dc.*` | | ✓（实时数据 REST） | | | | | ✓（线边仓事件） | | ✓（采样） | ✓（通信/报警/度量） | | | | — |

> Python 旁路消费：rag-service GraphProjector 消费 `mes.*`/`process.*`/`material.*`/`quality.*`；agent-service 消费 `process.route.lifecycle`/`equipment.fault`/不良率突增等。均不在上表（上下文外，复用契约）。

---

## 8. 实施路线建议

> 本节是场景普查后的落地优先级建议，与 [Outbox §16](业务事件/Outbox设计方案.md) 实施顺序、[高频数据 §11](高频数据/MES高频数据方案.md) 检查清单互补。

| 阶段 | 交付 | 优先级理由 |
|---|---|---|
| **P0 基础设施** | Kafka 集群（3 broker / RF=3 / min.insync=2）+ Spring Kafka 基础配置 + Outbox 表 + consumed_event 表 + DLT 机制 | 所有场景的前提 |
| **P1 业务事件骨干** | Outbox Publisher + 限流四层 + 消费幂等框架；先通"工单下达->过点->进度->完工"主链（`wo.*`/`mes.*`） | 主链跑通即覆盖模式 B 核心，验证 Outbox 端到端 |
| **P2 配置缓存投影** | 模式 A 的 5 大缓存（工艺 / 设备 / 质量门 / 齐套 / 工单）+ 降级 REST；通 `process.*`/`eam.asset.availability`/`quality.gate.lifecycle`/`wo.kit.status` | 过点 ≤200ms 的硬要求依赖此 |
| **P3 物料齐套闭环** | 模式 C 的 KitStatus 重算链（`material.*`->`wo.kit.status`）+ 排产齐套预占链（`schedule.*`↔`material.*`） | 验证多源重算与双向可逆状态机 |
| **P4 设备可用性聚合** | 模式 D 的 `eam.asset.availability` 聚合 + 同服务本地事务 vs 跨服务事件切分；通 `pm.*`/`calibration.*`/`repair.*`->台账 | 过点准入硬门禁，强防错边界落地 |
| **P5 高频采集管道** | 模式 E 的 dc.* 直连 + 边缘网关 + 断点续传 + 去重 + 乱序矫正；模式 F 的 MinIO 大载荷卸载 | 与业务事件独立可并行，但依赖车间设备实测 |
| **P6 Python 旁路** | 模式 G 的 GraphProjector（先 MVP 4 上下文）+ 模式 H 的 Agent 主动触发 + 模式 I 的动作卡双通道 | 依赖业务事件契约稳定后接入 |
| **P7 可观测与治理** | 全链路 trace 串联 + DLT 告警 + 限流可观测 + schema 演进规范 + 重放机制验证 | 贯穿各阶段，P7 集中补齐 |

---

## 9. 关键原则总结

1. **Kafka 是跨服务解耦的唯一事件骨干**，两条可靠性路径（Outbox 业务事件 / dc.* 高频直连）刻意分工是地基。
2. **低频业务契约事件走 Outbox（同事务原子），高频采集走边缘缓冲 + 直连（不丢不重）**——架构级排除，互不污染。
3. **命名空间按业务语义隔离**（13 前缀），前缀物理隔离防串台（wo/brework/rework/repair）。
4. **`partition_key=聚合根ID` 保序 + 幂等 Producer + 至少一次 + 消费端幂等**——所有 topic 统一可靠性基线。
5. **配置型主数据走"发布—缓存刷新"投影 + 降级 REST**，过点 ≤200ms 读本地缓存，缓存丢失可重建。
6. **版本快照不可变**（`routeVersion` / `SNAPSHOT_OF_ROUTE`），历史追溯免疫后续变更。
7. **强防错本地事务 vs 跨服务异步事件精细切分**：同服务 + 强防错 -> 本地事务；跨服务或非强防错 -> 事件。
8. **采集只搬运不解释**（INV-CX-01），网关 / 平台不做业务判定，协议适配器免疫工艺变更。
9. **大载荷走 MinIO，主流只传 URI + sha256**，对象先于引用存在。
10. **跨语言 Python 旁路复用既有 Kafka 契约**，物理边界强制只读，零新管道。
11. **限流是速率维度，顺序是分区维度，两者正交**——限流四层 + 自适应背压不破坏分区内顺序。
12. **幂等保证"不重复执行"，重放想"重复执行"必须主动绕过幂等**（新 consumer_group）。

---

## 附录 A：决策点 🔴（交还用户）

| 决策点 | 说明 | 默认建议 | 出处 |
|---|---|---|---|
| 缓存与权威源一致性窗口的双读校验 | 关键配置变更后是否短暂双读 | 强防错场景启用，常态关 | 模式 A |
| 乱序矫正窗口启用范围 | 哪些 dc.* topic 启用 | 仅需严格时序的聚合类（SPC / 运行时长）启用 | 模式 E |
| 边缘网关 HA 部署形态 | 单网关+缓冲 / 主备 / 集群 | 关键线体主备；切换 RTO 按可用性预算定 | 模式 E |
| 平台 `msg_id` 去重存储方案 | Redis 布隆+DB 唯一 / 仅 DB / Redis SET+异步 | 主推 Redis 布隆 + DB 唯一兜底 | 模式 E |
| MinIO 大载荷保留期 | 按 kind 分类 | `curve/aoi-image/log/deadletter/` 分类，按审计要求 | 模式 F |
| 图投影降级阈值 | 图覆盖度低于多少触发降级 REST | 🔴 待 L1 诊断实测定 | 模式 G |
| `CONSUMED_BATCH` 边事件契约 | 物料消耗明细事件是否定义 | MVP 用降级 REST，待物料上下文明确定义后改投影 | 模式 G |
| Agent 触发规则归属 | 触发条件配置在 Agent 侧还是 MES 事件契约 | 🔴 待定 | 模式 H |
| 同服务本地事务 vs 事件的边界 | 哪些运维异常走本地事务 | 同服务 + 强防错 -> 本地事务；跨服务或非强防错 -> 事件 | 模式 D / §5.8 |
| 全局 / 各 topic 发送配额 | 事件/s、burst、字节/s | 全局 200/s、burst 2×，按业务紧急度分配；上线前压测定 | [Outbox §9.6](业务事件/Outbox设计方案.md) |
| `consumed_event` 幂等保留期 | ≥ 最长重投窗口 | ≥ Kafka retention 或 ≥ 7 天 | [Outbox §12](业务事件/Outbox设计方案.md) |
| Kafka 集群级配额 | per client-id 字节配额 | 业务与 dc client-id 分离设配额兜底 | [Outbox §9.4](业务事件/Outbox设计方案.md) |

> 以上阈值在真实集群容量与车间实测量明确前，均为**设计目标 + 假设**，不作线上实绩承诺（口径见 [项目亮点与指标卡片](../面试指南/项目亮点与指标卡片.md) §0）。

---

## 附录 B：文档索引（权威源）

| 主题 | 权威文档 |
|---|---|
| 业务事件可靠投递（Outbox 表 / Publisher / 限流 / 幂等 / DLT / 重放） | [实现说明/业务事件/Outbox设计方案.md](业务事件/Outbox设计方案.md) |
| 高频采集管道（边缘网关 / 协议适配 / 断点续传 / 去重 / 乱序矫正 / 存储分层） | [实现说明/高频数据/MES高频数据方案.md](高频数据/MES高频数据方案.md) |
| Kafka 集群 / Spring Kafka 基础配置 | [实现说明/基础设施/Kafka配置说明.md](基础设施/Kafka配置说明.md) |
| MinIO 对象存储配置 | [实现说明/基础设施/MinIO配置说明.md](基础设施/MinIO配置说明.md) |
| 领域模型 / 事件风暴 / 不变式 | `领域模型/` 下各限界上下文文档 |
| 跨服务协作模型 / 事务边界 | [领域模型/领域总览.md](../领域模型/领域总览.md) §4/§5 |
| Python 旁路服务（Agent / RAG）技术选型 | [整体技术选型与模块划分.md](../整体技术选型与模块划分.md) |
