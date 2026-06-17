# 业务事件 MySQL 配置说明

> 适用范围：本文用于说明业务事件消息处理中的 MySQL 表结构、Outbox 事件表、消费幂等表、Publisher 领取 SQL、恢复 SQL 与清理策略。
>
> MySQL 服务器参数、Spring MyBatis、连接池、事务基础配置等项目级初始化内容，见《[../MySQL配置说明](../MySQL配置说明.md)》。

---

## 1. 适用场景

业务事件采用 Transactional Outbox 时，需要在业务数据库中保存待发布事件。

典型流程：

```text
业务事务内：
  - 修改业务表
  - 插入 outbox_event
事务提交后：
  - Outbox Publisher 扫描 outbox_event
  - 发布 Kafka
  - 更新 outbox_event 状态
```

本文只描述业务事件相关 MySQL 表和 SQL，不描述 MySQL 服务器初始化和 Spring MyBatis 基础配置。

---

## 2. Outbox 表

### 2.1 表名

建议每个服务拥有自己的 outbox 表：

```text
outbox_event
```

如果服务内有多个数据库或多个 bounded context，也可以按上下文拆分：

```text
asset_outbox_event
pm_outbox_event
repair_outbox_event
```

优先推荐服务内统一一张 `outbox_event`，通过 `source_context`、`topic`、`aggregate_type` 区分。

### 2.2 DDL

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

说明：

- `DATETIME(3)` 保留毫秒精度，便于计算发布延迟。
- `uk_outbox_event_id` 保证同一事件不会重复写入 outbox。
- `idx_outbox_publishable` 支撑 Publisher 按 `status + next_retry_at + created_at` 扫描待发布事件。
- `payload`、`headers` 如果 MyBatis 类型处理成本较高，可以改为 `TEXT` 保存 JSON 字符串。

---

## 3. 字段说明

| 字段 | 说明 |
|---|---|
| `id` | outbox 表主键，可与 `event_id` 相同，也可独立生成。 |
| `event_id` | 全局事件唯一 ID，消费方幂等去重的核心字段。 |
| `topic` | Kafka topic。 |
| `event_type` | 事件类型。 |
| `event_version` | 事件契约版本，用于 schema 演进。 |
| `aggregate_type` | 聚合根类型。 |
| `aggregate_id` | 聚合根 ID。 |
| `aggregate_version` | 聚合根版本号，可用于消费方判断顺序或并发冲突。 |
| `partition_key` | Kafka 分区键，通常使用聚合根 ID。 |
| `payload` | 事件正文。 |
| `headers` | Kafka headers 的业务扩展。 |
| `source_service` | 来源服务。 |
| `source_context` | 来源限界上下文。 |
| `trace_id` | 链路追踪 ID。 |
| `correlation_id` | 业务流程关联 ID，同一业务流程中的多个事件可共用。 |
| `causation_id` | 导致当前事件产生的上游事件或命令 ID。 |
| `status` | 消息状态。 |
| `retry_count` | 已重试次数。 |
| `max_retry_count` | 最大重试次数。 |
| `next_retry_at` | 下次可投递时间。 |
| `locked_by` | 被哪个 Publisher 实例锁定。 |
| `locked_at` | 锁定时间。 |
| `last_error_*` | 最近一次失败原因。 |
| `occurred_at` | 事件发生时间。 |
| `created_at` | outbox 行创建时间。 |
| `published_at` | 成功发布到 Kafka 的时间。 |
| `dead_lettered_at` | 进入死信状态的时间。 |

---

## 4. 多实例领取 SQL

MySQL 8.0 推荐在一个短事务中完成“查询并加锁 + 更新为 PUBLISHING”：

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

要求：

- 这两个 SQL 必须在同一个事务中执行。
- 事务只负责领取事件，不要包含 Kafka 发送。
- Kafka 发送完成后，再用单独 SQL 标记 `SENT`、`RETRYABLE` 或 `DEAD_LETTER`。

---

## 5. 成功与失败状态更新

### 5.1 标记 SENT

```sql
UPDATE outbox_event
SET status = 'SENT',
    published_at = CURRENT_TIMESTAMP(3),
    locked_by = NULL,
    locked_at = NULL,
    last_error_code = NULL,
    last_error_message = NULL
WHERE id = :id
  AND status = 'PUBLISHING';
```

