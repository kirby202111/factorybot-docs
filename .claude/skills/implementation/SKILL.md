---
name: implementation
description: 在本仓库（factorybot-docs，PCBA + 整机组装混合型车间 MES）下，以某个限界上下文已落地的"领域建模"文档为依据，按统一约定撰写或修订该限界上下文的"代码实现"文档，输出端口清单、主动/被动适配器、领域层与应用层落地、数据模型、消息主题配置。当用户要求"做 / 写 / 补 / 改 XX 限界上下文的代码实现 / 实现说明 / 把 XX 限界上下文从领域模型落到代码 / 写 XX 上下文的端口与适配器"等任务时启用。
---

# 代码实现写作 Skill

本 skill 规定本仓库**代码实现（Implementation）类文档**的统一骨架、与领域建模的对齐规则和质量门槛，把领域建模里的端口声明落实为可指导编码的**主动/被动适配器 + 技术选型 + 数据模型 + 消息配置**，并与横向基础设施文档（Kafka/MySQL/消息处理）互联。

> **前置纪律（来自项目 CLAUDE.md）**：当用户指定要做某个模块时，**只读该模块自身文档与其直接对接的领域（domain），不把其它模块杂糅进来**。代码实现只承载本限界上下文的端口与适配器，不代写相邻限界上下文的实现。

> **术语约定（全文强制，与 [event-storming](../event-storming/SKILL.md)、[domain-modeling](../domain-modeling/SKILL.md) 一致）**：
> - **限界上下文（Bounded Context）**：代码实现的落地单元。为行文简洁，下文以「**本上下文**」指代当前限界上下文，以「**相邻上下文**」指代其它限界上下文，以「**上下文外**」指代当前限界上下文之外。正文中具体限界上下文统一写作「XX限界上下文」，**文件名**按仓库既有约定写作 `XX上下文.md`。
> - **端口（Port）**：领域层/应用层声明的接口，描述"我需要什么能力"，与技术无关。端口在领域建模文档已隐式声明（应用服务入口、事件消费落点、ACL 翻译点、Repository 抽象），本 skill 把它们显式化。
> - **适配器（Adapter）**：端口的某一技术实现。**主动适配器**（驱动侧）调用应用层，如 REST Controller / Kafka Listener / 定时任务；**被动适配器**（被驱动侧）被应用层调用，如 Repository 实现 / ACL 实现 / 事件发布器实现。

---

## 1. 何时使用本 skill

- 用户说"做 / 写 / 补 / 改 XX 限界上下文的代码实现 / 实现说明 / 端口与适配器"。
- 用户给出一个限界上下文名称，需要把已落地的领域建模**翻译**为可编码的技术结构。
- 用户要把聚合根 / 应用服务 / 领域服务落实为 Spring Boot 工程的包结构、类、表、topic。

**强前置**：本 skill **必须以已存在的领域建模文档为依据**。若该限界上下文的领域建模尚不存在，**先停止**，提示用户先走 [domain-modeling](../domain-modeling/SKILL.md) skill 落地领域建模，再回来做代码实现——领域建模是源，代码实现是派生。跳过领域建模直接写实现，会让端口/适配器失去业务语义根基，也违背 CLAUDE.md 的 OOD/SOLID 硬约束。

不适用：业务流程梳理（走 event-storming）、领域结构设计（走 domain-modeling）。本 skill 只管"怎么接、怎么存、怎么发"。

---

## 2. 文档落位规则

代码实现文档统一落到 [实现说明/](../../../实现说明/) 下，**按服务镜像领域建模目录**，便于双向查找：

```
领域模型/<服务>/事件风暴/<限界上下文>.md      ← 业务流程（源）
领域模型/<服务>/领域建模/<限界上下文>.md      ← 领域结构（依据）
实现说明/<服务>/<限界上下文>.md               ← 本 skill 产物
```

完整目录结构（与 [领域总览](../../../领域模型/领域总览.md) 一致，三个服务）：

```
实现说明/
├── 制造资源服务/<限界上下文>.md
├── 生产执行服务/<限界上下文>.md
├── 设备管理服务/<限界上下文>.md
└── 业务事件/                                  ← 横向共享，已存在
    ├── Kafka配置说明.md
    ├── MySQL配置说明.md
    └── 消息处理实现说明.md
```

