# 架构设计与实现 · 实现讲解与面试 QA

> **定位**：配套《[简历核心贡献](简历核心贡献.md)》第一条（架构设计与实现维度）。本文把该条中每个技术动作展开到可实现细节，并配面试问答。
>
> **依据文档**：
> - 领域拆分与协作模型：[领域模型/领域总览.md](../领域模型/领域总览.md)
> - Outbox 权威方案：[实现说明/业务事件/Outbox设计方案.md](../实现说明/业务事件/Outbox设计方案.md)
> - Kafka 集成全景：[实现说明/Kafka集成全景与实现说明.md](../实现说明/Kafka集成全景与实现说明.md)
> - 过点执行与 CQRS：[领域模型/生产执行服务/事件风暴/在制品执行上下文.md](../领域模型/生产执行服务/事件风暴/在制品执行上下文.md)
> - 高频数据方案：[实现说明/高频数据/MES高频数据方案.md](../实现说明/高频数据/MES高频数据方案.md)

---

## 一、实现讲解

### 1.1 DDD 领域拆分：3 微服务 / 14 限界上下文

**服务划分**（依据：变化频率、一致性边界、视角分离，非主观判断）：

| 服务 | 领域定位 | 限界上下文 | 拆分理由 |
|---|---|---|---|
| 制造资源服务 | 生产执行的数据基座 | 物料、工艺管理、质量 | 配置型主数据写少读多，与过点频率差异大，单独成服务避免被高频过点拖累 |
| 设备管理服务 | 设备全生命周期与运维中心 | 设备工装台账、设备数据接入、点检保养、计量检定、维修 | 资产视角（能不能用）、通信视角（通不通）、执行视角（过不过站）关注点完全不同 |
| 生产执行服务 | 现场生产活动执行引擎 | 工单管理、在制品执行、首件处理、返修、返工、排产 | 过点是毫秒级实时执行，工单是天到周级生命周期，频率天差地别 |

**三段式不变式体系**（一致性边界显式分层，每条不变式直接说明"靠什么守护、崩了影响多大"）：

| 层级 | 一致性边界 | 一致性强度 | 守护机制 | 数量（示例） |
|---|---|---|---|---|
| INV-XX | 单一聚合 | 事务内强一致，始终为真 | 聚合根前置条件 / 状态机 / 规约 | 生产执行域 46 条 |
| BIZ-XX | 上下文内跨聚合 / 跨实例 | 最终一致 | 领域服务 + 唯一索引 / 事件去重表 / Saga，**禁止挂聚合根** | 生产执行域 34 条 |
| INV-CX | 跨限界上下文 | 最终一致 | 事件订阅 + ACL + 跨服务协调 | 生产执行域 42 条 |

- **BIZ 禁止挂聚合根的原因**：聚合根只能访问自己一致性边界内的数据。跨实例约束（如"同一资产不可有未决报废申请"）若挂聚合根，聚合就要查别的实例，破坏一致性边界——正确做法是 `uk_pending_scrap(asset_id, pending_flag)` 唯一索引保证。
- **INV-CX 编号在双方上下文保持一致**（如过点执行 INV-CX-02 ↔ 工艺管理上下文同指 routeVersion 锁定），跨服务协作一眼对齐。

### 1.2 Transactional Outbox：业务状态与事件的原子投递

**要解决的问题——崩溃窗口**：

```text
DB commit 成功
  -> 应用进程在 send Kafka 前宕机
  -> Kafka 永远收不到该业务事件
```

**解法**：业务状态变更与 `outbox_event` 写入放同一本地事务，后台 Publisher 异步投递：

```text
同一个本地事务：1. 更新业务表  2. 插入 outbox_event(PENDING)
事务提交后：    3. Publisher 异步读取  4. 发布 Kafka  5. 更新状态
```

**表结构关键设计**（`outbox_event`）：

