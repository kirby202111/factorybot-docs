# 业务事件 Kafka 配置说明

> 适用范围：本文用于说明业务事件消息处理中的 Kafka Topic 命名、分区键、Producer/Consumer 可靠性参数、事件 Header、DLT、可靠性边界与检查项。
>
> Kafka broker、Spring Kafka 基础连接、默认 Producer/Consumer、错误处理器等项目级初始化配置，见《[../Kafka配置说明](../Kafka配置说明.md)》。

---

## 1. 业务事件 Topic 命名

建议统一：

```text
<domain>.<aggregate>.<dimension>
```

示例：

```text
eam.asset.lifecycle
eam.asset.availability
eam.asset.specification
eam.asset.metric
eam.asset.location
eam.asset.scrap
```

命名建议：

- Topic 表达业务语义，不表达技术来源。
- 不建议用 `service-name.event` 作为 topic 名。
- 同一聚合根同一类事实优先进入同一个 topic。
- 高频设备原始数据、遥测数据、业务领域事件应使用不同 topic 命名空间和保留策略，不要混在同一个 topic 中。

---

## 2. 分区键设计

原则：同一聚合根的事件必须有序。

建议：

| 场景 | partition key |
|---|---|
| 聚合根生命周期事件 | 聚合根 ID |
| 聚合根状态变更事件 | 聚合根 ID |
| 流程单据事件 | 单据 ID |
| 资产/设备类事件 | 资产 ID 或设备 ID |
| 批次类事件 | 批次 ID |

示例：

| Topic | partition key |
|---|---|
| `eam.asset.lifecycle` | `asset_id` |
| `eam.asset.availability` | `asset_id` |
| `eam.asset.specification` | `asset_id` 或 `specification_profile_id` |
| `eam.asset.metric` | `asset_id` |
| `eam.asset.location` | `asset_id` |
| `eam.asset.scrap` | `asset_id` 或 `scrap_application_id`；若关心资产状态联动，优先 `asset_id` |

要求：

- ProducerRecord 的 key 使用 `partition_key`。
- 同一聚合根生命周期事件必须使用同一个 key。
- 不要使用随机 UUID 作为 Kafka key，否则同一聚合根事件会分散到不同分区，顺序无法保证。

---

## 3. Topic 创建参数

示例：

```bash
kafka-topics.sh --create \
  --topic eam.asset.lifecycle \
  --bootstrap-server kafka-1:9092,kafka-2:9092,kafka-3:9092 \
  --partitions 12 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete
```

建议配置：

| 参数 | 建议 | 说明 |
|---|---:|---|
| `partitions` | 6、12、24 起步 | 根据服务吞吐和消费并行度调整。分区数后续可增加，但会影响 key 的分布位置。 |
| `replication.factor` | 3 | 生产环境建议 3 副本。 |
| `min.insync.replicas` | 2 | 配合 Producer `acks=all` 使用。 |
| `retention.ms` | 7~30 天 | 业务事件建议保留足够长，支持故障恢复和新消费者追赶。 |
| `cleanup.policy` | `delete` | 业务事件通常保留日志，不建议用 compact 替代完整事件流。 |

注意：

- 生产环境不建议依赖 topic 自动创建。
- 如果某些主题需要长期可重放，不要只依赖 Kafka retention，应将事件归档到对象存储、数据湖或事件审计库。

---

## 4. Producer 配置

业务事件 Producer 推荐使用项目级 Producer 默认配置，并重点保证：

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

关键点：

| 配置 | 推荐值 | 说明 |
|---|---:|---|
| `acks` | `all` | 等待 leader 和 ISR 确认，避免只写 leader 后丢失。 |
| `enable.idempotence` | `true` | 开启 Kafka Producer 幂等，降低 Producer 内部重试导致的重复写入。 |
| `retries` | `10` 或更高 | Kafka 客户端内部重试次数；如果上层使用 Outbox，还会做业务层重试。 |
| `delivery.timeout.ms` | `120000` | Producer 完成发送的总超时时间。 |
| `request.timeout.ms` | `30000` | 单次请求超时。 |
| `retry.backoff.ms` | `1000` | Kafka 客户端内部重试间隔。 |
| `linger.ms` | `5~20` | 等待更多消息组成批次，提高吞吐。低延迟要求可设小。 |
| `batch.size` | `32768` 或更高 | 批量大小。 |
| `compression.type` | `zstd` / `lz4` | 推荐压缩，减少网络和 broker 存储压力。 |
| `max.in.flight.requests.per.connection` | `5` | 开启幂等时 Kafka 推荐不超过 5。 |

---

## 5. 是否需要 Kafka 事务 Producer

业务事件采用 Transactional Outbox 时，通常不需要 Kafka 事务 Producer。

原因：

- 业务状态与 outbox 事件记录的一致性由 MySQL 本地事务保证。
- Kafka 发布失败可由 outbox 重试保证。
- 消费方通过 `event_id` 幂等处理重复消息。