> **与横向文档的分工**：`实现说明/业务事件/` 下的 Kafka/MySQL/消息处理文档是**跨上下文共享的基础设施约定**（topic 命名规则、outbox 表 DDL、producer/consumer 参数、幂等表），本 skill 产物**不重复**这些内容，只引用。本 skill 产物写的是"本上下文具体用了哪些 topic、哪些表、哪些适配器类"。

**新建文件前必做三件事**：
1. `ls` 对应服务的 `实现说明/<服务>/` 目录确认有没有同名旧文件（目录不存在则创建）。**若旧文件存在但归属变了，先与用户确认是改写还是新建**。
2. `ls` 对应服务的 `领域模型/<服务>/领域建模/` 目录确认领域建模源文件存在——不存在则按 §1 强前置处理。
3. 文档头部"实现依据"链接必须指向该领域建模源文件（相对路径 `../../领域模型/<服务>/领域建模/<XX>上下文.md`）；同时链接到横向基础设施文档（`../业务事件/Kafka配置说明.md` 等）。

> **路径提示**：本 skill 文件位于 `.claude/skills/implementation/`，指向 `实现说明/` 的相对路径需写作 `../../../实现说明/`（三层 `../` 回到仓库根）。

---

## 3. 从领域建模到代码实现的映射规则（核心）

代码实现不是凭空设计，而是把领域建模里已声明的端口**逐一对位**到技术结构。下表是**强制对齐表**，落地时逐行落实：

| 领域建模元素（源） | 代码实现对应（产物） | 落实要点 |
|--------------------|--------------------|----------|
| 聚合根 / 实体 / 值对象（§1、§2） | 领域层类（§3） | 纯 POJO，表映射用原生 MyBatis XML Mapper / `@Select` 注解 SQL（写在 `infrastructure/persistence`）；禁止基础设施行为耦合；值对象不可变；状态机用枚举 + 方法守卫 |
| 领域事件（§5） | 领域事件类 + Envelope（§3） | 事件名与领域建模逐字一致；Envelope 字段对齐 [消息处理实现说明](../../../实现说明/业务事件/消息处理实现说明.md) |
| 命令应用服务（§4） | 应用层服务 + **主动适配器**（§4、§5） | 每个 `XxxCommandService` 方法 → 一个 REST Controller 端点或 CLI 入口；事务边界在应用层 |
| 事件消费应用服务（§4） | **主动适配器** Kafka Listener + 应用层 handler（§5） | 每个订阅主题 → 一个 `@KafkaListener`；幂等走 [MySQL配置说明](../../../实现说明/业务事件/MySQL配置说明.md) 的 `consumed_event` |
| 查询应用服务（§4 读模型） | 查询服务 + 读模型表/视图（§6） | 读模型表与写模型分离；CQRS 读侧 |
| 领域服务（§3） | 领域层服务类（§3） | 跨聚合编排；**不依赖** Controller / Repository 实现 |
| 跨聚合不变式"由谁保证"（§6） | 落实到具体技术手段（§7） | 唯一索引→DDL；Saga→编排器；事件去重表→`consumed_event`；领域服务→服务类 |
| 对外契约发布主题（§7） | Kafka topic + Outbox（§8） | topic 名、`<svc>` 前缀、partition_key 照搬领域建模；发件箱走 [消息处理实现说明](../../../实现说明/业务事件/消息处理实现说明.md) |
| Repository 隐式依赖（聚合加载/保存） | **被动适配器** Repository 接口 + 实现（§5） | 接口在领域层，实现在 infrastructure 层（依赖倒置） |
| ACL 翻译上下文外模型（§3、§4） | **被动适配器** ACL 实现（§5） | 调相邻上下文 REST / 订阅事件，翻译为本上下文值对象；防腐层隔离外部模型 |
| 暂缓模块说明（领域建模） | 暂缓实现说明（§11） | 暂缓项照搬，指明通过哪个端口预留 |

> **可追溯性硬约束**：代码实现文档 §10"与领域建模的映射"必须给出一张**领域建模元素→实现元素**的对位表，让读者能从任何一个聚合根/应用服务/事件查到它落到了哪个类/表/topic。这是本 skill 的验收门槛之一。

> **反向一致性**：若实现过程中发现领域建模有遗漏（如某应用服务缺事务边界说明、某事件缺 partition_key、某 ACL 缺翻译规则），**先回去补领域建模**，再回来改实现——不要在实现文档里私自补业务语义，那会让两份文档失同步。