```sql
CREATE TABLE outbox_event (
    id             VARCHAR(64)  NOT NULL,
    event_id       VARCHAR(64)  NOT NULL,   -- 消费端幂等核心字段
    topic          VARCHAR(128) NOT NULL,
    event_type     VARCHAR(128) NOT NULL,
    event_version  INT          NOT NULL DEFAULT 1,
    aggregate_type VARCHAR(64)  NOT NULL,
    aggregate_id   VARCHAR(128) NOT NULL,
    partition_key  VARCHAR(128) NOT NULL,   -- 通常 = 聚合根 ID
    payload        JSON         NOT NULL,
    status         VARCHAR(32)  NOT NULL,   -- PENDING/PUBLISHING/SENT/RETRYABLE/DEAD_LETTER/DISCARDED
    retry_count    INT          NOT NULL DEFAULT 0,
    next_retry_at  DATETIME(3)  NOT NULL,
    locked_by      VARCHAR(128) NULL,       -- 多实例领取标记
    trace_id / correlation_id / causation_id VARCHAR(128) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_outbox_event_id (event_id),
    KEY idx_outbox_publishable (status, next_retry_at, created_at),  -- 领取主索引
    KEY idx_outbox_topic_status (topic, status, created_at)           -- 按 topic 公平领取
);
```

**状态机**：

```text
PENDING ──> PUBLISHING ──send ok──> SENT（终态）
   ▲            │
   │            ├─send failed, retryable──> RETRYABLE ──next_retry_at 到达──┘
   │            ├─retry exhausted────────> DEAD_LETTER ──manual replay──> PENDING
   │            └─lock timeout───────────> RETRYABLE
DEAD_LETTER ──manual discard──> DISCARDED（终态）
```

**Publisher 多实例领取**（MySQL 8.0，领取与置 PUBLISHING 同一短事务，Kafka 发送不在该事务内）：

```sql
START TRANSACTION;
SELECT * FROM outbox_event
WHERE status IN ('PENDING', 'RETRYABLE')
  AND next_retry_at <= CURRENT_TIMESTAMP(3)
ORDER BY created_at
LIMIT :batchSize
FOR UPDATE SKIP LOCKED;          -- 多实例并发领取不互相阻塞
UPDATE outbox_event SET status = 'PUBLISHING', locked_by = :instanceId, ...;
COMMIT;
```

**重试与退避**：指数退避 + 抖动，`next_retry_at = now + min(max_retry_delay, initial * 2^retry_count) * (0.8~1.2)`；不可恢复错误（序列化失败、topic 不存在、schema 非法）直接进 DEAD_LETTER 不重试。

**事件 Envelope**（统一入口：幂等去重 / 监控聚合 / schema 演进 / 链路串联）：

| 字段 | 用途 |
|---|---|
| `event_id` | 全局唯一，消费端幂等去重核心 |
| `event_type` / `event_version` | 事件类型 / 契约版本，消费方据此分发（schema 演进） |
| `source_service` / `source_context` | 来源服务 / 限界上下文 |
| `trace_id` / `correlation_id` / `causation_id` | 技术链路 / 业务流程关联 / 因果上游 |

**分区保序**：`partition_key = 聚合根 ID`，同聚合根事件落同一分区；**禁止随机 UUID 作 key**（同聚合根事件散落多分区则顺序无法保证）。Producer 配 `acks=all` + `enable.idempotence=true` + `max.in.flight.requests.per.connection≤5`，分区内顺序即使重试也不乱序。

**消费端幂等**（`consumed_event` 表，主键 `(event_id, consumer_group)`）：

```text
Kafka poll -> 开本地 MySQL 事务
  -> INSERT consumed_event
       主键冲突 => 已处理过，直接 ack
       插入成功 => 同事务内执行业务处理
  -> commit -> ack offset
```

**幂等记录与业务必须同事务**：若分两个事务，"幂等插入成功但业务失败"会产生"标记已处理却没执行"的空洞，消息被永久跳过。

**四层限流 + 自适应背压**（生产侧）：

| 层 | 机制 | 保护对象 |
|---|---|---|
| ① 领取节流 | `fixed-delay` + `batch-size`（如 100 条/秒/实例） | DB |
| ② 全局令牌桶 | 事件/s 速率上限，允许 burst | Kafka Producer/broker |
| ③ 按 topic 公平 | 加权领取 + 内存加权轮询，防热点饿死低 volume 高紧急 topic | topic 间公平性 |
| ④ 在途上限 | `Semaphore` 限制未 ack 的 send 数 | 内存与 Producer 缓冲 |
| ⑤ 自适应背压 | 发送延迟 p99 / 失败率恶化时乘性缩减批大小，恢复后加性恢复 | 病态集群不被加重 |

