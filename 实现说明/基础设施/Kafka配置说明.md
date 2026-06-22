# Kafka 配置说明

> 适用范围：本文是项目初始化后的 Kafka 与 Spring Kafka 统一配置说明，仅描述项目级基础设施配置，不包含具体业务 Topic、业务事件 Header、Outbox、消费幂等、DLT 业务处理等实现细节。
>
> 业务事件相关的 Topic 命名、分区键、Producer/Consumer 可靠性参数、事件 Header、DLT 与 Outbox 可靠性边界，见《[业务事件/Kafka配置说明](业务事件/Kafka配置说明.md)》。

---

## 1. Kafka 集群基础要求

生产环境建议：

```text
Kafka 3.x+
至少 3 个 broker
Topic 默认副本数 3
min.insync.replicas = 2
禁用生产环境自动创建 topic
```

说明：

- 3 broker + 3 副本可以容忍单 broker 故障。
- `min.insync.replicas=2` 需要配合 Producer `acks=all` 才能提升写入可靠性。
- 生产环境不建议依赖自动创建 topic，避免拼写错误或默认参数不符合预期。

---

## 2. Kafka Broker 配置

以下参数作为项目初始化基线，实际值需要结合部署方式、磁盘、网络、消息量和保留周期调整。

```properties
# Broker 基础
broker.id=1
listeners=PLAINTEXT://0.0.0.0:9092
advertised.listeners=PLAINTEXT://kafka-1:9092
num.network.threads=3
num.io.threads=8
queued.max.requests=500

# 日志目录
log.dirs=/data/kafka-logs
num.partitions=6
default.replication.factor=3
min.insync.replicas=2

# Topic 管理
auto.create.topics.enable=false
delete.topic.enable=true

# 日志保留
log.retention.hours=168
log.segment.bytes=1073741824
log.retention.check.interval.ms=300000

# 消息大小
message.max.bytes=1048576
replica.fetch.max.bytes=1048576

# 复制
num.replica.fetchers=2
replica.lag.time.max.ms=30000

# 压缩
compression.type=producer
```

关键参数：

| 参数 | 建议 | 说明 |
|---|---:|---|
| `default.replication.factor` | `3` | 生产环境 topic 默认 3 副本。 |
| `min.insync.replicas` | `2` | 至少 2 个 ISR 确认，配合 `acks=all`。 |
| `auto.create.topics.enable` | `false` | 禁止自动创建 topic。 |
| `num.partitions` | `6` 起步 | 默认分区数，具体 topic 仍应显式创建。 |
| `log.retention.hours` | `168` | 默认保留 7 天，可按 topic 覆盖。 |
| `message.max.bytes` | `1048576` | 默认单消息 1MB，业务大消息不建议直接进 Kafka。 |
| `compression.type` | `producer` | 使用 Producer 指定的压缩类型。 |

---

## 3. Topic 默认配置

生产环境 topic 应显式创建，示例：

```bash
kafka-topics.sh --create \
  --topic example.topic \
  --bootstrap-server kafka-1:9092,kafka-2:9092,kafka-3:9092 \
  --partitions 6 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=604800000 \
  --config cleanup.policy=delete
```

通用建议：

| 参数 | 建议 | 说明 |
|---|---:|---|
| `partitions` | `6`、`12`、`24` 起步 | 按吞吐和消费并行度评估。 |
| `replication.factor` | `3` | 生产环境建议 3 副本。 |
| `min.insync.replicas` | `2` | 配合 Producer `acks=all`。 |
| `retention.ms` | 7~30 天 | 默认保留周期，具体业务 topic 可覆盖。 |
| `cleanup.policy` | `delete` | 默认按时间删除；需要 compact 的 topic 单独评估。 |

注意：

- 分区数后续可以增加，但会影响 key 到 partition 的映射分布。
- 不同消息类型应按吞吐、保留周期、消费模型拆分 topic。
- 大消息应放对象存储或文件服务，Kafka 消息只传引用和元数据。

---

## 4. Spring Kafka 基础配置

### 4.1 Maven 依赖

```xml
<dependency>
    <groupId>org.springframework.kafka</groupId>
    <artifactId>spring-kafka</artifactId>
</dependency>
```

Spring Boot 项目通常由 starter 或依赖管理自动控制版本，应与 Spring Boot 版本匹配。

### 4.2 基础连接配置

```yaml
spring:
  kafka:
    bootstrap-servers: ${KAFKA_BOOTSTRAP_SERVERS:localhost:9092}
    client-id: ${spring.application.name}
```

环境变量建议：

```text
KAFKA_BOOTSTRAP_SERVERS
```

生产环境建议至少配置 3 个 broker 地址：

```text
kafka-1:9092,kafka-2:9092,kafka-3:9092
```

---

## 5. Spring Kafka Producer 默认配置