---

## 4. 架构分层与端口-适配器（强制）

本上下文工程遵循**六边形架构（端口与适配器）**，分层与职责如下。**每个适配器必须能对应到一个领域建模声明的端口，禁止出现没有业务语义的"技术适配器"**。

```text
┌──────────────────────────────────────────────────────────────┐
│                      主动适配器（驱动侧）                       │
│   REST Controller · Kafka Listener · 定时任务 · CLI           │
│   职责：接收外部输入 → 转换为命令/事件 → 调用应用服务           │
└──────────────────────────┬───────────────────────────────────┘
                           │ 调用
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                          应用层                                │
│   CommandService · EventConsumer · QueryService               │
│   职责：事务边界 · 编排领域服务/聚合根 · 写 outbox · 读读模型    │
└──────────────────────────┬───────────────────────────────────┘
                           │ 调用
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                          领域层                                │
│   聚合根 · 实体 · 值对象 · 领域服务 · 领域事件                  │
│   职责：业务规则 · 不变式 · 不耦合基础设施行为                  │
└──────────────────────────┬───────────────────────────────────┘
                           │ 依赖接口（端口）
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                     被动适配器（被驱动侧）                      │
│   Repository 实现 · ACL 实现 · EventPublisher 实现             │
│   职责：实现领域层声明的端口 · 技术选型在此层切换                │
└──────────────────────────────────────────────────────────────┘
```

**依赖方向铁律**（CLAUDE.md 低耦合/高内聚 + SOLID 的 DIP）：

- 领域层**不耦合基础设施行为**。聚合根/实体/领域服务方法内**禁止**直接调用 `KafkaTemplate` / `RestTemplate` / 数据访问层（`Mapper`/`SqlSession`），**禁止** `@Transactional`（事务归应用层）。表映射用原生 MyBatis（XML Mapper 或 Mapper 接口上的 `@Select`/`@Insert` 注解 SQL），写在 `infrastructure/persistence`，领域对象保持纯 POJO；领域服务允许 `@Component` + 构造器注入端口接口。判断标准：注解是装配提示则放行，引入运行时外部副作用则禁止。
- 应用层只依赖领域层 + 端口接口；`@Transactional` 只出现在应用层。
- 主动适配器依赖应用层；被动适配器实现领域层端口（依赖倒置）。
- 换掉 HTTP→gRPC、MySQL→Postgres、Kafka→RabbitMQ，**只改适配器层，领域层与应用层一行不动**——这是验收分层是否正确的口诀。

---

## 5. 标准章节骨架（按顺序）

> **编号说明**：以下 `## 0` ~ `## 11` 是**目标文档**的章节结构，不是本 skill 自身的章节编号（本 skill 自身编号见各 `## 1.`~`## 11.` 标题）。

````markdown
# <XX>上下文 — 代码实现（<English Implementation Name>）

> **限界上下文**：<XX>
> **实现依据**：[领域建模 — <XX>上下文](../../领域模型/<服务>/领域建模/<XX>上下文.md)
> **技术栈**：Spring Boot · Spring Kafka · MyBatis · MySQL · Java
> **共享基础设施**：[消息处理](../业务事件/消息处理实现说明.md) · [Kafka配置](../业务事件/Kafka配置说明.md) · [MySQL配置](../业务事件/MySQL配置说明.md)

---

## 0. 实现总览

（一段话讲清本上下文工程结构与技术边界，对应领域建模 §0 的职责结论。）
（一张分层 ASCII 图，标出本上下文的主动/被动适配器清单——具体类名。）

---

## 1. 包结构

```text
src/main/java/com/company/mes/<svc>/
  interfaces/                  # 接口层（对外入口，原主动适配器）
    controller/                #   REST Controller（@RestController）
    listener/                  #   Kafka Listener（@KafkaListener）
    scheduler/                 #   定时任务（@Scheduled）
  application/                 # 应用层（事务边界、编排，@Transactional）
    service/                   #   应用服务（命令 / 查询 / 事件消费统一放此）
    port/                      #   出站端口接口（Repository 以外的 ACL、Publisher）
  domain/                      # 领域层（允许映射/DI 注解；禁止基础设施行为耦合，见 §6 第 1 条）
    model/                     #   聚合根、实体、值对象
    event/                     #   领域事件
    service/                   #   领域服务
    repository/                #   Repository 端口接口
  infrastructure/              # 基础设施层（被动适配器实现 + 配置）
    persistence/               #   Repository 实现 + MyBatis Mapper
    acl/                       #   ACL 实现（调相邻上下文 / 设备接口）
    messaging/                 #   EventPublisher 实现 + Outbox 接入
    config/                    #   @Configuration、Bean 装配
```