**DLT 与重放**：DLT topic 命名 `<source-topic>.DLT`，必须有告警、保留原始 payload/headers/异常摘要、人工处理或补偿，不静默丢弃。三种重放场景：① 生产侧死信修复后重置 PENDING 重投；② 消费端 `auto-offset-reset=earliest` 重建投影；③ 强制重处理必须换新 `consumer_group`（绕过幂等）。**关键认知：幂等保证"不重复执行"，重放想"重复执行"必须主动绕过幂等。**

### 1.3 Kafka 事件骨干：topic 命名与协作模式

- **命名规范**：`<domain>.<aggregate>.<dimension>`，表达业务语义而非技术来源（不用 `service-name.event`），13 个命名空间承载近百个领域事件。
- **13 个业务命名空间清单**（走 Outbox 的业务契约事件；`mes.*` 与 `wip.*` 同属在制品执行上下文，文档口径合并计为 13）：

| 服务 | 命名空间 | 发布上下文 | 典型 topic |
|---|---|---|---|
| 生产执行 | `mes.*` | 在制品执行（写侧） | `mes.checkpoint.lifecycle`、`mes.routing.progress`、`mes.workorder.progress`、`mes.testresult.structured`、`mes.fixture.used`、`mes.malfunction.reported` |
| 生产执行 | `wip.*` | 在制品执行（读侧投影） | `wip.position.changed`、`wip.location.snapshot` |
| 生产执行 | `wo.*` | 工单管理 | `wo.order.*`（created/released/started/completed/closed…）、`wo.kit.status` |
| 生产执行 | `fai.*` | 首件处理 | `fai.article.*`、`fai.flow.*`、`fai.unit.bound` |
| 生产执行 | `rework.*` | 返修（单件） | `rework.task.*`、`rework.diagnosis`、`rework.verification` |
| 生产执行 | `brework.*` | 返工（批量） | `brework.order.*` |
| 生产执行 | `schedule.*` | 排产 | `schedule.suggested/confirmed/rescheduled/released/rejected` |
| 制造资源 | `material.*` | 物料 | `material.bom.lifecycle`、`material.inventory.changed`、`material.kit.*` |
| 制造资源 | `process.*` | 工艺管理 | `process.route.lifecycle`、`process.operation.lifecycle`、`process.parameter.template` |
| 制造资源 | `quality.*` | 质量 | `quality.gate.lifecycle`、`quality.inspection.verdict`、`quality.anomaly.batch` |
| 设备管理 | `eam.*` | 设备工装台账 | `eam.asset.lifecycle/availability/metric/scrap` |
| 设备管理 | `pm.*` | 点检保养 | `pm.inspection.*`、`pm.maintenance.*`、`pm.fixture.life` |
| 设备管理 | `calibration.*` | 计量检定 | `calibration.plan.*`、`calibration.task.*`、`calibration.certificate.*` |
| 设备管理 | `repair.*` | 维修 | `repair.order.*`、`repair.diagnosis`、`repair.escalation` |

- **不计入 13 的两条独立通道**：`dc.*`（设备高频采集，边缘缓冲 + Kafka 直连，不走 Outbox——即简历"双链路分流"的另一条链路）；`agent.*`（Python 旁路跨语言，Agent 模块，简历已忽略）。
- **前缀物理隔离防串台**：`wo.*`（正常工单）/ `brework.*`（批量返工）/ `rework.*`（单件返修）/ `repair.*`（设备维修）四者语义相近但严禁混用，靠前缀物理隔离避免事件路由错误。
- **集群基线**：3 broker / RF=3 / `min.insync.replicas=2`，业务事件保留 7~30 天。

### 1.4 过点五聚合同事务（零分布式事务）

过点判定时五个聚合在**同一本地事务**写入 + Outbox 发布事件：

| 聚合 | 职责 |
|---|---|
| CheckpointRecord | 过点记录，不可变，携带 `routeVersion` 快照 |
| CheckpointDecision | 放行/拦截决策，BLOCK 必带 `blocking_reason` + `failed_checks[]` |
| RoutingProgress | 单件工艺路线推进（当前步序权威） |
| WorkOrderProgress | 工单维度完成量累计（良品/不良品/返工量） |
| TestResult | 测试结果（FTP/HTTP/手工填报异构渠道结构化产出） |

