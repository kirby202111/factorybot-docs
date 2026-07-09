# Outbox 设计方案

> **定位**：本文是本 MES 项目 Transactional Outbox 的**权威完整方案**，自包含地覆盖表结构、与 Kafka 的交互、限流与背压、可观测、重放与补偿、运维清理。
>
> **与既有文档的关系**：原《[消息处理实现说明](消息处理实现说明.md)》《[MySQL配置说明](MySQL配置说明.md)》《[Kafka配置说明](Kafka配置说明.md)》三篇的内容已整合并入本文，**以本文为准**。三篇旧文顶部已加废弃指引，建议后续归档或删除，避免双份事实源。
>
> **适用边界**：仅用于**低频业务契约事件**（过点 / 工单 / 维修 / 物料 / 质量 / 工艺版本生效 / 资产生命周期等）。**高频设备原始采集数据不走 Outbox**，走"边缘缓冲 + Kafka 直连"（见 [领域总览](../../领域模型/领域总览.md) §5.3、[设备数据接入上下文](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md)）。这条架构级排除是 Outbox 不被写爆的根本前提，详见 §9.1。
>
> **可靠性语义**：至少一次投递（at-least-once）+ 消费端 `event_id` 幂等，等效一次处理。

---

## 0. 口径纪律

本文出现的吞吐/限流数值均为**设计容量目标 + 假设**，不是线上实绩（参见 [项目亮点与指标卡片](../../面试指南/项目亮点与指标卡片.md) §0）。所有需要按真实集群容量拍板的阈值用 🔴 标注，交还运维/架构决策，**不自行编造 SLA**。

---

## 1. 设计目标与要解决的问题

领域服务处理业务命令时通常要同时做两件事：

1. 修改本服务数据库中的业务状态；
2. 发布业务事件，通知其它服务或后续处理流程。

若业务代码在数据库事务提交后**直接**调用 Kafka Producer，存在崩溃窗口：

```text
DB commit 成功
  -> 应用进程在 send Kafka 前宕机
  -> Kafka 永远收不到该业务事件
```

Transactional Outbox 的目标：把"业务状态变更"和"待发布事件记录"放进**同一个本地数据库事务**提交，再由后台 Publisher 异步投递 Kafka。

```text
同一个本地事务：
  1. 更新业务表
  2. 插入 outbox_event(PENDING)

事务提交后：
  3. Outbox Publisher 异步读取 outbox_event
  4. 发布 Kafka
  5. 更新 outbox_event 状态
```

这样任意时刻崩溃，未发布成功的事件仍保留在 outbox 表中，由后台 Publisher 继续投递。

**与"直接发 Kafka"的对比**：

| 维度 | 直接发 Kafka | Transactional Outbox |
|---|---|---|
| 一致性 | DB commit 与 Kafka 发送非原子，有崩溃窗口 | 同本地事务原子，崩溃零丢失 |
| 延迟 | 提交即发，最低 | 多一次 DB 写 + 异步投递，有亚秒级延迟 |
| DB 压力 | 无额外写 | 每事件多一次 INSERT + 状态更新 |
| 适用 | 高频原始数据（采集） | 低频业务契约事件 |

结论：**业务契约事件走 Outbox，高频采集走直连**，是本项目的刻意分工（见 §9.1）。

---

## 2. 整体架构

```text
┌──────────────────────────────────┐
│ Application Service              │
│  - 处理命令 / 调用聚合根          │
│  - 保存业务状态                   │
│  - 写入 outbox_event (同事务)     │
└──────────────┬───────────────────┘
               │ 同一个本地 DB 事务
               ▼
┌──────────────────────────────────┐
│ Business Tables + outbox_event   │
└──────────────┬───────────────────┘
               │ afterCommit 唤醒 + 定时轮询兜底
               ▼
┌──────────────────────────────────┐
│ Outbox Publisher                 │
│  - 领取(FOR UPDATE SKIP LOCKED)  │
│  - 限流令牌桶 / 公平调度          │
│  - 发送 Kafka                     │
│  - 更新 outbox 状态               │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│ Kafka (acks=all + 幂等 Producer) │
└──────────────┬───────────────────┘
               ▼
┌──────────────────────────────────┐
│ Consumers                        │
│  - 幂等消费 (event_id)           │
│  - 本地事务处理 + 手动 ack        │
│  - 限流 / Bulkhead / 慢消费者背压 │
│  - 失败重试 / DLT                 │
└──────────────────────────────────┘
```

---

## 3. 表结构设计

### 3.1 outbox_event DDL

建议每个服务拥有自己的 outbox 表，服务内统一一张 `outbox_event`，用 `source_context` / `topic` / `aggregate_type` 区分上下文（若服务内有独立数据库或强隔离需求，再按上下文拆表）。