> outbox 是所有服务共用的逻辑，抽成公共模块，建在 `com.company.mes.common.outbox` 下，不再在每个业务服务里各写一遍。它本身也按 `domain` / `application` / `infrastructure` / `api` 分层：
>
> - `domain` — `OutboxEvent`、`OutboxStatus`
> - `application` — `OutboxEventFactory`、`OutboxPublisher`、`OutboxPublisherJob`、`OutboxRecoveryJob`
> - `infrastructure` — `MyBatisOutboxEventMapper`、`KafkaOutboxMessageSender`、`OutboxProperties`
> - `api` — `EventEnvelope`、`EventHeaderNames`
>
> 业务服务的应用服务只调 `OutboxEventFactory` 生成 `outbox_event` 记录，不直接碰 Kafka；真正发 Kafka 的细节被关在公共模块的 `infrastructure` 里。

---

## 2. 端口清单

从领域建模推导出的所有端口，分两类列：

### 2.1 入站端口（应用服务入口，由主动适配器调用）

| 端口（应用服务方法） | 来源命令/事件 | 主动适配器 | 说明 |
|--------------------|--------------|-----------|------|
| `AssetCommandService.registerAsset` | RegisterAsset | `RegisterAssetController`（REST） | ... |
| `AssetEventConsumer.onMalfunctionReported` | mes.malfunction.reported | `MalfunctionListener`（Kafka） | ... |

### 2.2 出站端口（领域层/应用层声明，由被动适配器实现）

| 端口接口 | 实现类 | 技术手段 | 说明 |
|---------|--------|---------|------|
| `AssetRepository`（domain.repository） | `MyBatisAssetRepository` | MyBatis + MySQL | 聚合根持久化 |
| `EquipmentGateway`（application.port） | `HttpEquipmentGateway` | REST 调设备接口 | ACL 翻译 |
| `DomainEventPublisher`（application.port） | `OutboxEventPublisher` | Outbox + Kafka | 事件发布 |

---

## 3. 领域层落地

### 3.1 聚合根

```java
public final class Equipment {
    private final AssetId assetId;
    private AvailabilityState state;
    // ... 字段与领域建模 §1.1 yaml 一一对应

    public AssetRegistered register(AssetKind kind, String model, ...) {
        // 校验 + 改状态 + 返回事件，不依赖任何框架
    }

    public AssetSuspended suspend(BlockingReason reason) {
        if (this.state != AvailabilityState.IN_SERVICE) {
            throw new DomainException("INV-07: 只能从 IN_SERVICE 停用");
        }
        this.state = AvailabilityState.SUSPENDED;
        return new AssetSuspended(this.assetId, reason, Instant.now());
    }
}
```

要求：
- 字段与领域建模 §1 的 yaml 逐项对齐，值对象用不可变类型。
- 每个行为方法对应领域建模一条行为；前置条件校验对应 INV，抛 `DomainException` 并带 INV 编号。
- **禁止** `@Entity` / `@Table` / `@Component` / 任何 Spring 注解。

### 3.2 值对象

```java
public record AssetIdentifier(String code, CodeScheme codeScheme) {
    public AssetIdentifier { Objects.requireNonNull(code); }
}
```

### 3.3 领域事件

```java
public record AssetSuspended(
    AssetId assetId,
    BlockingReason reason,
    Instant suspendedAt
) implements DomainEvent {
    @Override public String eventType() { return "AssetSuspended"; }
    @Override public int eventVersion() { return 1; }
}
```

> 事件名与领域建模 §5 逐字一致；Envelope 包装见 [消息处理实现说明 §4.3](../业务事件/消息处理实现说明.md)。

### 3.4 领域服务

跨聚合编排，照搬领域建模 §3 的方法签名；**不依赖** Repository 实现，只依赖端口接口。

---

## 4. 应用层落地