**事务边界原则**（领域总览 §5.3）：过点主事务内**只写本地 DB**，跨服务一律走事件订阅或 REST 查询；测试数据接入与过点判定同事务；设备级高频采集与过点解耦。**为什么不用 Saga/2PC**：过点是车间最高频业务事务（每板秒级），分布式事务的跨服务协调会直接拖垮吞吐与延迟；同库本地事务开销远低，且五者是"过站瞬间"的原子业务事实，要么一起成功要么一起回滚。

**事务内编排**（事件风暴 §N.2）：`CheckpointReleased` 触发同事务的五个命令——`RecordCheckpoint`（写不可变记录，锁 routeVersion）+ `CheckStationSequence`（校验下一站步序，跳站则拦截）+ `AdvanceRouting`（推进单件步序）+ `AccrueWorkOrderProgress`（累计工单良品/不良品量）+ `ReportFixtureUsage`（上报工装使用，经 Outbox 发 `mes.fixture.used`）。

**判定前的六维并行防错校验**（`AntiErrorCheckService`）按数据源分三类：

- **快照类（无一致性窗口）**：工艺版本、物料/工装——工单下达时由工单管理上下文绑定 BOM 与工艺路线版本（领域总览 §2.3/§4.1，创建工单时 REST 绑定），过点校验对绑定的不可变版本读取，缓存里是新版还是旧版都不影响判定，最多未命中走降级 REST；
- **实时投影类（有一致性窗口，治理见 Q10）**：设备准入（可用性）、质量门禁、齐套状态、首件门禁——订阅事件维护本地缓存，未命中降级 REST；
- **非缓存类**：前置工序读写侧同事务数据；设备实时数据按需 REST（≤200ms，超时保守拦截）。

再叠加跳站校验，综合判定放行/拦截。

**关键不变式**：过点记录不可变（INV-12，补传迟到不回填已关单记录，发 `TestResultLateArrived`）；同一 `(sn, work_order_id, station_id)` 不可重复过点（BIZ-04）；工艺路线与 BOM 在工单下达时绑定版本快照，工艺版本变更仅影响变更后首次过点的在制品（INV-CX-02）；`RoutingProgress` COMPLETED 终态拒收一切推进。

### 1.5 上下文内 CQRS 写读分离

- **写侧权威**：5 个写侧聚合（过点事实，毫秒级）。
- **读侧投影**：`WipUnit`（在制品投影型聚合根），进程内订阅写侧 `mes.*` 事件投影位置/状态/流转历史，秒级最终一致。
- **为什么进程内订阅而不走 Kafka**：写读两侧同属生产执行服务、同部署单元——原"在制品追踪上下文"与过点执行的"跨上下文不变式"实为伪跨上下文，2026-07-10 已合并。同服务内走 Kafka 只添延迟和运维成本，不买任何解耦。
- **可重放重建**：投影可从事件流完全重放，不依赖本地状态——读侧丢了能从事件流恢复，写侧不受读侧故障影响。
- **流转历史只增不可改不可删**：位置是历史的投影（`WipUnit.position` = 最后一条 `WipHistoryEntry` 的 `to_station`），历史是不可变事实，天然一致、天然可审计。

### 1.6 双链路架构级分流（地基级决策）

| 维度 | 低频业务契约事件 | 高频采集数据 |
|---|---|---|
| 典型数据 | 过点决策、工单状态、维修派工、工艺版本生效 | 设备遥测、工艺参数采样、单件事件 |
| 管道 | DB 事务 + Outbox + Publisher | 边缘缓冲 + Kafka 直连 |
| 可靠性 | 同事务原子（强一致） | 不丢不重（边缘断点续传 + `msg_id` 去重 + 消费幂等） |
| 单条价值 | 高（缺失=流程断裂） | 低（缺失可容忍，靠聚合兜底） |

**判定准则**：必须和某个业务状态变更原子提交 → 走 Outbox；只是观测事实的搬运、丢一条可容忍 → 走直连。这条架构级排除是 Outbox 不被写爆（写放大 + 行锁竞争 + 复制延迟）的根本前提，也让 Outbox 始终面对低频事件、限流参数可定。

---

## 二、面试 QA

**Q1：为什么拆成 3 个微服务、14 个限界上下文？会不会过度设计？**