```yaml
spring:
  kafka:
    producer:
      key-serializer: org.apache.kafka.common.serialization.StringSerializer
      value-serializer: org.apache.kafka.common.serialization.StringSerializer
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

关键配置：

| 配置 | 推荐值 | 说明 |
|---|---:|---|
| `acks` | `all` | 等待 leader 和 ISR 确认。 |
| `enable.idempotence` | `true` | 开启 Producer 幂等，降低客户端重试导致的重复写入。 |
| `retries` | `10` 或更高 | Kafka 客户端内部重试次数。 |
| `delivery.timeout.ms` | `120000` | Producer 完成发送的总超时时间。 |
| `request.timeout.ms` | `30000` | 单次请求超时。 |
| `retry.backoff.ms` | `1000` | Kafka 客户端内部重试间隔。 |
| `linger.ms` | `5~20` | 等待更多消息组成批次，提高吞吐。 |
| `batch.size` | `32768` 或更高 | 批量大小。 |
| `compression.type` | `zstd` / `lz4` | 推荐压缩，减少网络和 broker 存储压力。 |
| `max.in.flight.requests.per.connection` | `5` | 开启幂等时 Kafka 推荐不超过 5。 |

---

## 6. Spring Kafka Consumer 默认配置

```yaml
spring:
  kafka:
    consumer:
      key-deserializer: org.apache.kafka.common.serialization.StringDeserializer
      value-deserializer: org.apache.kafka.common.serialization.StringDeserializer
      enable-auto-commit: false
      auto-offset-reset: latest
      properties:
        max.poll.records: 100
        session.timeout.ms: 45000
        heartbeat.interval.ms: 15000
    listener:
      ack-mode: manual
      concurrency: 3
```

关键配置：

| 配置 | 推荐 | 说明 |
|---|---:|---|
| `enable-auto-commit` | `false` | 由业务处理成功后显式提交 offset。 |
| `ack-mode` | `manual` | 手动 ack，便于控制提交时机。 |
| `auto-offset-reset` | `latest` | 通用默认值；需要补投影或从头追赶的消费者单独设为 `earliest`。 |
| `max.poll.records` | `100` | 控制单批处理量，避免单次拉取过多导致处理超时。 |
| `concurrency` | 与分区数匹配 | 不应明显超过 topic 分区数。 |
| `session.timeout.ms` | `45000` | 消费者会话超时。 |
| `heartbeat.interval.ms` | `15000` | 心跳间隔，通常小于 session timeout 的 1/3。 |

---

## 7. Spring Kafka Listener 基础写法

```java
@KafkaListener(
    topics = "example.topic",
    groupId = "${spring.application.name}.example-consumer"
)
public void onMessage(
    ConsumerRecord<String, String> record,
    Acknowledgment acknowledgment
) {
    try {
        // 处理消息
        acknowledgment.acknowledge();
    } catch (Exception ex) {
        throw ex;
    }
}
```

要求：

- 不开启自动提交 offset。
- 成功处理后再 ack。
- 失败时抛出异常，让 Spring Kafka 错误处理器处理重试或转入 DLT。
- Listener 内部不要吞掉异常后直接 ack。

---

## 8. 错误处理器基础配置

通用错误处理器示例：

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
        IllegalArgumentException.class
    );

    return errorHandler;
}
```

说明：

- 默认重试 5 次，每次间隔 5 秒。
- 不可恢复异常直接进入 DLT。
- DLT topic 应显式创建，不依赖自动创建。
- 具体哪些异常可重试、哪些不可重试，由业务场景决定。

---

## 9. 序列化约定

项目初始化默认使用：

```text
key: String
value: String(JSON)
```

即：

```yaml
key-serializer: org.apache.kafka.common.serialization.StringSerializer
value-serializer: org.apache.kafka.common.serialization.StringSerializer
key-deserializer: org.apache.kafka.common.serialization.StringDeserializer
value-deserializer: org.apache.kafka.common.serialization.StringDeserializer
```

建议：

- 消息 value 使用 JSON 字符串，便于调试和跨语言消费。
- 大消息不要直接写入 Kafka。
- 如果后续使用 Avro / Protobuf / Schema Registry，应另行制定项目级 schema 管理规范。

---

## 10. 监控与运维基线

需要关注：

| 指标 | 说明 |
|---|---|
| broker 存活状态 | broker 是否在线。 |
| under replicated partitions | 副本不足分区数量。 |
| offline partitions | 离线分区数量。 |
| consumer lag | 消费积压。 |
| producer send error rate | 生产发送错误率。 |
| request latency | broker 请求延迟。 |
| disk usage | Kafka 日志磁盘使用率。 |
| DLT 新增消息数 | 消费失败进入死信数量。 |

告警建议：

- broker 下线。
- offline partition > 0。
- under replicated partition 持续存在。
- consumer lag 持续增长。
- DLT 有新增。
- 磁盘使用率超过阈值。

---

## 11. 初始化检查清单

### 11.1 Kafka 集群

- [ ] 生产环境至少 3 个 broker。
- [ ] `default.replication.factor=3`。
- [ ] `min.insync.replicas=2`。
- [ ] `auto.create.topics.enable=false`。
- [ ] 已规划 topic 默认保留周期。
- [ ] 已规划 Kafka 日志磁盘容量。
- [ ] 已建立 broker、partition、consumer lag 监控。

### 11.2 Spring Kafka

- [ ] 已配置 `spring-kafka` 依赖。
- [ ] 已配置 `KAFKA_BOOTSTRAP_SERVERS`。
- [ ] Producer `acks=all`。
- [ ] Producer `enable.idempotence=true`。
- [ ] Producer `max.in.flight.requests.per.connection <= 5`。
- [ ] Consumer `enable-auto-commit=false`。
- [ ] Listener 使用手动 ack。
- [ ] 已配置错误处理器。
- [ ] DLT topic 不依赖自动创建。