```java
@Service
@Transactional(rollbackFor = Exception.class)
public class AssetCommandService {

    private final AssetRepository repository;          // 端口
    private final DomainEventPublisher publisher;      // 端口
    private final AvailabilityRecomputeService recompute;

    public void suspendAsset(SuspendAssetCommand cmd) {
        Equipment asset = repository.findById(cmd.assetId())
            .orElseThrow(() -> new NotFoundException(...));
        AssetSuspended event = asset.suspend(cmd.reason());
        repository.save(asset);
        publisher.publish(event);   // 写 outbox，不直接 send Kafka
        recompute.onSuspended(cmd.assetId());
    }
}
```

要求：
- `@Transactional` 只在应用层；事务内只写本地 DB（业务表 + outbox），不直接调 Kafka（见 [消息处理实现说明 §7.1](../业务事件/消息处理实现说明.md)）。
- 应用服务不含业务规则，只编排（CLAUDE.md 分层）。
- 事件发布走 `DomainEventPublisher` 端口 → Outbox 适配器。

---

## 5. 适配器落地

### 5.1 主动适配器

**REST Controller**（对应命令应用服务）：
```java
@RestController
@RequestMapping("/api/assets")
public class AssetCommandController {
    private final AssetCommandService service;
    @PostMapping("/{id}/suspend")
    public void suspend(@PathVariable String id, @RequestBody SuspendRequest req) {
        service.suspendAsset(new SuspendAssetCommand(AssetId.of(id), req.reason()));
    }
}
```

**Kafka Listener**（对应事件消费应用服务）：
```java
@Component
public class MalfunctionListener {
    private final AssetEventConsumer consumer;
    @KafkaListener(topics = "mes.malfunction.reported",
                   groupId = "eam.malfunction-consumer")
    public void on(ConsumerRecord<String, String> record, Acknowledgment ack) {
        EventEnvelope envelope = parse(record.value());
        consumer.onMalfunctionReported(envelope);
        ack.acknowledge();
    }
}
```
> ack 顺序、幂等、DLT 严格遵循 [Kafka配置说明 §7/§8](../业务事件/Kafka配置说明.md) 与 [消息处理实现说明 §6](../业务事件/消息处理实现说明.md)。

### 5.2 被动适配器

**Repository 实现**：
```java
@Repository
public class MyBatisAssetRepository implements AssetRepository {
    private final AssetMapper mapper;
    public Optional<Equipment> findById(AssetId id) {
        AssetPO po = mapper.selectById(id.value());
        return Optional.ofNullable(po).map(AssetPO::toDomain);
    }
}
```
> PO（持久化对象）与领域聚合根之间用 mapper 双向转换；PO 带 `@TableName`，领域对象不带任何注解。

**ACL 实现**：
```java
@Repository
public class HttpEquipmentGateway implements EquipmentGateway {
    // 调相邻上下文 REST，翻译为本上下文值对象；隔离外部模型变更
}
```

---

## 6. 数据模型

### 6.1 写模型表

| 表 | 对应聚合根 | 说明 |
|----|----------|------|
| `asset_equipment` | Equipment | 设备主数据 |
| `asset_availability_projection` | AvailabilityProjection | 可用性投影 |

DDL 示例（关键约束）：
```sql
CREATE TABLE asset_equipment (
    asset_id          VARCHAR(64) NOT NULL,
    code              VARCHAR(64) NOT NULL,
    state             VARCHAR(32) NOT NULL,
    -- ...
    PRIMARY KEY (asset_id),
    UNIQUE KEY uk_asset_code (code)          -- INV-01
) ENGINE=InnoDB;
```
> 跨聚合不变式落到唯一索引/约束的，在表注释里标 INV 编号。

### 6.2 读模型表

CQRS 读侧，与写模型分离；查询应用服务只读读模型。

### 6.3 outbox / consumed_event

直接复用 [MySQL配置说明](../业务事件/MySQL配置说明.md) 的 `outbox_event` / `consumed_event`，不在本文重复 DDL。

---

## 7. 不变式落地

把领域建模 §6 的每条 INV 落到具体技术手段：

| INV | 保证手段 | 落地位置 |
|-----|---------|---------|
| INV-01 同一资产编码全局唯一 | 唯一索引 `uk_asset_code` | `asset_equipment` DDL |
| INV-03 Retired 后拒收任何事件 | 聚合根方法前置校验 | `Equipment.retire()` 后所有方法守卫 |
| INV-04 同一资产不可重复未决报废申请 | 领域服务查读模型 + 唯一索引 | `ScrapApprovalService` + `(asset_id, status=SUBMITTED)` |
| INV-27 可用性重算幂等 | 乐观锁 + 事件去重表 | `asset_availability_projection.version` + `consumed_event` |