A：拆分依据是变化频率、一致性边界和视角分离，不是按"看着大"。制造资源服务是配置型主数据（写少读多），单独成服务避免被高频过点拖累；设备管理服务把资产视角（能不能用）、通信视角（通不通）、执行视角（过不过站）分开；生产执行服务内部过点是毫秒级、工单是天到周级，频率天差地别不能合并。每个"拆"都有可辩护理由：维修 vs 点检（被动事件驱动 vs 主动计划驱动）、返修 vs 返工（单件 vs 批量）、数据接入 vs 台账（通信 vs 资产）。反过来台账与工装没拆，因为生命周期同构、可用性引擎共用、对外契约一致——**拆与不拆都按非主观标准判断，这是 DDD 落地成熟度的标志**。

**Q2：三段式不变式（INV/BIZ/INV-CX）解决了什么问题？BIZ 为什么禁止挂聚合根？**

A：把一致性边界显式分层——INV 是聚合内强一致（事务内始终为真），BIZ 是上下文内跨聚合/跨实例最终一致，INV-CX 是跨上下文最终一致。每条不变式直接告诉你"它靠什么守护、崩了影响多大"。BIZ 禁止挂聚合根，因为聚合根只能访问自己一致性边界内的数据：跨实例约束挂聚合根会导致聚合去查别的实例，破坏一致性边界、引入分布式协调。比如"同一资产不可有未决报废申请"是跨实例约束，用唯一索引保证而不是聚合根前置条件。混在一起就会把"该用唯一索引保证的"误写成"聚合根前置条件"，看着对、实则守不住。

**Q3：Transactional Outbox 比直接发 Kafka 好在哪里？**

A：解决"DB commit 成功但 Kafka 没发出去"的崩溃窗口——直接发 Kafka 时，commit 后、send 前宕机则事件永久丢失。Outbox 把业务表更新和事件记录放同一本地事务，要么一起成功要么一起回滚，未发布事件留在 outbox 表由 Publisher 继续投递。代价是多一次 DB 写和亚秒级异步延迟，所以只用于低频业务契约事件。

**Q4：Outbox 有重复窗口（PUBLISHING 超时恢复重发），怎么保证不重复处理？**

A：这是"不丢"换"可能重复"的固有取舍：Publisher 发送成功、标 SENT 前宕机，PUBLISHING 超时恢复为 RETRYABLE 会重发同一 `event_id`。靠消费端 `event_id + consumer_group` 幂等吸收，所有业务事件消费者必须幂等、无例外。注意 Producer 幂等（`enable.idempotence=true`）只能降低 Producer 内部重试导致的重复写入，**不能消除** Outbox 故障恢复导致的重复——两层机制职责不同，都要有。

**Q5：消费端幂等的具体实现？为什么幂等记录必须和业务同事务？**

A：`consumed_event` 表主键 `(event_id, consumer_group)`。消费时先 INSERT：主键冲突说明该组已处理过，直接 ack；插入成功则在同一本地事务内执行业务处理，commit 后才 ack offset。必须同事务的原因：若分两个事务，"幂等插入成功但业务失败"会产生"标记已处理却没执行"的空洞，消息被永久跳过。commit 成功但 ack 前崩溃的场景，Kafka 重投后靠幂等表跳过。

**Q6：partition_key 怎么选？为什么禁止随机 UUID 作 key？**

A：同一聚合根的事件必须有序，`partition_key` 用聚合根 ID（工单事件用工单 ID、资产事件用资产 ID、批次事件用批次 ID），保证同聚合根事件落同一分区。禁止随机 UUID，否则同聚合根事件散落多分区，分区内有序无法保证、跨分区又无全局序。配合幂等 Producer（`max.in.flight≤5`），Kafka 保证分区内顺序即使重试也不乱序。

**Q7：四层限流会不会破坏分区内顺序？**

A：不会。**限流是速率维度，顺序是分区维度，两者正交**。生产侧：令牌桶/在途上限/公平调度只改变"何时发、发多快、先发哪个 topic"；领取按 `created_at`、发送按 `producer.send()` 调用顺序，幂等 Producer 保证分区内不乱序。消费侧：同一分区单线程消费，令牌桶/Bulkhead/pause 只延迟处理，不跨分区乱序。

**Q8：过点五聚合同事务会不会太重？为什么不用 Saga？**