### 5.2 标记 RETRYABLE

```sql
UPDATE outbox_event
SET status = 'RETRYABLE',
    retry_count = :retryCount,
    next_retry_at = :nextRetryAt,
    locked_by = NULL,
    locked_at = NULL,
    last_error_code = :lastErrorCode,
    last_error_message = :lastErrorMessage
WHERE id = :id
  AND status = 'PUBLISHING';
```

### 5.3 标记 DEAD_LETTER

```sql
UPDATE outbox_event
SET status = 'DEAD_LETTER',
    retry_count = :retryCount,
    locked_by = NULL,
    locked_at = NULL,
    last_error_code = :lastErrorCode,
    last_error_message = :lastErrorMessage,
    dead_lettered_at = CURRENT_TIMESTAMP(3)
WHERE id = :id
  AND status IN ('PUBLISHING', 'RETRYABLE');
```

---

## 6. Publisher 恢复 SQL

恢复长时间卡在 `PUBLISHING` 的消息：

```sql
UPDATE outbox_event
SET status = 'RETRYABLE',
    locked_by = NULL,
    locked_at = NULL,
    next_retry_at = CURRENT_TIMESTAMP(3),
    last_error_code = 'PUBLISHING_LOCK_TIMEOUT',
    last_error_message = 'Publisher lock timeout, reset to retryable'
WHERE status = 'PUBLISHING'
  AND locked_at < CURRENT_TIMESTAMP(3) - INTERVAL 5 MINUTE;
```

注意：如果 Kafka 已发送成功但还没来得及标记 `SENT` 就宕机，恢复后可能重复发送同一 `event_id`，消费端必须幂等。

---

## 7. 消费幂等表

消费 Kafka 业务事件的服务建议建立消费幂等表：

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

消费端必须利用主键做幂等：

```text
先 insert consumed_event
若主键冲突，说明该 consumer_group 已处理过该 event_id，直接 ack
若插入成功，在同一个本地事务中执行业务处理
```

幂等记录和业务处理必须在同一个本地事务中提交。

---

## 8. Outbox 配置属性

使用 Outbox Publisher 的服务可增加如下应用配置：

```yaml
app:
  outbox:
    publisher:
      enabled: true
      instance-id: ${HOSTNAME:${random.uuid}}
      fixed-delay: 1000
      batch-size: 100
      lock-timeout: 5m
      max-retry-count: 20
      initial-retry-delay: 5s
      max-retry-delay: 30m
      cleanup:
        enabled: true
        sent-retention: 7d
        batch-size: 1000
```

说明：

| 配置 | 建议 | 说明 |
|---|---:|---|
| `fixed-delay` | `1000` | 后台轮询间隔，单位毫秒。 |
| `batch-size` | `100` | 单轮领取数量，按吞吐和 DB 压力调整。 |
| `lock-timeout` | `5m` | `PUBLISHING` 超时恢复阈值。 |
| `sent-retention` | `7d` | `SENT` 数据保留时间。 |

---

## 9. 数据清理

Outbox 表不建议无限增长。

建议：

```text
SENT 保留 3~7 天
DEAD_LETTER 长期保留直到人工处理
DISCARDED 保留 30 天或按审计要求
```

清理 SQL 示例：

```sql
DELETE FROM outbox_event
WHERE status = 'SENT'
  AND published_at < CURRENT_TIMESTAMP(3) - INTERVAL 7 DAY
LIMIT 1000;
```

大表建议使用按日期分区或归档表，避免一次性大批量删除影响线上业务。

---

## 10. 检查项

- [ ] `outbox_event` 使用 InnoDB。
- [ ] `event_id` 有唯一约束。
- [ ] `idx_outbox_publishable` 已创建。
- [ ] Publisher 领取 SQL 使用 `FOR UPDATE SKIP LOCKED`。
- [ ] 领取 SQL 和置 `PUBLISHING` SQL 在同一个短事务中。
- [ ] Kafka 发送不在数据库领取事务内。
- [ ] `PUBLISHING` 超时可恢复。
- [ ] 消费端有 `consumed_event` 幂等表。
- [ ] 消费幂等记录和业务处理在同一个本地事务中。
- [ ] `SENT` 数据有清理或归档策略。