> 每条 INV 必须能指到一个具体的类/表/索引——指不到说明实现没覆盖该不变式。

---

## 8. 消息配置

### 8.1 本上下文发布的主题

| Topic | 事件 | partition_key | 来源聚合 | DLT |
|-------|------|---------------|---------|-----|
| `eam.asset.lifecycle` | AssetRegistered/... | asset_id | Equipment | `eam.asset.lifecycle.DLT` |

> topic 名、前缀、partition_key 与领域建模 §7 / 事件风暴 §对外契约逐字一致；通用参数见 [Kafka配置说明](../业务事件/Kafka配置说明.md)。

### 8.2 本上下文订阅的主题

| Topic | 来源上下文 | Listener | 消费组 | 幂等键 |
|-------|-----------|----------|--------|--------|
| `pm.inspection.completed` | 点检保养 | `InspectionListener` | `eam.inspection-consumer` | event_id |

### 8.3 Outbox 配置

引用 [MySQL配置说明 §8](../业务事件/MySQL配置说明.md) 的属性前缀 `app.outbox.*`，本文只列本上下文特有覆盖项。

---

## 9. 事务边界

照搬 [消息处理实现说明 §7](../业务事件/消息处理实现说明.md) 的两条边界，落实到本上下文的具体方法：

```text
suspendAsset 命令 = 一个本地事务
  事务内：加载 Equipment → asset.suspend() → save → 写 outbox_event
  事务外：afterCommit 唤醒 Publisher（兜底有定时扫描）
```

---

## 10. 与领域建模的映射

领域建模元素→实现元素对位表（覆盖所有聚合/应用服务/事件/端口/INV/主题）。这是验收门槛——表不齐=实现没做完。

| 领域建模元素 | 实现元素 | 章节 |
|-------------|---------|------|
| Equipment 聚合根 | `Equipment` 类 | §3.1 |
| `suspend(reason)` 行为 | `Equipment.suspend()` + `AssetSuspended` 事件 | §3.1/§3.3 |
| AssetCommandService.suspendAsset | `AssetCommandService.suspendAsset` + `AssetCommandController` | §4/§5.1 |
| INV-07 | `suspend()` 前置校验 | §3.1/§7 |
| eam.asset.lifecycle 主题 | `OutboxEventPublisher` + topic 配置 | §8.1 |

---

## 11. 暂缓实现说明

领域建模里"暂缓模块"的落地预留，指明通过哪个端口/空实现衔接，避免回头返工。
````

---

## 6. 写作规范（关键）

1. **领域层按耦合性质分档**（CLAUDE.md OOD + DIP，技术栈为原生 MyBatis，不用 MyBatis-Plus）：
   - **表映射在 Mapper 侧**：聚合根/实体/值对象保持**纯 POJO**，不带任何映射注解；表映射用原生 MyBatis XML Mapper（`mapper/**/*.xml`）或 Mapper 接口上的 `@Select`/`@Insert`/`@Update`/`@Delete` 注解 SQL，统一写在 `infrastructure/persistence`。领域对象不认识 Mapper，Mapper 认识领域对象。
   - **允许（DI 注解）**：领域服务上的 `@Component`/`@Service` + **构造器注入端口接口**（禁止字段 `@Autowired`）。注解仅作装配提示，测试时仍可 new 并传 mock。
   - **禁止（基础设施行为耦合）**：聚合根/实体/领域服务方法内直接调用 `KafkaTemplate`/`RestTemplate`/`Mapper`/`SqlSession`，或带 `@Transactional`。引入运行时外部副作用、破坏可测性与事务一致性的，一概禁止。判断标准：**装配提示注解可留，运行时外部副作用一律禁止。**