A：反而比分布式事务轻。过点判定本来就要同时写过点记录/决策/路线推进/工单进度/测试结果，这五者是"过站瞬间"的原子业务事实，要么一起成功要么一起回滚；同库本地事务开销远低于 Saga/2PC 的跨服务协调。真正会拖垮过点的是把跨服务调用塞进主事务（如同步扣库存），所以物料消耗走最终一致、配置查询走缓存降级，主事务内只写本地 DB。过点是车间最高频业务事务（每板秒级），分布式事务会直接拖垮吞吐与延迟。

**Q9：CQRS 读侧投影和写侧不一致怎么办？投影丢了能恢复吗？**

A：`WipUnit.position` 是最终一致的投影，与写侧 `RoutingProgress` 存在秒级延迟，这是 CQRS 的固有特征，不保证强一致——任何位置查询都应接受"最近一次过点事件可能尚未投影"的语义。投影可幂等重放重建：位置/状态/历史可从 `mes.routing.progress` 事件流完全重放，不依赖本地状态，读侧丢了能从事件流恢复，写侧不受读侧故障影响。

**Q10：六维并行防错校验时，如果缓存内容和实际不一致，如何处理？**

A：先分两种成因，处理路线不同；且六维中只有部分维度存在此问题。**① 缓存未命中**（所有读缓存的维度）：降级 REST 同步查权威源（工艺 ≤500ms、设备 ≤200ms）→ 回填缓存后继续判定 → 超时/失败则 `CheckpointBlocked(RouteCacheMiss / EquipmentCacheMiss)` 保守拦截；降级查询不进过点主事务（BIZ-05），失败不产生半成品状态。**② 缓存滞后**（配置已发布、事件在途、读到旧值）：工艺版本与物料/工装两维不存在此问题——工单下达时由工单管理上下文绑定 BOM 与工艺路线版本，过点校验对绑定的不可变版本读取，缓存里是新版还是旧版都不影响判定（INV-CX-02 的版本锁定在此落地，工艺变更只影响变更后首次过点的在制品）。真正暴露在窗口下的是设备准入、质量门禁、齐套、首件门禁等实时状态投影，缓解由重到轻：**把强防错维度从异步事件摘出去**——同服务部署时，设备可用性阻塞（点检异常/超期、保养超期、寿命超限、CRITICAL 故障）由台账在同一本地事务内同步更新 `AvailabilityProjection`（台账 INV-CX-08），最危险的"停用"判定不过 Kafka 最终一致窗口；**关键变更后短暂双读校验**（🔴 强防错场景启用、常态关）；**降级率监控**——命中率 >99% 是常态，降级率异常升高说明订阅链路断了。补充：前置工序读写侧同事务数据、设备实时数据不缓存按需查询（超时保守拦截，INV-10），均无缓存窗口；所有异常路径统一收敛到保守拦截 + `blocking_reason` + `failed_checks[]`（12 种 `BlockingReason` 枚举），并提供 `OverrideCheckpoint` 授权放行逃生通道（记录例外而非修改原记录）；配套 `rule_id + version` 粒度缓存键（弃用精确移除条目、不整批失效）、冷启动缓存预热（新消费组 `earliest` 补投影）+ 降级查询限流熔断。另注意版本口径分离：降级 REST 返回"当前生效版"用于本次判定，过点记录锁定"过点当时版"用于追溯，两者刻意不同。

依据：在制品执行上下文 §2.3/§2.4、领域总览 §2.3/§4.1/§5.1/§5.2、Kafka 全景模式 A 重难点 1~4、台账 INV-CX-08。

**Q11：高频采集为什么不走 Outbox？两条链路的边界怎么判定？**

A：Outbox 每条事件 = 一次 INSERT + Publisher 至少一次 UPDATE，靠 MySQL 本地事务买强一致。高频采集（秒级持续流）走 Outbox 会把 `outbox_event` 写爆（写放大 + 行锁竞争 + 复制延迟），限流的四类压力全部恶化。判定准则：**必须和某个业务状态变更原子提交 → 走 Outbox；只是观测事实的搬运、丢一条可容忍、靠聚合/重传兜底 → 走边缘缓冲 + Kafka 直连**。这条架构级排除是 Outbox 不被写爆的根本前提。

**Q12：近百个领域事件，schema 怎么演进？**

A：新增字段必须可选或有默认值；不删除已有字段、不改变语义与类型；重大不兼容变更用新 `event_version` 或新 `event_type`；每条事件必带 `event_type + event_version`，消费方据此分发。统一 Envelope（`event_id`/`event_type`/`event_version`/`trace_id`/`correlation_id`/`causation_id`）是幂等去重、监控聚合、schema 演进、链路串联的统一入口。