```sql
CREATE TABLE outbox_event (
    id                  VARCHAR(64)   NOT NULL,
    event_id            VARCHAR(64)   NOT NULL,
    topic               VARCHAR(128)  NOT NULL,
    event_type          VARCHAR(128)  NOT NULL,
    event_version       INT           NOT NULL DEFAULT 1,

    aggregate_type      VARCHAR(64)   NOT NULL,
    aggregate_id        VARCHAR(128)  NOT NULL,
    aggregate_version   BIGINT        NULL,

    partition_key       VARCHAR(128)  NOT NULL,
    payload             JSON          NOT NULL,
    headers             JSON          NULL,

    source_service      VARCHAR(64)   NOT NULL,
    source_context      VARCHAR(64)   NOT NULL,
    trace_id            VARCHAR(128)  NULL,
    correlation_id      VARCHAR(128)  NULL,
    causation_id        VARCHAR(128)  NULL,

    status              VARCHAR(32)   NOT NULL,
    retry_count         INT           NOT NULL DEFAULT 0,
    max_retry_count     INT           NOT NULL DEFAULT 20,
    next_retry_at       DATETIME(3)   NOT NULL,

    locked_by           VARCHAR(128)  NULL,
    locked_at           DATETIME(3)   NULL,

    last_error_code     VARCHAR(128)  NULL,
    last_error_message  TEXT          NULL,

    occurred_at         DATETIME(3)   NOT NULL,
    created_at          DATETIME(3)   NOT NULL,
    published_at        DATETIME(3)   NULL,
    dead_lettered_at    DATETIME(3)   NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uk_outbox_event_id (event_id),
    KEY idx_outbox_publishable (status, next_retry_at, created_at),
    KEY idx_outbox_aggregate (aggregate_type, aggregate_id, created_at),
    KEY idx_outbox_topic_status (topic, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

设计要点：

- `DATETIME(3)` 保留毫秒，便于计算发布延迟（`published_at - created_at`）。
- `uk_outbox_event_id`：同一事件不会重复写入 outbox（生产侧去重）。
- `idx_outbox_publishable (status, next_retry_at, created_at)`：支撑 Publisher 按 `status + next_retry_at + created_at` 高效扫描待发布事件，是领取查询的主索引。
- `idx_outbox_topic_status (topic, status, created_at)`：支撑按 topic 公平领取（§9.2）与按 topic 的积压监控。
- `idx_outbox_aggregate`：支撑按聚合根排查事件序列。
- `payload` / `headers` 用 `JSON`；若 MyBatis 类型处理成本高可改 `TEXT` 存 JSON 字符串。

### 3.2 字段说明

| 字段 | 说明 |
|---|---|
| `id` | outbox 主键，可与 `event_id` 相同或独立生成。 |
| `event_id` | 全局事件唯一 ID，**消费端幂等去重核心字段**。 |
| `topic` / `event_type` / `event_version` | Kafka topic、事件类型、契约版本（schema 演进）。 |
| `aggregate_type` / `aggregate_id` / `aggregate_version` | 聚合根类型 / ID / 版本，可用于消费方判顺序或并发冲突。 |
| `partition_key` | Kafka 分区键，通常用聚合根 ID。 |
| `payload` / `headers` | 事件正文 / Kafka headers 业务扩展。 |
| `source_service` / `source_context` | 来源服务 / 限界上下文。 |
| `trace_id` / `correlation_id` / `causation_id` | 链路追踪 / 业务流程关联 / 因果上游 ID。 |
| `status` | 消息状态（见 §4）。 |
| `retry_count` / `max_retry_count` | 已重试 / 上限。 |
| `next_retry_at` | 下次可投递时间（退避后）。 |
| `locked_by` / `locked_at` | 被哪个 Publisher 实例锁定 / 锁定时间。 |
| `last_error_*` | 最近一次失败原因。 |
| `occurred_at` / `created_at` / `published_at` / `dead_lettered_at` | 事件发生 / 入表 / 成功发布 / 进死信时间。 |

### 3.3 消费幂等表 consumed_event

消费 Kafka 业务事件的服务建立幂等表：

```sql
CREATE TABLE consumed_event (
    event_id          VARCHAR(64)   NOT NULL,
    consumer_group    VARCHAR(128)  NOT NULL,
    topic             VARCHAR(128)  NOT NULL,
    event_type        VARCHAR(128)  NOT NULL,
    processed_at      DATETIME(3)   NOT NULL,

    PRIMARY KEY (event_id, consumer_group),
    KEY idx_consumed_event_processed_at (processed_at),
    KEY idx_consumed_event_topic (topic, processed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

消费端用主键 `(event_id, consumer_group)` 做幂等：先 `INSERT consumed_event`，主键冲突说明该组已处理过该 `event_id`，直接 ack；插入成功则在同一本地事务内执行业务处理。**幂等记录与业务处理必须同事务提交**（见 §8.3）。

### 3.4 索引代价与大表治理

- `outbox_event` 是**高写表**：每条业务事件一次 INSERT，Publisher 每条至少一次 UPDATE（领取置 PUBLISHING + 终态更新）。索引越多写放大越严重，因此只保留上述 4 个必要索引。
- `SENT` 行需及时清理（§12），否则 `idx_outbox_publishable` 扫描时仍要越过大量历史行（虽然 `status` 前缀能过滤，但行数膨胀会拖慢统计与备份）。
- 🔴 **大表治理策略二选一**（按实际写入量决策）：①按月对 `outbox_event` 做 `RANGE PARTITION`（按 `created_at`），清理旧分区用 `DROP PARTITION` 替代 `DELETE`；②保持单表 + 定期归档 `SENT`/`DEAD_LETTER` 到归档表或对象存储。MES 低频业务事件量级下通常单表 + 归档即可，分区仅在确认单表超千万行后再引入。

---

## 4. Outbox 状态机

| 状态 | 可被 Publisher 拉取 | 终态 | 说明 |
|---|---:|---:|---|
| `PENDING` | 是 | 否 | 初始状态，等待发布。 |
| `PUBLISHING` | 否 | 否 | 已被 Publisher 领取，正在发送。 |
| `SENT` | 否 | 是 | 已成功发送到 Kafka。 |
| `RETRYABLE` | 是，但需到达 `next_retry_at` | 否 | 上次发送失败，等待重试。 |
| `DEAD_LETTER` | 否 | 是 | 重试耗尽或不可恢复错误，等待人工处理或补偿。 |
| `DISCARDED` | 否 | 是 | 人工确认不再发布。 |

```text
               ┌────────────────────────┐
               │                        ▼
PENDING ──-> PUBLISHING ──send ok──-> SENT
   ▲            │
   │            ├─send failed, retryable──-> RETRYABLE
   │            │                            │
   │            │                            └─next_retry_at reached──┘
   │            │
   │            ├─retry exhausted────────-> DEAD_LETTER
   │            │
   │            └─lock timeout───────────-> RETRYABLE
   │
   └─manual replay from DEAD_LETTER, if fixed

DEAD_LETTER ──manual discard──-> DISCARDED
```

---

## 5. 生产侧实现

### 5.1 应用服务事务

应用服务必须在**同一个** `@Transactional` 中完成业务状态变更和 outbox 写入：

```java
@Transactional(rollbackFor = Exception.class)
public void handleCommand(BusinessCommand command) {
    Aggregate aggregate = repository.getById(command.aggregateId());

    DomainEvent event = aggregate.handle(command);   // 聚合根只返回事件

    repository.save(aggregate);

    OutboxEvent outboxEvent = outboxEventFactory.create(
        event,
        topicResolver.resolve(event),
        partitionKeyResolver.resolve(event)
    );

    outboxEventRepository.save(outboxEvent);
}
```

要求：

- 业务状态变更与 outbox 写入在**同一本地事务**提交。
- 事务内只写本地数据库，**不直接发 Kafka**。
- 事务回滚则业务状态与 outbox 记录一起回滚。

### 5.2 聚合根职责（纯洁性）

聚合根只表达业务事实，**不依赖消息基础设施**：

```java
public class Aggregate {
    public DomainEvent handle(BusinessCommand command) {
        // 校验业务规则
        // 修改聚合状态
        // 返回领域事件（不发送）
    }
}
```

禁止聚合根直接依赖：`KafkaTemplate`、`ApplicationEventPublisher`、`OutboxRepository`、任何消息发送器。由**应用服务**编排 outbox 写入，聚合根保持 OOD 纯洁（SRP：聚合根只负责业务不变式）。

### 5.3 事件 Envelope

业务事件统一使用 Envelope（字段、Kafka Header、版本演进见 §7.6/§7.7）。统一 Envelope 的目的：消费方用 `event_id` 幂等；监控按 `event_type`/`source_service` 聚合；schema 演进有统一入口；`trace_id` 串联链路。

### 5.4 afterCommit 快速路径

为降低延迟，事务提交后触发一次本地唤醒信号，仍保留后台轮询兜底：

```java
@Transactional(rollbackFor = Exception.class)
public void handleCommand(BusinessCommand command) {
    // 保存业务状态 + 写 outbox_event

    TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
        @Override
        public void afterCommit() {
            outboxPublishSignal.tryWakeup();   // 只唤醒，不替代 Publisher
        }
    });
}
```

注意：`afterCommit` 仅唤醒 Publisher；即使未执行，定时任务仍扫 `PENDING`。极低延迟场景可用内存队列传递 outbox id，但**发布前仍须从 DB 读取并校验状态**（内存信号不可作为可靠性来源）。

---

## 6. Outbox Publisher

### 6.1 定时扫描

```java
@Component
public class OutboxPublisherJob {
    private final RateLimitedOutboxPublisher publisher;

    @Scheduled(fixedDelayString = "${app.outbox.publisher.fixed-delay:1000}")
    public void publish() {
        publisher.publishBatch();
    }
}
```

### 6.2 多实例领取（避免重复处理）

领取要求（具体 SQL 见 §6.3）：

- 只领取 `PENDING` / `RETRYABLE` 且到达 `next_retry_at` 的记录。
- 领取与置 `PUBLISHING` 在**同一短事务**完成。
- **Kafka 发送不在领取事务内**。
- `PUBLISHING` 超时后必须可恢复为 `RETRYABLE`。

### 6.3 领取 / 状态更新 / 恢复 SQL

**领取（MySQL 8.0，`FOR UPDATE SKIP LOCKED`）：**

```sql
START TRANSACTION;

SELECT *
FROM outbox_event
WHERE status IN ('PENDING', 'RETRYABLE')
  AND next_retry_at <= CURRENT_TIMESTAMP(3)
ORDER BY created_at
LIMIT :batchSize
FOR UPDATE SKIP LOCKED;

UPDATE outbox_event
SET status = 'PUBLISHING',
    locked_by = :instanceId,
    locked_at = CURRENT_TIMESTAMP(3)
WHERE id IN (:ids);

COMMIT;
```

**按 topic 公平领取**（§9.2 启用时，对活跃 topic 逐个领取小子批，保证一个热点 topic 不饿死其它 topic）：

```sql
-- 对每个活跃 topic 执行一次，topicMaxPerTick 为该 topic 本轮配额
SELECT id FROM outbox_event
WHERE topic = :topic
  AND status IN ('PENDING', 'RETRYABLE')
  AND next_retry_at <= CURRENT_TIMESTAMP(3)
ORDER BY created_at
LIMIT :topicMaxPerTick
FOR UPDATE SKIP LOCKED;
```

**标记 SENT：**

```sql
UPDATE outbox_event
SET status = 'SENT',
    published_at = CURRENT_TIMESTAMP(3),
    locked_by = NULL, locked_at = NULL,
    last_error_code = NULL, last_error_message = NULL
WHERE id = :id AND status = 'PUBLISHING';
```

**标记 RETRYABLE（带退避）：**

```sql
UPDATE outbox_event
SET status = 'RETRYABLE',
    retry_count = :retryCount,
    next_retry_at = :nextRetryAt,
    locked_by = NULL, locked_at = NULL,
    last_error_code = :lastErrorCode,
    last_error_message = :lastErrorMessage
WHERE id = :id AND status = 'PUBLISHING';
```

**标记 DEAD_LETTER：**

```sql
UPDATE outbox_event
SET status = 'DEAD_LETTER',
    retry_count = :retryCount,
    locked_by = NULL, locked_at = NULL,
    last_error_code = :lastErrorCode,
    last_error_message = :lastErrorMessage,
    dead_lettered_at = CURRENT_TIMESTAMP(3)
WHERE id = :id AND status IN ('PUBLISHING', 'RETRYABLE');
```

**PUBLISHING 锁超时恢复：**

```sql
UPDATE outbox_event
SET status = 'RETRYABLE',
    locked_by = NULL, locked_at = NULL,
    next_retry_at = CURRENT_TIMESTAMP(3),
    last_error_code = 'PUBLISHING_LOCK_TIMEOUT',
    last_error_message = 'Publisher lock timeout, reset to retryable'
WHERE status = 'PUBLISHING'
  AND locked_at < CURRENT_TIMESTAMP(3) - INTERVAL 5 MINUTE;
```

> 注意：若 Kafka 已发送成功但来不及标 `SENT` 就宕机，恢复后会**重复发送同一 `event_id`**，消费端必须幂等（§8.3、§7.8）。

### 6.4 发送 Kafka

```java
public void publishOne(OutboxEvent event) {
    kafkaOutboxMessageSender.send(event);
    outboxEventRepository.markSent(event.getId(), Instant.now());
}
```

- 低吞吐：同步等待 Kafka ack，逻辑简单。
- 中高吞吐：异步 future + 批量状态更新。
- 失败按异常类型 + 重试次数转 `RETRYABLE` 或 `DEAD_LETTER`。

### 6.5 重试与退避策略

- `retry_count` 递增，超过 `max_retry_count` 进 `DEAD_LETTER`。
- 退避用**指数退避 + 抖动**，封顶 `max_retry_delay`：

```text
next_retry_at = now + min(max_retry_delay, initial_retry_delay * 2^retry_count) * (0.8 ~ 1.2 抖动)
```

- **不可恢复错误**直接进 `DEAD_LETTER`，不重试：序列化失败、topic 不存在、payload schema 非法等（对应 §8.4 消费侧的 `addNotRetryableExceptions` 思路）。
- 🔴 `max_retry_count=20`、`initial_retry_delay=5s`、`max_retry_delay=30m` 为默认值，按业务 SLA 调整。

---

## 7. 与 Kafka 交互

### 7.1 Topic 命名

统一 `<domain>.<aggregate>.<dimension>`，表达业务语义而非技术来源，不用 `service-name.event`：

```text
eam.asset.lifecycle      eam.asset.availability     eam.asset.scrap
mes.workorder.lifecycle  mes.pass.point             mes.repair.order
mfg.process.version      mfg.material.batch         mfg.quality.gate
```

高频设备原始数据、遥测、业务领域事件使用**不同 topic 命名空间和保留策略**，不混在同一 topic。

### 7.2 分区键与顺序保证

原则：**同一聚合根的事件必须有序**。

| 场景 | partition key |
|---|---|
| 聚合根生命周期 / 状态变更 | 聚合根 ID |
| 流程单据事件 | 单据 ID |
| 资产 / 设备类事件 | 资产 / 设备 ID |
| 批次类事件 | 批次 ID |

要求：

- `ProducerRecord` 的 key 用 `partition_key`（通常 = 聚合根 ID）。
- 同一聚合根生命周期事件用**同一 key**，落同一分区，保序。
- **禁止**用随机 UUID 作 Kafka key，否则同聚合根事件散落多分区，顺序无法保证。

**顺序与限流的兼容**（详见 §9.5）：令牌桶只节流发送**速率**，不改变发送**顺序**。Publisher 按 `created_at` 领取、按分区提交顺序调用 `producer.send()`，配合幂等 Producer（`enable.idempotence=true` + `max.in.flight.requests.per.connection<=5`），Kafka 保证**分区内顺序即使在重试时也不乱序**。

### 7.3 Topic 创建参数

```bash
kafka-topics.sh --create \
  --topic mes.repair.order \
  --bootstrap-server kafka-1:9092,kafka-2:9092,kafka-3:9092 \
  --partitions 12 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete
```

| 参数 | 建议 | 说明 |
|---|---:|---|
| `partitions` | 6/12/24 起步 | 按吞吐与消费并行度调整；后续可增但影响 key 分布。 |
| `replication.factor` | 3 | 生产环境 3 副本。 |
| `min.insync.replicas` | 2 | 配合 Producer `acks=all`。 |
| `retention.ms` | 7~30 天 | 保留足够长以支持故障恢复与新消费者追赶。 |
| `cleanup.policy` | `delete` | 业务事件保留完整日志，不用 compact 替代。 |

生产环境**不依赖 topic 自动创建**。需长期可重放的事件不要只靠 Kafka retention，应归档到对象存储/数据湖/事件审计库（§13）。

### 7.4 Producer 配置

```yaml
spring:
  kafka:
    producer:
      acks: all
      retries: 10
      properties:
        enable.idempotence: true
        max.in.flight.requests.per.connection: 5
        delivery.timeout.ms: 120000
        request.timeout.ms: 30000
        retry.backoff.ms: 1000
        linger.ms: 10
        batch.size: 32768
        compression.type: zstd
```

| 配置 | 推荐值 | 说明 |
|---|---:|---|
| `acks` | `all` | 等 leader + ISR 确认，避免只写 leader 后丢失。 |
| `enable.idempotence` | `true` | Producer 幂等，降低内部重试导致的重复写入，并保分区内顺序。 |
| `retries` | `10`+ | Kafka 客户端内部重试；上层 Outbox 还有业务层重试。 |
| `delivery.timeout.ms` | `120000` | Producer 完成发送的总超时。 |
| `linger.ms` | `5~20` | 攒批提吞吐；低延迟要求可设小。 |
| `compression.type` | `zstd`/`lz4` | 减网络与 broker 存储压力。 |
| `max.in.flight.requests.per.connection` | `5` | 开启幂等时 Kafka 建议不超过 5。 |

### 7.5 是否需要 Kafka 事务 Producer

**通常不需要**。原因：业务状态与 outbox 一致性由 MySQL 本地事务保证；Kafka 发布失败由 outbox 重试保证；消费方 `event_id` 幂等处理重复。Kafka 事务 Producer 解决的是"Kafka 内部多 topic/partition 原子写"，**不能**解决"MySQL 事务 + Kafka 发送"的原子性。因此优先：`普通 Producer + 幂等 Producer + Transactional Outbox`。

### 7.6 事件 Header

Producer 写入以下 header（payload 中也保留这些 envelope 字段，header 用于路由/过滤/追踪/排障）：

| Header | 说明 |
|---|---|
| `event_id` | 全局事件唯一 ID。 |
| `event_type` / `event_version` | 事件类型 / 版本。 |
| `source_service` / `source_context` | 来源服务 / 上下文。 |
| `trace_id` / `correlation_id` / `causation_id` | 链路 / 流程关联 / 因果上游。 |

### 7.7 版本演进

- 新增字段必须可选或有默认值。
- 不删除已有字段、不改变语义与类型。
- 重大不兼容变更用新 `event_version` 或新 `event_type`。
- 每条事件必带 `event_type` + `event_version`，消费方据此分发。

### 7.8 可靠性边界（重复窗口）

至少一次投递，业务侧幂等实现等效一次。Kafka Producer 幂等只能降低 Producer 内部重试的重复写入，**不能消除** Outbox 故障恢复导致的重复事件：

```text
Publisher 发送 Kafka 成功
  -> 应用在标 outbox_event=SENT 前宕机
  -> PUBLISHING 超时恢复为 RETRYABLE
  -> Publisher 再次发送同一 event_id
```

故**所有**业务事件消费者必须用 `event_id + consumer_group` 幂等。

---

## 8. 消费侧实现

### 8.1 Consumer 配置

```yaml
spring:
  kafka:
    consumer:
      enable-auto-commit: false
      auto-offset-reset: earliest
      properties:
        isolation.level: read_committed
        max.poll.records: 100
        max.poll.interval.ms: 300000
        session.timeout.ms: 45000
        heartbeat.interval.ms: 15000
    listener:
      ack-mode: manual
      concurrency: 3
```

| 配置 | 推荐 | 说明 |
|---|---:|---|
| `enable-auto-commit` | `false` | 禁止自动提交，避免业务失败但 offset 已提交。 |
| `ack-mode` | `manual` | 业务事务成功后手动 ack。 |
| `auto-offset-reset` | `earliest` | 新消费组从最早开始，适合领域事件补投影。 |
| `max.poll.records` | `100` | 控制单批处理量，避免拉取过多超时。 |
| `max.poll.interval.ms` | `300000`+ | 容忍一个满批处理时长；超时触发 rebalance（配合 §9.3 慢消费者 pause）。 |
| `concurrency` | ≤ 分区数 | 并发不应超过分区数太多。 |
| `isolation.level` | `read_committed` | 上游若用 Kafka 事务则只读已提交；本方案不强依赖，可保留。 |

### 8.2 Listener 与 ack 顺序

```java
@KafkaListener(
    topics = "eam.asset.lifecycle",
    groupId = "repair-service.asset-lifecycle-consumer"
)
public void onMessage(
    ConsumerRecord<String, String> record,
    Acknowledgment acknowledgment
) {
    EventEnvelope envelope = objectMapper.readValue(record.value(), EventEnvelope.class);
    assetLifecycleEventHandler.handle(envelope);
    acknowledgment.acknowledge();
}
```

处理顺序**必须**是：

```text
Kafka poll 收到消息
  -> 开启本地 MySQL 事务
    -> 插入 consumed_event 幂等记录
    -> 执行业务处理
  -> MySQL commit
  -> ack Kafka offset
```

- MySQL commit 前失败：不 ack，Kafka 后续重投。
- MySQL commit 成功但 ack 前崩溃：Kafka 重复投递，消费方靠幂等表跳过。

### 8.3 幂等消费

幂等记录与业务处理在**同一本地事务**：

```text
先 INSERT consumed_event
  -> 主键 (event_id, consumer_group) 冲突 => 已处理过，直接 ack
  -> 插入成功 => 同事务内执行业务处理 => commit => ack
```

### 8.4 消费失败与 DLT

```java
@Bean
public DefaultErrorHandler defaultErrorHandler(KafkaTemplate<String, String> kafkaTemplate) {
    DeadLetterPublishingRecoverer recoverer = new DeadLetterPublishingRecoverer(
        kafkaTemplate,
        (record, ex) -> new TopicPartition(record.topic() + ".DLT", record.partition())
    );
    FixedBackOff backOff = new FixedBackOff(5000L, 5L);
    DefaultErrorHandler errorHandler = new DefaultErrorHandler(recoverer, backOff);
    errorHandler.addNotRetryableExceptions(
        IllegalArgumentException.class,
        EventSchemaException.class
    );
    return errorHandler;
}
```

DLT topic 命名 `<source-topic>.DLT`。要求：DLT 必须有告警；DLT 消息保留原始 payload/headers/异常摘要；DLT 后需人工处理或补偿，**不静默丢弃**。

### 8.5 顺序消费与并发

- 同一分区由同一消费者线程消费（Kafka 保证），故 `concurrency ≤ 分区数` 即可保证分区内顺序。
- 消费侧限流（§9.3）只延迟处理，不跨分区乱序，顺序保证不受影响。

---

## 9. 限流与背压（核心）

> 本节是相对既有文档**新增**的部分。Outbox 把"何时发、发多快"从业务事务里剥离到 Publisher，正好是施加限流与背压的天然控制点。限流的目标不是压低吞吐，而是**在突发（批量工单下发、过点高峰、集群抖动）下保护 Kafka、DB 和下游不被击穿，并保证不同 topic 之间的公平性**。

### 9.1 为什么需要限流：四类压力

| 压力来源 | 触发场景 | 不限流的后果 | 限流手段 |
|---|---|---|---|
| **突发入表** | 批量工单下发 / BOM 导入 / 故障恢复后积压排空 | outbox 表暴写、DB 复制延迟、行锁竞争 | 入口节流（业务侧批量化）+ Publisher 排空速率上限 |
| **Publisher 排空** | 积压数千 PENDING 一次性领取发送 | 打爆 Kafka Producer/broker，拖垮同集群其它 topic 与服务 | 全局令牌桶 + 按 topic 公平 + 在途上限 |
| **DB 领取/写入** | 领取过频、批过大 | 短事务风暴、`outbox_event` 写放大 | 领取节流（fixed-delay + batch-size） |
| **消费侧** | 高 volume topic 消费、消费方下游慢 | 消费方 DB 击穿、下游被放大调用、poll 超时 rebalance | max.poll.records + 令牌桶 + Bulkhead + pause 背压 |

**最根本的限流是架构级排除**：高频设备原始采集数据**不走 Outbox**（采集走边缘缓冲 + Kafka 直连，可靠性靠边缘断点续传 + 平台 `msg_id` 去重 + 幂等消费，不靠 Outbox 强一致，见 [设备数据接入上下文](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md) §0/§2.3）。若采集也走 Outbox，`outbox_event` 会被秒级状态流写爆，四类压力全部恶化。这条边界让 Outbox 始终面对**低频业务事件**，限流参数才好定。

### 9.2 生产侧限流

生产侧分四层，自下而上叠加：

**① 领取节流（claim throttle）——保护 DB、设定自然发送上限**

`fixed-delay` + `batch-size` 给出单实例天然发送上限：`batch-size / (fixed-delay/1000)`。例如 `batch-size=100`、`fixed-delay=1000ms` → ≤100 事件/s/实例。这是最粗的限流，也是 DB 写入保护的第一道。

**② 全局发送令牌桶（global token bucket）——保护 Kafka Producer/broker**

不论领取多少，发送速率受全局令牌桶约束（事件/s，可选叠加字节/s）。突发时允许短暂 burst（令牌桶天然支持 burst = 桶容量）。

**③ 按 topic 公平调度（per-topic fairness）——防热点饿死其它 topic**

积压排空时若按 `created_at` FIFO，一个热点 topic（如批量工单）可能独占多轮领取，把维修 SLA 事件等低 volume 高紧急事件堵在后面。解决：**按 topic 加权领取 + 内存内加权轮询发送**。每个 topic 有配额（事件/s），Publisher 每 tick 对各活跃 topic 领取小子批，再按权重轮询投递，全局令牌桶统一节流。🔴 是否默认启用、各 topic 权重，按业务紧急度定（建议维修/质量门 > 工单 > 物料批次）。

**④ 在途上限（in-flight cap）——保护内存与 Producer 缓冲**

`Semaphore` 限制未完成 ack 的 `producer.send()` 数量，配合 `delivery.timeout.ms`，防止慢 broker 下 Future 堆积 OOM。

**⑤ 自适应背压（adaptive backpressure）——集群不健康时主动减速**

监控发送延迟 p99 与失败率：恶化时**乘性缩减** `batch-size` / 增大 `fixed-delay`（MD），恢复后**加性恢复**（AI）。避免 Publisher 把病态集群打得更糟。

集成示例：

```java
@Component
public class RateLimitedOutboxPublisher {

    private final OutboxEventMapper outboxMapper;
    private final KafkaOutboxMessageSender sender;
    private final RateLimiter globalLimiter;                 // ② 全局令牌桶
    private final Map<String, RateLimiter> topicLimiters;    // ③ 按 topic 配额
    private final Semaphore inflight;                        // ④ 在途上限
    private final AdaptiveClaimController claimController;   // ⑤ 自适应
    private final OutboxProperties props;
    private final String instanceId;

    public void publishBatch() {
        int batchSize = claimController.currentBatchSize();          // ⑤ 动态批大小
        List<OutboxEvent> events = claimFair(batchSize);             // ③ 按 topic 公平领取
        for (OutboxEvent e : fairOrderedByWeight(events)) {
            globalLimiter.acquire();                                 // ② 全局节流
            topicLimiters.getOrDefault(e.getTopic(), unlimited).acquire();
            inflight.acquireUninterruptibly();                       // ④ 在途 +1
            sender.sendAsync(e).whenComplete((ok, ex) -> {
                inflight.release();                                  // ④ 在途 -1
                claimController.record(outcome(ex));                 // ⑤ 反馈健康度
                handleResult(e, ex);                                 // SENT / RETRYABLE / DEAD_LETTER
            });
        }
    }
}
```

> 顺序保证：循环内逐条 `acquire` 后调用 `sendAsync`，`producer.send()` 的调用顺序即提交顺序；幂等 Producer 保证分区内顺序不乱序（§7.2、§9.5）。不同分区可并发完成，互不影响。

### 9.3 消费侧限流

**① 拉取量与并发上限**：`max.poll.records` + `concurrency`（≤ 分区数）天然限流。

**② 消费端令牌桶**：业务处理前 `acquire`，保护消费方自身 DB。

**③ Bulkhead 隔离**：业务处理走有界线程池（与 Kafka listener 线程隔离），限并发在途处理数，避免慢路径耗尽线程触发 `max.poll.interval.ms` 超时 rebalance。

**④ 慢消费者 pause/resume 背压**：当处理速率持续低于到达速率、poll-ack 间隔逼近 `max.poll.interval.ms` 时，主动 `consumer.pause(partitions)` 暂停拉取，处理完积压再 `resume()`。用显式背压替代 rebalance。

**⑤ 下游依赖限流**：消费方若调用其它服务（REST），用 Resilience4j RateLimiter + Bulkhead + CircuitBreaker 包裹，**消费方不得成为负载放大器**——上游突发时把压力以受控速率传给下游，而不是原样放大。

### 9.4 Kafka 集群级配额（兜底）

应用级限流是"自律"，集群级配额是"强制兜底"。Kafka 支持 per client-id / user 的 `producer_byte_rate` / `consumer_byte_rate` 配额，broker 通过**延迟响应**（而非报错）限速，Producer/Consumer 自然退避。建议为每个服务 client-id 设置字节配额 🔴，保护共享集群不被任一服务打爆。与 §9.2/§9.3 的应用级限流互补：应用级管事件粒度与公平性，集群级管字节总量与多租户隔离。

### 9.5 限流与顺序保证的兼容

- **生产侧**：令牌桶/in-flight/公平调度都只改变"何时发""发多快""发哪个 topic"，**不改变分区内提交顺序**。领取按 `created_at`，发送按 `producer.send()` 调用顺序，幂等 Producer 保证分区内即使重试也不乱序。✅
- **消费侧**：同一分区单线程消费，令牌桶/Bulkhead/pause 只延迟处理，不跨分区乱序。✅

结论：限流是**速率维度**的控制，顺序是**分区维度**的保证，两者正交，可叠加。

### 9.6 MES 场景限流配置示例

> 以下数值均为**设计容量目标 + 假设**（假设单服务低频业务事件峰值数百/秒，突发来自批量操作），🔴 为需按真实集群容量与业务 SLA 拍板的阈值。

| 场景 | 限流要点 | 设计目标（🔴 可调） |
|---|---|---|
| 批量工单下发 / BOM 导入 | 单命令产生大量事件，入口即节流；Publisher 排空受全局令牌桶约束 | 入口批量化（每批 N 条入表）；Publisher 全局 ≤200 事件/s 🔴 |
| 过点高峰（换班） | 过点为低频但换班有峰；允许令牌桶 burst 吸收 | 过点 topic 配额 ≤100 事件/s，burst 2× 🔴 |
| 维修 SLA 事件 | 低 volume 高紧急，公平调度优先 | 维修 topic 配额 ≥20 事件/s，权重高于工单 🔴 |
| 集群抖动 | 自适应背压 MD 减批、AI 恢复 | 失败率 >5% 或 p99 >2s 触发缩减 🔴 |
| 采集数据 | **不走 Outbox**，隔离 | 边缘缓冲 + Kafka 直连，Outbox 不承担 |

### 9.7 限流可观测与告警

在 §11 指标基础上补充限流专属指标：

| 指标 | 含义 |
|---|---|
| `outbox_throttle_wait_total` / `_seconds` | 令牌桶等待次数 / 累计等待时长（节流强度）。 |
| `outbox_inflight` | 当前在途发送数（应 ≤ 在途上限）。 |
| `outbox_topic_pending{topic}` | 按 topic 的 PENDING 积压（公平调度健康度）。 |
| `outbox_claim_batch_size` | 自适应当前批大小。 |
| `consumer_throttle_wait_total` | 消费端令牌桶等待次数。 |
| `consumer_pause_total` | 慢消费者 pause 次数（背压触发频度）。 |

告警：令牌桶等待时长持续高位（说明限流是瓶颈，考虑扩容或调参）；某 topic `outbox_topic_pending` 持续增长（公平失效或下游消费不及）；`consumer_pause_total` 频繁（消费方持续追不上，需扩分区或优化处理）。

---

## 10. 事务边界汇总

**生产侧**：

```text
一个业务命令 = 一个本地数据库事务
事务内：加载聚合根 -> 执行业务方法 -> 保存业务状态 -> 插入 outbox_event
事务外：不保证立即发 Kafka（afterCommit 仅唤醒，可靠性靠轮询兜底）
```

禁止：`@Transactional` 方法内直接 `kafkaTemplate.send()`。

**消费侧**：

```text
Kafka poll -> 开本地 MySQL 事务 -> 幂等插入 consumed_event -> 业务变更 -> commit -> ack offset
```

---

## 11. 可观测性与告警

必须暴露以下指标：

| 指标 | 含义 |
|---|---|
| `outbox_pending_count` | PENDING + RETRYABLE 待发布数量。 |
| `outbox_publishing_count` | PUBLISHING 数量。 |
| `outbox_dead_letter_count` | DEAD_LETTER 数量。 |
| `outbox_publish_latency_seconds` | `published_at - created_at`。 |
| `outbox_publish_success_total` / `_failure_total` | 成功 / 失败发布总数。 |
| `outbox_retry_total` | 重试总数。 |
| `consumer_duplicate_event_total` | 消费方幂等去重次数。 |
| `consumer_dead_letter_total` | 消费方进 DLT 数量。 |

（限流专属指标见 §9.7。）

告警建议：

| 告警条件 | 严重性 |
|---|---|
| `DEAD_LETTER > 0` 持续 5 分钟 | 高 |
| 最老 PENDING 超过 1 分钟 | 中/高（按业务 SLA） |
| PENDING 数量持续增长 | 高 |
| PUBLISHING 锁超时数量增长 | 中 |
| Kafka send failure rate 超阈值 | 高 |
| 消费 DLT 有新增 | 高 |

---

## 12. 数据清理与归档

`outbox_event` 不建议无限增长：

```text
SENT      保留 3~7 天
DEAD_LETTER 长期保留直到人工处理
DISCARDED  保留 30 天或按审计要求
```

```sql
DELETE FROM outbox_event
WHERE status = 'SENT'
  AND published_at < CURRENT_TIMESTAMP(3) - INTERVAL 7 DAY
LIMIT 1000;
```

大表用按日期分区或归档表，避免一次性大批量删除影响线上。`consumed_event` 同理按 `processed_at` 滚动清理（幂等窗口外即可删，🔴 幂等保留期按最长重投窗口 + 安全余量定，建议 ≥ Kafka retention 或 ≥ 7 天）。

---

## 13. 重放与补偿

Outbox + Kafka retention 使事件可重放，支撑读模型重建与故障补偿。三种重放场景：

**① 生产侧死信重放**：`DEAD_LETTER` 经人工修复（修数据 / 修 schema / 修下游）后，重置为 `PENDING`、清 `next_retry_at`，Publisher 重新投递。重放会重复发送同一 `event_id`，消费端幂等吸收。

**② 消费端重建投影**：新消费者或读模型重建时，`auto-offset-reset=earliest` 从头消费。注意：若 `consumed_event` 已有该 `event_id` 记录，幂等会**跳过**已处理事件——这正好避免重复副作用，但也意味着**单纯重放不会重跑业务**。

**③ 强制重处理（reprocess）**：若需要让消费方对已处理事件**重跑**业务（如修了投影逻辑后重建），**不能**复用原 `consumer_group`（幂等会全部跳过）。两种做法 🔴：
- **新 consumer_group**：换组名从头消费，`consumed_event` 用新 `consumer_group`，逻辑上等同首次处理。简单，推荐。
- **reprocess 模式**：保留原组，引入"重处理标记 + 扩展幂等键"（如 `event_id + consumer_group + reprocess_epoch`），绕过旧幂等记录。复杂，仅在不能换组时用。

> 关键认知：**幂等是"不重复执行"的保证，重放想"重复执行"必须主动绕过幂等**。这是 Outbox 重放最易踩的坑。

---

## 14. 推荐包结构

```text
src/main/java/com/company/mes/common/outbox/
  domain/
    OutboxEvent.java
    OutboxStatus.java
  application/
    OutboxEventFactory.java
    OutboxPublisher.java
    RateLimitedOutboxPublisher.java     # 限流集成
    AdaptiveClaimController.java        # 自适应背压
    OutboxPublisherJob.java
    OutboxRecoveryJob.java
    OutboxCleanupJob.java
  infrastructure/
    MyBatisOutboxEventMapper.java
    KafkaOutboxMessageSender.java
    RateLimiterFactory.java             # 令牌桶/Resilience4j
    OutboxProperties.java
  api/
    EventEnvelope.java
    EventHeaderNames.java
```

业务域只依赖 outbox 抽象（`OutboxEventFactory`），**不直接依赖 Kafka 细节**。

---

## 15. 实现检查清单

**生产侧**
- [ ] 业务状态变更与 outbox 写入在同一 `@Transactional`。
- [ ] 聚合根不直接依赖 Kafka。
- [ ] 每条事件有全局唯一 `event_id`、`event_type`、`event_version`、`occurred_at`。
- [ ] `partition_key` 保证同聚合根有序。
- [ ] Publisher 多实例并发领取（`FOR UPDATE SKIP LOCKED`）。
- [ ] 领取与置 PUBLISHING 在同一短事务，Kafka 发送不在该事务内。
- [ ] `PUBLISHING` 锁超时可恢复。
- [ ] 失败重试 + 死信 + 指数退避。
- [ ] 全局令牌桶 + 按 topic 公平 + 在途上限 + 自适应背压已就位。
- [ ] `SENT` 有清理策略。

**消费侧**
- [ ] `enable-auto-commit=false` + 手动 ack。
- [ ] `event_id + consumer_group` 幂等，幂等记录与业务同事务。
- [ ] 失败重试 + DLT，DLT 有告警与人工处理。
- [ ] 消费端令牌桶 + Bulkhead + 慢消费者 pause 背压。
- [ ] 下游调用有 RateLimiter/CircuitBreaker。

**可观测**
- [ ] 监控 PENDING 积压（含按 topic）、发布延迟、DEAD_LETTER。
- [ ] 监控限流等待、在途、pause 频度。
- [ ] 监控 Kafka producer 错误率、消费 DLT。

---

## 16. 推荐实施顺序

1. 建 `outbox_event` / `consumed_event` 表与索引（§3）。
2. 建 Kafka topic 与 DLT（§7.1/§7.3/§8.4）。
3. 实现 `OutboxEventFactory`（统一 Envelope/payload/headers）。
4. 改造应用服务：业务变更同事务写 outbox（§5.1）。
5. 实现 `OutboxPublisher`：扫描/领取/发送/状态更新（§6）。
6. 加 `OutboxRecoveryJob`（恢复超时 PUBLISHING）与 `OutboxCleanupJob`（清理 SENT）。
7. **接限流层**：全局令牌桶 + 按 topic 公平 + 在途上限 + 自适应背压（§9.2）。
8. 消费端幂等表 + 手动 ack + 重试/DLT（§8）。
9. 消费端限流：令牌桶 + Bulkhead + pause 背压 + 下游 CircuitBreaker（§9.3）。
10. 集群级配额兜底（§9.4，需集群管理员协同）🔴。
11. 监控指标 + 告警（§11/§9.7）。
12. 选低频、影响可控的业务事件试点，验证稳定后推广。

---

## 17. 关键原则总结

1. **业务事务只写业务表 + outbox 表，不直接承担可靠 Kafka 投递。**
2. **outbox 表是可靠性来源，Publisher 是投递执行器 + 限流控制点。**
3. **Kafka 投递按至少一次，消费方必须 `event_id` 幂等。**
4. **同聚合根用相同 partition key，分区内有序；限流只节流速率不破坏顺序。**
5. **低频业务契约事件走 Outbox，高频原始采集数据不走 Outbox——这是架构级限流。**
6. **生产侧四层限流（领取节流/全局令牌桶/按 topic 公平/在途上限）+ 自适应背压；消费侧令牌桶 + Bulkhead + pause 背压 + 下游熔断。**
7. **幂等保证"不重复执行"；要"重复执行"必须主动绕过幂等（新 consumer_group）。**
8. **即使当前只有服务内部消费者，只要是业务契约事件，统一走 Kafka + Outbox。**

---

## 附录 A：决策点 🔴（交还用户）

| 决策点 | 说明 | 默认建议 |
|---|---|---|
| 全局/各 topic 发送配额（事件/s、burst、字节/s） | 取决于未实测的集群容量与业务 SLA | 全局 200/s、burst 2×，按 §9.6 topic 权重分配；上线前压测定 |
| 按 topic 公平领取是否默认启用 | 增加每 tick DB 查询数，换取热点不饿死 | 积压场景启用，常态可关 |
| Kafka 集群级配额（producer/consumer_byte_rate） | 需集群管理员协同，影响多租户 | 按 client-id 设字节配额作兜底 |
| outbox 大表治理：分区 vs 归档 | 取决于实际写入量 | 单表 + 归档；单表超千万行再上 RANGE 分区 |
| `consumed_event` 幂等保留期 | 需 ≥ 最长重投窗口 | ≥ Kafka retention 或 ≥ 7 天 |
| 重放策略：新 consumer_group vs reprocess 模式 | 取决于投影是否幂等可重入 | 新 consumer_group（简单） |
| 重试上限 / 退避参数 | 取决于业务 SLA | 20 次、5s 起、30m 封顶 |

> 以上阈值在真实集群容量与业务 SLA 明确前，均为**设计目标 + 假设**，不作线上实绩承诺（口径见 §0）。