2. **依赖倒置（DIP）**：Repository / Gateway / Publisher 端口接口在领域层或应用层，实现在 `infrastructure`（`persistence` / `acl` / `messaging`）。应用层依赖接口，不依赖实现。
3. **事务边界只在应用层**：`@Transactional` 只出现在应用服务；事务内只写本地 DB（业务表 + outbox），**禁止** `@Transactional` 方法内直接 `kafkaTemplate.send()`（见 [消息处理实现说明 §7.1](../业务事件/消息处理实现说明.md)）。
4. **每个适配器对应一个领域建模端口**：禁止出现没有业务语义的"技术适配器"。Controller 必须能指到某个命令应用服务方法；Listener 必须能指到某个事件消费方法。
5. **事件名与领域建模逐字一致**：类名、eventType、topic、partition_key 都不改。要改先改领域建模，再改实现，并在变更说明里记一笔。
6. **INV 必须可指到落地点**：每条不变式要么是聚合根方法守卫，要么是 DDL 唯一索引，要么是领域服务 + 唯一索引，要么是去重表。指不到=没实现。
7. **PO 与领域对象可合一**：聚合根/实体可直接带 `@TableName` 等映射注解映射表（见第 1 条），不强制 PO↔领域对象双模型。若聚合有复杂嵌套或需隔离框架细节，仍可单独建 PO + 转换器，由实现文档按上下文取舍。
8. **ACL 隔离外部模型**：调相邻上下文或外部系统时，经防腐层翻译为本上下文值对象，外部模型不渗透进领域层（CLAUDE.md 低耦合）。
9. **复用横向基础设施文档**：outbox DDL、producer/consumer 参数、幂等表、Envelope 字段——只引用 [实现说明/业务事件/](../../../实现说明/业务事件/) 下既有文档，**不重复**。本文只写本上下文特有的 topic、表、类。
10. **代码示例是 Java + Spring Boot**：与既有实现说明文档技术栈一致；示例要可直接落地，不是伪代码。
11. **包结构采用 Spring 常见 DDD 分层**：`interfaces/application/domain/infrastructure` 四层（接口层放 `controller`/`listener`/`scheduler`，基础设施层放 `persistence`/`acl`/`messaging`/`config`）；outbox 公共模块建在 `com.company.mes.common.outbox` 下跨服务复用，见 §1。
12. **ASCII 分层图单一方向**：从上往下驱动侧→应用→领域→被驱动侧，不混用。

---

## 7. 常见反模式（拒绝写法）

| 反模式 | 为什么不行 | 改怎么写 |
|--------|-----------|---------|
| 没有领域建模就直接写实现 | 端口/适配器失去业务语义根基 | 先走 domain-modeling skill |
| 聚合根方法里调 `KafkaTemplate`/`Mapper`/带 `@Transactional` | 基础设施行为混入领域，破坏可测性与事务一致性 | 映射/DI 注解可留；副作用下沉到应用层或适配器（见第 1 条三档） |
| `@Transactional` 方法内直接 `kafkaTemplate.send()` | 崩溃窗口丢事件（见消息处理说明） | 走 Outbox，事务内只写 DB |
| Repository 实现在领域层 | 依赖方向反了 | 接口在领域层，实现在 `infrastructure/persistence` |
| 出现没有业务语义的"技术适配器" | 分层混乱，无法追溯 | 每个适配器对应一个领域建模端口 |
| 事件名/ topic 与领域建模不一致 | 两份文档失同步 | 逐字照搬；要改先改领域建模 |
| 重复 outbox DDL / producer 参数 | 横向文档已存在，重复会失同步 | 只引用 [实现说明/业务事件/](../../../实现说明/业务事件/) |
| INV 没有落地点 | 不变式没被实现守护 | 每条 INV 指到类/表/索引/去重表 |
| 应用服务里写业务规则 | 违反 CLAUDE.md 分层 | 业务规则下沉到领域服务/聚合根 |
| 外部模型渗透进领域层 | 边界塌陷 | 经 ACL 翻译为本上下文值对象 |
| 把读模型表和写模型表混在一起 | CQRS 读写未分离 | 读模型表单列，查询服务只读读模型 |
| 一篇实现文档覆盖两个限界上下文 | 边界塌陷 | 拆成两篇，用订阅主题/REST 端点互联 |

---

## 8. 撰写流程（操作步骤）

### 8.A 新建文档