**Q13：DLT 堆积了怎么处理？重放和幂等的冲突怎么理解？**

A：DLT 不能静默丢弃：必须有告警（`DEAD_LETTER > 0` 持续 5 分钟为高）、保留原始 payload/headers/异常摘要、人工处理或补偿流程；生产侧死信可重放（修复后重置 PENDING 重新投递，消费端幂等吸收重复）。关键认知：**幂等保证"不重复执行"，重放想"重复执行"必须主动绕过幂等**——单纯重放会被幂等表跳过、不会重跑业务。要重跑就换新 `consumer_group` 从头消费（`consumed_event` 用新组名，逻辑上等同首次处理），这是 Outbox 重放最易踩的坑。

**Q14：跨 6+ 上下文的长链路最终一致，怎么观测"卡在哪一步"？**

A：`correlation_id`（业务流程关联）+ `causation_id`（因果上游）贯穿全链，配合 `trace_id` 做端到端时序还原；关键节点（工单状态、过点进度、齐套状态）建读模型投影供"当前状态"查询；DLT 告警 + 人工处理流程，不静默丢弃。这是事件编排相对同步编排最痛的运维点——没有中心编排器可查"当前到哪了"，只能靠三段 ID + 读模型投影 + DLT 告警补上可观测性。

**Q15：如果设备实际故障，但可用性缓存还没更新（陈旧命中），怎么办？**

A：这是"缓存陈旧命中"（stale hit）——与未命中不同：未命中会走降级 REST 拿到真相，陈旧命中（缓存里还是 `available=true`）会直接通过设备准入校验，必须专门治理。四层防御由近及远：**① 台账侧零延迟**——强防错阻塞（CRITICAL 故障、点检异常/超期、保养超期、寿命超限）在设备管理服务内部走本地事务同步更新 `AvailabilityProjection`（台账 INV-CX-08 / 维修 INV-CX-09），故障进台账不等 Kafka；但这只消除设备管理服务内部的窗口，台账到生产执行缓存的跨服务传播仍走 Kafka、有秒级滞后。**② 跨服务窗口双读校验**（🔴 决策点，默认强防错场景启用、常态关）——`AssetAvailabilityChanged(available=false)` 到达后的短暂窗口内，设备准入校验做"缓存 + 权威源"双读取严；常态关闭时窗口期靠下面两层兜底。**③ 关键工序实时数据校验**（INV-10，绕过可用性缓存）——关键工序过点按需 REST 直查设备实时数据（≤200ms）：通信中断 → `EquipmentDataTimeout` 保守拦截；程序版本/参数异常 → `EquipmentDataOutOfRange` 拦截；通信类故障在窗口期内也能拦住，局限是仅关键工序启用、覆盖不了"通信正常但机械故障"。**④ 误放行的事后闭环**（补救层）——过点记录绑定设备与 `routeVersion`，设备停用事件有时序记录，质量上下文异步判定（`QualityVerdictIssued`）可标记异常批次，返修/返工流程补救。防守话术：先讲"故障进台账本地事务零延迟"，再讲"跨服务窗口靠双读 + 实时数据直查兜底"，最后讲"误放行由追溯事后闭环"，并主动说明双读是待拍板的决策点——区分确定机制与待定权衡，比全盘打包票更显判断力。

依据：设备管理服务-面试闪光点 §3.4、在制品执行上下文 §2.4、Kafka 全景模式 A 重难点 1 与附录 A、台账 INV-CX-08 / 维修 INV-CX-09。

---

## 三、讲解使用建议

- **面试主攻 Q1/Q3/Q8/Q10/Q11**：分别对应"拆分判断力、崩溃窗口理解、事务选型、缓存一致性治理、架构级分流"五个最易拉开差距的点。
- **被追问实现细节时的下钻顺序**：表结构 → 状态机 → 领取 SQL → 幂等同事务 → 限流与顺序正交，能讲到 SQL 和配置级即证明落地过；缓存一致性被深挖时按 Q10（未命中 / 滞后分层）→ Q15（陈旧命中的设备故障场景）应答。
- **口径纪律**：本维度所有机制均有文档出处；量化数据（投递成功率、缺陷下降比例等）为简历示例值，被问"怎么测的"时按真实情况回答，机制本身不受影响。