Kafka 事务 Producer 更适合解决“Kafka 内部多 topic / 多 partition 的原子写入”，不能直接解决“MySQL 事务 + Kafka 发送”的原子性问题。

因此业务事件优先使用：

```text
普通 Producer + 幂等 Producer + Transactional Outbox
```

---

## 6. Consumer 配置

业务事件 Consumer 推荐关闭自动提交 offset，使用手动 ack：

```yaml
spring:
  kafka:
    consumer:
      enable-auto-commit: false
      auto-offset-reset: earliest
      properties:
        isolation.level: read_committed
        max.poll.records: 100
        session.timeout.ms: 45000
        heartbeat.interval.ms: 15000
    listener:
      ack-mode: manual
      concurrency: 3
```

关键点：

| 配置 | 推荐 | 说明 |
|---|---:|---|
| `enable-auto-commit` | `false` | 禁止自动提交 offset，避免业务处理失败但 offset 已提交。 |
| `ack-mode` | `manual` | 业务事务成功后手动 ack。 |
| `auto-offset-reset` | `earliest` | 新消费组从最早开始消费，适合领域事件补投影。 |
| `max.poll.records` | `100` | 控制单批处理量，避免单次拉取过多导致处理超时。 |
| `concurrency` | 与分区数匹配 | 并发数不应超过分区数太多。 |
| `isolation.level` | `read_committed` | 如果上游使用 Kafka 事务则只读已提交消息；本方案不强依赖，但可保留。 |

---

## 7. Listener 与 ack 顺序

示例：

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

处理顺序必须是：

```text
Kafka poll 收到消息
  → 开启本地 MySQL 事务
    → 插入 consumed_event 幂等记录
    → 执行业务处理
  → MySQL commit
  → ack Kafka offset
```

如果 MySQL commit 前失败，不 ack，Kafka 后续重投。

如果 MySQL commit 成功但 ack 前应用崩溃，Kafka 会重复投递，消费方通过 `event_id` 幂等表跳过。

---

## 8. 消费失败与 DLT

推荐使用 Spring Kafka 的 `DefaultErrorHandler`：

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

DLT topic 命名：

```text
<source-topic>.DLT
```

示例：

```text
eam.asset.lifecycle.DLT
eam.asset.availability.DLT
```

建议：

- DLT 必须有告警。
- DLT 消息要保留原始 payload、headers、异常摘要。
- DLT 后需要人工处理或补偿流程，不应静默丢弃。

---

## 9. 事件 Header 建议

Producer 发布业务事件时建议写入以下 header：

| Header | 说明 |
|---|---|
| `event_id` | 全局事件唯一 ID。 |
| `event_type` | 事件类型。 |
| `event_version` | 事件版本。 |
| `source_service` | 来源服务。 |
| `source_context` | 来源限界上下文。 |
| `trace_id` | 链路追踪 ID。 |
| `correlation_id` | 业务流程关联 ID。 |
| `causation_id` | 导致当前事件产生的上游事件或命令 ID。 |

payload 中也应保留这些 envelope 字段，header 主要用于路由、过滤、追踪和排障。

---

## 10. 可靠性边界

业务事件 Kafka 投递默认按以下语义设计：

```text
至少一次投递，业务侧通过幂等实现等效一次处理
```

Kafka Producer 幂等只能降低 Producer 内部重试造成的重复写入，不能消除 Outbox 故障恢复导致的重复事件。

典型重复窗口：

```text
Publisher 发送 Kafka 成功
应用在标记 outbox_event 为 SENT 前宕机
PUBLISHING 超时恢复为 RETRYABLE
Publisher 再次发送同一 event_id
```

所以所有业务事件消费者必须使用 `event_id + consumer_group` 做幂等。

---

## 11. 业务事件 Topic 与版本演进

事件 schema 演进必须遵守：

- 新增字段必须可选或有默认值。
- 不删除已有字段。
- 不改变已有字段语义。
- 不改变已有字段类型。
- 重大不兼容变更使用新的 `event_version` 或新的 `event_type`。

每条事件必须带：

```text
event_type
event_version
```

消费方根据类型和版本分发。

---

## 12. 检查项

- [ ] Producer `acks=all`。
- [ ] Producer `enable.idempotence=true`。
- [ ] Producer `max.in.flight.requests.per.connection <= 5`。
- [ ] Topic 副本数生产环境为 3。
- [ ] Topic `min.insync.replicas=2`。
- [ ] Topic 分区数满足消费并发需求。
- [ ] 禁止生产环境依赖 topic 自动创建。
- [ ] 业务事件使用稳定 partition key。
- [ ] Consumer `enable-auto-commit=false`。
- [ ] Listener 使用手动 ack。
- [ ] 业务事务提交成功后再 ack。
- [ ] 消费失败有重试和 DLT。
- [ ] DLT 有告警和人工处理流程。