1. **确认前置**：`ls` 领域建模目录确认源文件存在；不存在则停止，提示用户先做领域建模。
2. **通读领域建模**：完整读一遍源领域建模，抽出"端口清单"——所有命令应用服务方法、事件消费方法、查询方法、领域服务、Repository 依赖、ACL 翻译点、发布主题、INV。这张清单就是 §2 端口清单和 §10 映射表的左列。
3. **定包结构**：按 §1 模板列出 `interfaces/application/domain/infrastructure`，把本上下文具体类名填进去。
4. **填端口清单**：§2.1 入站端口（应用服务→主动适配器）、§2.2 出站端口（领域/应用层接口→被动适配器）。
5. **落领域层**：§3 聚合根/值对象/事件/领域服务，字段与领域建模 yaml 一一对应，方法对应行为，前置校验对应 INV。
6. **落应用层**：§4 每个 `XxxCommandService` / `XxxEventConsumer` / `XxxQueryService`，标事务边界与 outbox 写入。
7. **落适配器**：§5.1 主动适配器（Controller/Listener/Scheduler），§5.2 被动适配器（Repository 实现/ACL 实现/Publisher 实现）。
8. **落数据模型**：§6 写模型表 + 读模型表 DDL，跨聚合不变式落到唯一索引并标 INV；outbox/consumed_event 只引用。
9. **落 INV**：§7 把领域建模 §6 每条 INV 指到具体落地点。
10. **落消息配置**：§8 发布主题（与领域建模 §7 一致）+ 订阅主题（与领域建模 §4 事件消费一致）。
11. **事务边界**：§9 照搬消息处理说明的两条边界，落到本上下文方法。
12. **补映射表**：§10 把第 2 步端口清单逐行对位到实现元素——验收门槛。
13. **暂缓说明**：§11 领域建模的暂缓项指明端口预留。
14. **diff 自检**：用 §9 自检清单过一遍。

### 8.B 修订既有文档

1. **先读旧文件 + 领域建模**：标出已发布的 topic、表名、端点路径、端口接口——这些是契约面，不能无声破坏。
2. **界定改动范围**：只动用户指定模块相关的适配器/表/topic；相邻上下文即使"顺手"也不代写（CLAUDE.md 硬约束）。
3. **保留契约稳定性**：已对外发布的 topic、REST 路径、表名尽量不改。若必须改：旧名标"已弃用"、新名同步给出，并在文档顶部加**变更说明**（旧名 → 新名、影响哪些下游、迁移方式）。
4. **与领域建模同步**：若领域建模改了事件名/聚合字段/INV，实现文档同步改并在变更说明记一笔。
5. **diff 自检**：改完后用 §9 自检清单过一遍，重点确认"§10 映射表覆盖领域建模最新所有元素""端口清单未遗漏新增应用服务"。

---

## 9. 自检清单（提交前过一遍）

**前置与边界**
- [ ] 领域建模源文件存在，文档头部"实现依据"链接指向它
- [ ] 文件落位在 `实现说明/<服务>/<限界上下文>.md`，文件名为 `XX上下文.md`
- [ ] **没有把其他模块的适配器/表/topic 顺手代写进来**（CLAUDE.md 硬约束）
- [ ] §0 分层图列出本上下文所有主动/被动适配器具体类名

**分层与依赖**
- [ ] 领域层按三档：映射/DI 注解允许；方法内无 `KafkaTemplate`/`RestTemplate`/数据访问层调用、无 `@Transactional`
- [ ] Repository / Gateway / Publisher 接口在领域层或应用层，实现在 `infrastructure`（`persistence`/`acl`/`messaging`）
- [ ] `@Transactional` 只在应用层；事务内不直接 `kafkaTemplate.send()`
- [ ] 每个适配器能对应到一个领域建模端口，无"技术适配器"

**与领域建模对齐**
- [ ] 字段、方法、事件名、topic、partition_key 与领域建模逐字一致
- [ ] §7 每条 INV 指到具体类/表/索引/去重表
- [ ] §10 映射表覆盖领域建模**所有**聚合/应用服务/事件/端口/INV/主题
- [ ] 暂缓项与领域建模"暂缓模块说明"一致

**基础设施复用**
- [ ] outbox DDL / producer 参数 / 幂等表 / Envelope 只引用 [实现说明/业务事件/](../../../实现说明/业务事件/)，未重复
- [ ] 包结构对齐 [消息处理实现说明 §11](../业务事件/消息处理实现说明.md)

**修订场景**
- [ ] 已发布的 topic/路径/表名/端口接口未做破坏性改名；若改名已在顶部留变更说明，旧名标"已弃用"
- [ ] 领域建模变更已同步到实现文档
