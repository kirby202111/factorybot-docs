---
name: implementation
description: 在本仓库（factorybot-docs，PCBA + 整机组装混合型车间 MES）下，以某个限界上下文已落地的“领域建模”文档为依据，按 Java + Spring 技术栈撰写或修订该限界上下文的“实现设计”文档，输出分层包结构、聚合代码落地、Repository/持久化、应用服务事务、领域事件/Outbox、API/ACL、读模型、测试策略与关键代码骨架。当用户要求“做 / 写 / 补 / 改 XX 限界上下文的代码实现设计 / 实现方案 / Java Spring 落地 / 从领域模型落代码”等任务时启用。
---

# 代码实现设计 Skill（Java + Spring + DDD）

本 skill 规定本仓库**实现设计（Implementation Design）类文档**的统一骨架、分层边界、代码映射规则与质量门槛。目标是把已落地的“领域建模”文档进一步翻译为可指导 Java + Spring 工程编码的设计说明，做到：

- **可追溯**：每个聚合根 / 实体 / 值对象 / 领域服务 / 应用服务 / 领域事件，都能追溯到领域建模文档。
- **可编码**：给出包结构、类职责、接口、事务边界、持久化映射、事件发布、测试切面与关键代码骨架。
- **不过度设计**：吸收大厂 DDD 工程经验（COLA / 洋葱架构 / 六边形架构 / Clean Architecture / 事务 Outbox / CQRS 读写分离），但按 MES 场景取舍，不把示例做成框架秀。
- **边界清晰**：领域层保持业务表达，Spring / MyBatis / MQ / HTTP 等技术细节停留在应用层、基础设施层或防腐层。

> **前置纪律（来自项目 CLAUDE.md）**：当用户指定要做某个模块时，**只读该模块自身文档与其直接对接的领域（domain），不把其它模块杂糅进来**。实现设计只承载本限界上下文的代码落地，不代写相邻限界上下文的实现。

> **术语约定（全文强制，与 [domain-modeling](../domain-modeling/SKILL.md) skill 一致）**：
> - **限界上下文（Bounded Context）**：实现设计的代码边界与服务内模块边界。正文中具体限界上下文统一写作“XX限界上下文”，**文件名**按仓库既有约定写作 `XX上下文.md`。
> - **领域模型**：指 `领域模型/<服务>/领域建模/<限界上下文>.md` 中已确认的聚合、实体、值对象、服务、事件与不变式。
> - **实现设计**：不是完整源码，不写大段可直接复制的业务代码；它说明“应该如何组织代码、哪些类负责什么、关键方法如何表达业务规则、哪些技术边界不能越界”。

---

## 1. 何时使用本 skill

- 用户说“做 / 写 / 补 / 改 XX 限界上下文的实现设计 / 代码实现设计 / Java Spring 实现方案”。
- 用户给出一个限界上下文名称，需要把已落地的领域建模文档翻译为 Java + Spring 分层代码方案。
- 用户要求“基于 DDD 领域模型生成代码骨架 / 包结构 / Repository / ApplicationService / DomainEvent / Outbox / ACL / 测试策略”。
- 用户要求判断某个限界上下文落代码时“哪些地方用 DDD、哪些地方不要过度设计”。

**强前置**：本 skill **必须以已存在的领域建模文档为实现依据**。若该限界上下文的领域建模尚不存在，**先停止**，提示用户先走 [domain-modeling](../domain-modeling/SKILL.md) skill 落地领域模型，再回来做实现设计——领域建模是源，实现设计是派生，跳过领域建模直接写实现会导致类、方法、事件、不变式与业务语义脱节。

不适用：
- 纯事件风暴 / 纯领域建模 → 使用对应 skill。
- 纯数据库建表脚本 → 只可作为实现设计的持久化章节补充，不单独用本 skill 生成散乱表结构。
- 纯 API 文档 → 只可作为应用层入口章节补充，不能替代领域实现设计。
- 直接写生产源码 → 本 skill 先产出实现设计；若用户明确要求编码，再按实现设计落代码。

---

## 2. 文档落位规则

实现设计文档统一落到 [领域模型/](../../../领域模型/) 下，与事件风暴、领域建模**同服务不同子目录**平行存放：

```text
领域模型/<服务>/事件风暴/<限界上下文>.md      ← 上游源 1
领域模型/<服务>/领域建模/<限界上下文>.md      ← 上游源 2（本 skill 的直接依据）
领域模型/<服务>/实现设计/<限界上下文>.md      ← 本 skill 产物
```

完整目录结构（与 [领域总览](../../../领域模型/领域总览.md) 一致，仅三个服务）：

```text
领域模型/
├── 制造资源服务/实现设计/<限界上下文>.md
├── 生产执行服务/实现设计/<限界上下文>.md
└── 设备管理服务/实现设计/<限界上下文>.md
```

> 制造资源服务下若领域建模已按子域细分，则 `实现设计/` 下保持同样子目录结构；其它服务直接以限界上下文为文件名，不再嵌套。

**新建文件前必做三件事**：
1. 确认 `领域模型/<服务>/领域建模/<限界上下文>.md` 存在；不存在则按 §1 强前置处理。
2. 确认 `领域模型/<服务>/实现设计/` 目录下是否已有同名旧文件。**若旧文件存在但归属变了，先与用户确认是改写还是新建**。
3. 文档头部“实现依据”链接必须指向该领域建模源文件（相对路径 `../领域建模/XX上下文.md`）。如需辅助追溯，可同时链接事件风暴源文件。

> **路径提示**：本 skill 文件位于 `.claude/skills/implementation/`，指向 `领域模型/` 的相对路径需写作 `../../../领域模型/`（三层 `../` 回到仓库根）。

---

## 3. 总体落地取舍（Java + Spring + 大厂经验的边界）

实现设计必须体现 DDD 的工程落地，但不能堆砌概念。默认采用下列取舍：

### 3.1 默认架构风格

采用“**微服务内的整洁分层 + 端口适配器**”模型：

```text
interfaces   → 对外入口：REST Controller / MQ Listener / Scheduler / DTO
application  → 用例编排：ApplicationService / Command / Query / Transaction / Idempotency
domain       → 领域核心：AggregateRoot / Entity / ValueObject / DomainService / DomainEvent / Repository Port
infrastructure → 技术实现：MyBatis / MyBatis-Plus、MQ、Outbox、ACL Client、Redis、文件、设备接口
```

**强制边界**：
- `domain` 不依赖 Spring、MyBatis、MQ、HTTP、Redis。
- `application` 可以依赖 Spring 事务、领域对象、Repository Port、ACL Port，但不写业务规则。
- `infrastructure` 实现 Repository / ACL / EventPublisher 等端口，负责技术细节。
- `interfaces` 只做协议转换与入参校验，不直接操作 Repository，不绕过 ApplicationService。

### 3.2 聚合实现取舍

- 聚合根优先使用**充血模型**：业务行为写成聚合根方法，方法内部校验不变式并记录领域事件。
- 值对象使用不可变对象：Java `record` 或 final class；对 JDK / Jackson / MyBatis 类型处理器兼容性敏感时说明取舍。
- 实体只在聚合内部存在，不单独暴露 Repository。
- 不默认使用 Event Sourcing；MES 主链更强调可查询、可追溯、可纠错，默认采用**状态存储 + 领域事件 + Outbox**。
- 对实时性高、状态机复杂、审计要求强的对象，可在文档中建议“事件日志/操作流水表”作为补充，但不要把整个上下文默认改成事件溯源。

### 3.3 持久化取舍

本仓库实现设计**仅使用 MyBatis / MyBatis-Plus** 作为持久化方案，不使用 Spring Data JPA / Hibernate。取舍理由：MES 场景通常存在复杂追溯查询、报表投影、历史数据兼容、SQL 可控性与性能调优要求；MyBatis 更适合把写模型聚合重建与读模型 SQL 投影分开处理。

| 选择 | 适用场景 | 约束 |
|------|----------|------|
| MyBatis | 复杂 SQL、聚合装载需要手写 ResultMap、多表/子表装配、追溯查询性能敏感 | 领域对象与 DO/PO 分离；Mapper 只做数据访问；Repository Adapter 负责装配聚合 |
| MyBatis-Plus | 单表 CRUD、字典/配置/Outbox/幂等表等基础设施表、简单读模型 | 禁止把 ServiceImpl 当应用服务；禁止绕过聚合直接改写核心状态 |

默认建议：**写模型 Repository 用聚合语义，读模型 Query 用 SQL/投影语义**。不要为了“纯 DDD”把所有查询都塞进聚合，也不要为了“开发快”让 Controller 直接调 Mapper 改状态。

**MyBatis 落地边界**：
- `Mapper` 是基础设施层 DAO，不是领域 Repository。
- `DO/PO` 是数据库行对象，不是领域对象。
- `PersistenceAssembler` 负责 `DO/PO ↔ AggregateRoot` 转换。
- 写侧保存以聚合根为单位；聚合内子实体可拆表，但由同一个 Repository Adapter 统一保存。
- 读侧可以直接返回 ReadModel / View DTO，不强制还原聚合。

### 3.4 事件与一致性取舍

- 聚合内强一致：单事务内完成。
- 跨聚合协作：领域服务 + 应用服务编排；跨上下文协作通过集成事件 / Saga / 流程编排。
- 对外事件发布：默认采用**事务 Outbox**，避免“数据库提交成功但 MQ 发送失败”。
- 消费外部事件：必须有幂等键、去重表或消费日志；通过 ACL 翻译为本上下文命令或值对象。
- 不默认做分布式事务；MES 现场系统以可恢复、可补偿、可追溯为优先。

---

## 4. 从领域建模到代码实现的映射规则（核心）

实现设计不是重新设计业务，而是把领域建模里的元素**逐一对位**到 Java + Spring 工程结构。下表是**强制对齐表**，设计时逐行落实，不得遗漏：

| 领域建模元素（源） | 实现设计对应（产物） | 落实要点 |
|--------------------|----------------------|----------|
| 聚合根 | `domain.model.<aggregate>.<AggregateRoot>` | 聚合根类、行为方法、领域事件记录、Repository Port |
| 实体 | 聚合包内实体类 | 不提供独立 Repository；生命周期受聚合根控制 |
| 值对象 | `record` / final class / enum | 不可变；构造时校验；禁止贫血 String 满天飞 |
| 聚合根行为 | 聚合根业务方法 | 方法名与领域建模一致；校验 INV；状态变更；记录 DomainEvent |
| 领域服务 | `domain.service.<XxxDomainService>` | 只放跨聚合或不自然归属单聚合的业务规则；不持有事务注解 |
| 应用服务 | `application.service.<XxxApplicationService>` | 接收 Command；开启事务；加载聚合；调用领域行为；保存；发布/落 Outbox |
| 命令应用服务 | Command DTO + ApplicationService 方法 | Command 是用例入参，不是领域对象；Controller/MQ Listener 转 Command |
| 查询应用服务 / 读模型 | `application.query` + `infrastructure.query` | 不污染聚合；复杂查询直接走投影 / SQL / ES（如需要） |
| 领域事件 | `domain.event.<XxxEvent>` | 聚合内记录；应用层收集并写 Outbox；事件名与领域建模逐字一致 |
| 对外契约主题 | Outbox/EventPublisher 配置 | topic、eventType、主消费方照搬领域建模 §对外契约 |
| 不变规则 INV | 聚合方法 / 领域服务 / DB 约束 / 幂等表 / Saga | 每条 INV 说明由哪一层保证，不能只写“代码校验” |
| 上下文外事件消费 | MQ Listener + ACL + ApplicationService | 外部事件不得直接进领域；先翻译成本上下文命令/值对象 |
| 热点 / 暂缓项 | 实现风险 / 暂缓设计 | 不确定点不得静默消失；列出推荐取舍与后续扩展点 |

> **可追溯性硬约束**：实现设计文档 §12“与领域建模的映射”必须给出一张**领域模型元素 → 实现元素**的对位表，让读者能从任何一个聚合、方法、事件、INV 查到它将落到哪个包、哪个类、哪个方法、哪种技术机制。这是本 skill 的验收门槛之一。

> **反向一致性**：若实现设计过程中发现领域建模有遗漏（如某命令没有应用服务入口、某领域事件无发出者、某 INV 无保证机制），**先回去补领域建模**，再回来改实现设计——不要在实现设计里私自补业务概念。

---

## 5. 标准章节骨架（按顺序）

下面是一篇合格实现设计文档的章节模板。**章节标题与编号必须保持一致**，便于不同限界上下文横向比较。

> **编号说明**：以下 `## 0` ~ `## 12` 是**目标文档**的章节结构，不是本 skill 自身的章节编号。

````markdown
# <XX>上下文 — 实现设计（Java + Spring）

> **限界上下文**：<XX>
> **所属服务**：<制造资源服务 / 生产执行服务 / 设备管理服务>
> **实现依据**：[领域建模 — <XX>上下文](../领域建模/<XX>上下文.md)
> **技术栈取舍**：Java 17+ / Spring Boot 3.x / MyBatis 或 MyBatis-Plus / Outbox / <MQ>

---

## 0. 实现总览

（一段话讲清本上下文代码实现目标、核心聚合、关键事务、关键集成点。）

### 分层边界

```text
interfaces  -> application  -> domain
                  |             ^
                  v             |
            infrastructure ------+
```

**本实现坚持**：
- 领域层不依赖 Spring / MyBatis / MQ。
- 应用服务负责编排与事务，不写业务规则。
- 基础设施层实现 Repository、Outbox、ACL、Query。

---

## 1. 模块与包结构

```text
com.factorybot.<service>.<context>
├── interfaces
│   ├── rest
│   └── mq
├── application
│   ├── command
│   ├── query
│   ├── service
│   └── event
├── domain
│   ├── model
│   │   └── <aggregate>
│   ├── service
│   ├── event
│   └── repository
└── infrastructure
    ├── persistence
    ├── query
    ├── messaging
    └── acl
```

| 包 | 职责 | 禁止事项 |
|----|------|----------|
| `interfaces` | 协议适配、DTO 转 Command | 禁止写业务规则、禁止直接调 Mapper 改状态 |
| `application` | 用例编排、事务、幂等、事件发布 | 禁止承载 INV 业务判断 |
| `domain` | 聚合、值对象、领域服务、领域事件 | 禁止依赖 Spring / MyBatis / MQ |
| `infrastructure` | 技术实现、持久化、消息、ACL | 禁止反向定义业务语义 |

---

## 2. 聚合代码落地

每个聚合根按下列子结构写：

### 2.N <AggregateRoot>（<中文名>）

**来源**：领域建模 §1.N  
**一致性边界**：照搬领域建模，补充代码层如何保证。  
**实现形态**：`domain.model.<aggregate>.<AggregateRoot>`

```java
public class <AggregateRoot> extends AggregateRoot<<AggregateId>> {
    private <AggregateId> id;
    private <State> state;

    public void doSomething(...) {
        // 1. guard invariant
        // 2. change state
        // 3. registerEvent(new SomethingHappened(...))
    }
}
```

**行为映射**：
| 领域行为 | Java 方法 | 校验 INV | 发出事件 | 备注 |
|----------|-----------|----------|----------|------|
| `doSomething(args)` | `<AggregateRoot>.doSomething(...)` | INV-XX | `SomethingHappened` | ... |

**实现注意**：
- 聚合根方法使用业务动词，禁止 `setXxx` / `updateXxx` 流水账。
- 聚合内部实体只能由聚合根创建或变更。
- 聚合根只保存其它聚合 ID，不持有其它聚合对象引用。

---

## 3. 值对象与枚举实现

| 值对象 | Java 类型 | 不变校验 | 持久化方式 | 备注 |
|--------|-----------|----------|------------|------|
| XxxId | `record XxxId(String value)` | 非空 / 格式 | varchar | 聚合根 ID |
| XxxState | `enum XxxState` | 状态机由聚合方法控制 | varchar | 禁止外部直接改状态 |

**值对象原则**：
- 优先不可变：`record` / final class。
- 构造时完成格式与范围校验。
- 不把业务含义退化为裸 `String` / `Long` 在各层传递。

---

## 4. Repository 与持久化设计

### 4.1 Repository Port

```java
public interface <AggregateRoot>Repository {
    Optional<<AggregateRoot>> findById(<AggregateId> id);
    void save(<AggregateRoot> aggregate);
}
```

### 4.2 持久化实现

**技术选择**：<MyBatis / MyBatis-Plus>  
**选择理由**：...（复杂聚合装载优先 MyBatis；简单基础设施表或简单读模型可用 MyBatis-Plus）

| 领域对象 | DO/PO / 表 | Mapper | 转换器 | 说明 |
|----------|------------|--------|--------|------|
| <AggregateRoot> | `<AggregateRoot>DO` / `<table_name>` | `<AggregateRoot>Mapper` | `<AggregateRoot>PersistenceAssembler` | 聚合根主表 |
| <Entity> | `<Entity>DO` / `<child_table>` | `<Entity>Mapper` | 同上 | 聚合内子表，由聚合 Repository 统一装配 |

**数据库约束对应 INV**：
| 约束 | 对应 INV | 实现机制 |
|------|----------|----------|
| uk_xxx | INV-XX | 唯一索引 |

---

## 5. 应用服务与事务边界

每个命令用例按下列结构写：

### 5.N <UseCase>ApplicationService

```java
@Service
public class <UseCase>ApplicationService {
    @Transactional
    public <Result> handle(<Command> command) {
        // 1. idempotency check if needed
        // 2. load aggregate / call domain service
        // 3. invoke aggregate behavior
        // 4. save aggregate
        // 5. collect domain events and write outbox
    }
}
```

| 用例 | Command | 事务边界 | 调用领域对象 | 产出事件 | 幂等策略 |
|------|---------|----------|--------------|----------|----------|
| ... | ... | 单聚合事务 / 跨聚合编排 | ... | ... | ... |

**边界要求**：
- `@Transactional` 放在应用服务，不放领域对象。
- 应用服务只编排，不写 INV 业务规则。
- 需要跨聚合读取/判断时，优先交给领域服务表达业务语义。

---

## 6. 领域服务实现

| 领域服务 | Java 类 | 使用场景 | 依赖端口 | 为什么不在聚合根内 |
|----------|---------|----------|----------|--------------------|
| XxxDomainService | `domain.service.XxxDomainService` | ... | Repository Port | 跨聚合 / 不自然归属单聚合 |

```java
public class XxxDomainService {
    public void coordinate(...) {
        // domain decision only
    }
}
```

> 领域服务可以依赖 Repository Port 或其它领域端口，但不直接依赖 Mapper、MQ Client、REST Client。

---

## 7. 领域事件、Outbox 与消息发布

### 7.1 领域事件类

| 领域事件 | Java 类 | 发出者 | 关键字段 | 对外发布 |
|----------|---------|--------|----------|----------|
| XxxHappened | `domain.event.XxxHappened` | <AggregateRoot> | ... | 是 / 否 |

### 7.2 Outbox 设计

```text
聚合根 registerEvent
        ↓
应用服务 save aggregate
        ↓
同事务写 outbox_event
        ↓
OutboxPublisher 异步发布 MQ
        ↓
消费成功标记 published
```

| 字段 | 说明 |
|------|------|
| event_id | 全局唯一事件 ID |
| aggregate_id | 聚合根 ID |
| event_type | 事件类型 |
| topic | 对外主题 |
| payload | JSON 载荷 |
| status | INIT / PUBLISHED / FAILED |
| occurred_at | 发生时间 |

**取舍说明**：默认不用分布式事务；使用 Outbox 保证数据库状态与消息发布最终一致。

---

## 8. 接口层与防腐层（ACL）

### 8.1 REST / RPC 入口

| 接口 | Controller | 入参 DTO | Command | 应用服务 |
|------|------------|----------|---------|----------|
| POST /xxx | XxxController | XxxRequest | XxxCommand | XxxApplicationService |

### 8.2 MQ 事件消费入口

| 订阅主题 | 外部事件 | Listener | ACL 转换 | 本上下文命令 | 幂等键 |
|----------|----------|----------|----------|--------------|--------|
| ... | ... | XxxListener | XxxAclTranslator | XxxCommand | event_id |

**ACL 原则**：
- 外部事件 / DTO 不直接进入领域层。
- ACL 把外部模型翻译为本上下文统一语言。
- 消费外部事件必须记录幂等，避免设备重复上报、MQ 重投导致重复过账。

---

## 9. 查询模型与读侧设计

| 查询场景 | QueryService | 数据来源 | 是否读模型投影 | 说明 |
|----------|--------------|----------|----------------|------|
| ... | XxxQueryService | SQL / projection table | 是 / 否 | ... |

**读写取舍**：
- 写侧围绕聚合根保护不变式。
- 读侧围绕页面 / 报表 / 追溯链路组织投影。
- 查询不反向污染聚合；复杂报表不要求通过 Repository 加载聚合再拼装。

---

## 10. 异常、幂等与并发控制

| 场景 | 实现策略 | 对应 INV / 风险 |
|------|----------|-----------------|
| 重复命令 | request_id / command_id 幂等表 | 防重复执行 |
| 并发修改同一聚合 | 乐观锁 version / 悲观锁 | 防状态覆盖 |
| 外部事件重复投递 | consumed_event 去重表 | 防重复消费 |
| 跨聚合最终一致 | Saga / 补偿任务 / 对账任务 | 防中间态丢失 |

**MES 特别注意**：设备上报、扫码过点、物料扣减、维修/返工/返修事件通常存在重复、乱序、迟到；实现设计必须说明至少一种幂等与乱序处理策略。

---

## 11. 测试策略

| 测试层级 | 测试对象 | 重点 |
|----------|----------|------|
| 领域单元测试 | 聚合根 / 值对象 / 领域服务 | INV、状态机、事件发出 |
| 应用服务测试 | ApplicationService | 事务编排、Repository 调用、Outbox 写入 |
| 持久化测试 | Repository Adapter | 映射、唯一索引、乐观锁 |
| 消息测试 | Listener / OutboxPublisher | 幂等、重投、ACL 转换 |
| 契约测试 | 发布事件 / API | topic、payload 兼容性 |

每条核心 INV 至少对应一个失败用例；每个状态机终态至少有一个“拒绝非法命令”的测试。

---

## 12. 与领域建模的映射

| 领域建模元素 | 实现元素 | 保证机制 / 备注 |
|--------------|----------|-----------------|
| 聚合根 Xxx | `domain.model.xxx.Xxx` | Repository: `XxxRepository` |
| 行为 `doSomething()` | `Xxx.doSomething()` | 校验 INV-XX，发出 `SomethingHappened` |
| 领域事件 `SomethingHappened` | `domain.event.SomethingHappened` + Outbox topic | topic: `svc.xxx.something-happened` |
| INV-XX | 聚合方法 / 唯一索引 / 幂等表 | ... |
| 领域服务 XxxService | `domain.service.XxxDomainService` | ... |
| 查询读模型 XxxView | `application.query.XxxQueryService` | ... |

---

## 暂缓与扩展点（可选）

按当前阶段不展开的实现能力，留待后续，并指明不影响当前实现的原因：
- 例如：暂不引入 Event Sourcing，仅保留 Outbox + 操作流水；未来如审计要求提升，可从 `domain_event_log` 扩展。
- 例如：暂不引入 CQRS 独立读库，先用同库投影表；后续追溯查询压力上来再拆 Elasticsearch / ClickHouse。
````

---

## 6. 写作规范（关键）

1. **以领域建模为源，不重新发明业务**：聚合、行为、事件、INV 名称必须与领域建模对齐；要改业务名，先改领域建模。
2. **面向对象，不是面向表脚本**：聚合根行为是方法，不是 `updateStatus/save/delete` 流水账。表结构是持久化结果，不是领域模型本身。
3. **领域层不依赖框架**：领域对象禁止 `@TableName`、`@TableId`、`@Service`、`@Autowired`、`@Transactional`、`@KafkaListener` 等框架注解；MyBatis / MyBatis-Plus 注解只允许出现在基础设施层 DO/PO 或 Mapper 上。
4. **应用服务只编排**：应用服务可以校验入参、事务、权限、幂等、加载保存、写 Outbox；业务判断下沉聚合根或领域服务。
5. **Repository 是聚合语义，不是 Mapper 包装**：写侧 Repository 以聚合根为单位；复杂查询不要硬塞进 Repository，放 QueryService。
6. **值对象不可变且有语义**：ID、编码、数量、状态、工位、设备号等不要全用裸 String/Long；用值对象承载校验与语言。
7. **领域事件在聚合行为中产生**：状态改变的方法必须记录领域事件；应用服务负责收集并持久化/发布，不在 Controller 里拼事件。
8. **Outbox 默认优先于直接发 MQ**：只要事件与数据库状态必须一致，默认使用事务 Outbox；直接发 MQ 需要说明为什么可接受。
9. **跨上下文只走 ACL**：外部 DTO / Event / Client 不进入 domain；经 ACL 翻译成本上下文命令、值对象或领域端口结果。
10. **幂等是一等设计**：MES 场景的扫码、设备数据、MQ 消费、ERP 回调都可能重复；必须说明幂等键与去重位置。
11. **并发控制要落到机制**：只写“保证并发安全”不合格；要说明乐观锁、唯一索引、悲观锁、串行队列或业务锁。
12. **不变式必须能定位实现层**：每条 INV 都要对应聚合方法、领域服务、数据库约束、幂等表、Saga 或补偿任务之一。
13. **接口 DTO 不等于领域对象**：Request/Response/Message DTO 可贫血，领域对象不可贫血；转换器职责要明确。
14. **大厂方案要取舍，不照搬**：可以借鉴 COLA/六边形/整洁架构，但只保留对本上下文有价值的层次与端口，不制造无意义抽象。
15. **ASCII 图保持单一方向**：分层、事件流、Outbox 流程图统一从上到下或从左到右。

---

## 7. 常见反模式（拒绝写法）

| 反模式 | 为什么不行 | 改怎么写 |
|--------|------------|----------|
| 没有领域建模就直接写实现 | 类与方法没有业务来源，容易退化成 CRUD | 先走 domain-modeling skill |
| Controller 直接调 Mapper 改状态 | 绕过应用服务和聚合不变式 | Controller → Command → ApplicationService → Aggregate |
| ApplicationService 里堆 if/else 业务规则 | 应用层变成过程脚本，领域贫血 | 规则下沉聚合根或领域服务 |
| 聚合根只有 getter/setter | 没有行为，无法保护不变式 | 用业务方法表达命令和状态流转 |
| Repository 为每张表建一套 | 破坏聚合边界，实体被外部随意修改 | Repository 以聚合根为单位 |
| 为所有对象都建 DomainService | 领域服务滥用，模型失去内聚 | 单聚合规则放聚合根，跨聚合才建领域服务 |
| 直接把外部事件对象传进领域方法 | 上下文边界塌陷 | MQ Listener → ACL Translator → Command/VO |
| 事务内直接发 MQ | DB 成功 MQ 失败会丢事件 | 同事务写 Outbox，异步发布 |
| 所有查询都加载聚合再拼 DTO | 性能差，读写职责混淆 | QueryService + 投影 / SQL |
| 只写“使用乐观锁”但不说明 version | 无法编码与测试 | 写清 version 字段、冲突异常、重试/拒绝策略 |
| INV 只出现在文档，不映射代码 | 不可验证，设计无法落地 | 在 §12 映射到类/方法/索引/幂等表 |
| 把大厂分层照搬成十几层 | 抽象过度，开发成本高 | 维持 interfaces/application/domain/infrastructure 四层即可 |
| 一篇实现设计覆盖多个限界上下文 | 代码边界塌陷，后续无法微服务化 | 每个限界上下文一篇，通过 ACL / 事件契约协作 |

---

## 8. 撰写流程（操作步骤）

### 8.A 新建文档

1. **确认前置**：确认领域建模源文件存在；不存在则停止，提示用户先做领域建模。
2. **确认归属**：根据 [领域总览](../../../领域模型/领域总览.md) 与领域建模路径，确定服务与目标路径 `领域模型/<服务>/实现设计/XX上下文.md`。
3. **通读领域建模**：完整抽出聚合根、实体、值对象、领域服务、应用服务、领域事件、INV、对外契约、读模型、暂缓项。
4. **确定工程取舍**：说明 Java/Spring 基线、MyBatis / MyBatis-Plus 使用边界、事件方案（Outbox）、读模型策略、是否需要 ACL。
5. **设计包结构**：按四层结构给出包名与职责，明确禁止事项。
6. **落聚合代码**：每个聚合根给 Java 类骨架、行为方法映射、事件记录方式、实体和值对象实现方式。
7. **落 Repository**：定义 Repository Port；说明 Adapter、表、转换器、唯一索引、乐观锁。
8. **落应用服务**：把领域建模中的应用服务和命令逐个映射成 Command + ApplicationService 方法，标事务边界、幂等策略、Outbox 写入点。
9. **落领域服务**：为每个领域服务写 Java 类、依赖端口、调用方，并回答“为什么不放聚合根内”。
10. **落事件机制**：领域事件类、Outbox 表、发布流程、topic 映射、失败重试与补偿。
11. **落接口与 ACL**：REST/MQ 入口、DTO/Command 转换、外部事件翻译、幂等键。
12. **落读模型**：查询服务、投影表/SQL、与写模型的同步方式。
13. **落异常并发**：重复命令、重复事件、并发状态变更、乱序/迟到事件的处理。
14. **落测试策略**：每条 INV 对应测试层级；状态机非法边必须有失败用例。
15. **补映射表**：§12 把领域建模所有元素逐行对位到实现元素，这是验收门槛。
16. **diff 自检**：用 §10 自检清单过一遍。

### 8.B 修订既有文档

1. **先读旧实现设计 + 最新领域建模**：标出已发布的包名、类名、事件类名、topic、表名、INV 映射。
2. **界定改动范围**：只动用户指定限界上下文；相邻上下文即使“顺手”也不代写。
3. **保留契约稳定性**：已对外发布的 API、topic、eventType、表关键字段尽量不改。若必须改，顶部加**变更说明**（旧名 → 新名、影响下游、迁移方式）。
4. **实现映射随领域模型同步**：领域建模新增/删除/改名的聚合、事件、INV，必须同步到 §12。
5. **不变式编号延续**：沿用领域建模 INV 编号；废弃项不删映射，标“已废止（原因）”。
6. **技术取舍变更要说明成本**：如 MyBatis 与 MyBatis-Plus 使用边界调整、直接 MQ 改 Outbox、同库读模型改独立读库，要说明迁移影响。
7. **diff 自检**：重点确认领域层未被框架污染、应用层未堆业务、事件发布仍有 Outbox/幂等保障。

---

## 9. 最小 Demo 片段（参考骨架）

下方是一段缩水版“备件管理限界上下文”实现设计片段，演示包结构、聚合代码、Repository、应用服务、Outbox、ACL 与映射表的形态。**真实文档每节都要更完整**。备件管理限界上下文为示意，仓库中尚无该文件。

````markdown
# 备件管理上下文 — 实现设计（Java + Spring）

> **限界上下文**：备件管理
> **所属服务**：制造资源服务
> **实现依据**：[领域建模 — 备件管理上下文](../领域建模/备件管理上下文.md)
> **技术栈取舍**：Java 17 / Spring Boot 3.x / MyBatis / Outbox / Kafka

---

## 0. 实现总览

备件管理限界上下文以 `SparePartLot` 聚合保护单批次库存不变式，以 `ReorderRule` 聚合维护再订货规则。写侧通过聚合根方法产生领域事件，应用服务同事务保存聚合并写 Outbox；读侧通过库存投影表支撑维修领料查询。

---

## 1. 模块与包结构

```text
com.factorybot.resource.spareparts
├── interfaces
│   ├── rest
│   └── mq
├── application
│   ├── command
│   ├── query
│   └── service
├── domain
│   ├── model.sparepartlot
│   ├── model.reorderrule
│   ├── event
│   └── repository
└── infrastructure
    ├── persistence.mybatis
    ├── messaging.outbox
    └── acl.repair
```

---

## 2. 聚合代码落地

### 2.1 SparePartLot（备件批次）

```java
public class SparePartLot extends AggregateRoot<LotId> {
    private LotId id;
    private PartNo partNo;
    private LotNo lotNo;
    private Quantity quantity;
    private LotState state;

    public void accept() {
        requireState(LotState.INCOMING);
        this.state = LotState.IN_STOCK;
        registerEvent(new LotAccepted(id, partNo, lotNo));
    }

    public void deduct(Quantity qty, ConsumptionSource source) {
        requireState(LotState.IN_STOCK);
        this.quantity = this.quantity.subtract(qty);
        if (this.quantity.isZero()) {
            this.state = LotState.DEPLETED;
        }
        registerEvent(new SparePartConsumed(id, qty, source));
        registerEvent(new InventoryChanged(partNo, this.quantity));
    }
}
```

| 领域行为 | Java 方法 | 校验 INV | 发出事件 |
|----------|-----------|----------|----------|
| `accept()` | `SparePartLot.accept()` | INV-03 | `LotAccepted` |
| `deduct(qty)` | `SparePartLot.deduct(...)` | INV-01, INV-04 | `SparePartConsumed`, `InventoryChanged` |

---

## 4. Repository 与持久化设计

```java
public interface SparePartLotRepository {
    Optional<SparePartLot> findById(LotId id);
    Optional<SparePartLot> findAvailableByPartNo(PartNo partNo);
    void save(SparePartLot lot);
}
```

| 领域对象 | 表 | 约束 |
|----------|----|------|
| SparePartLot | `spare_part_lot` | `uk_part_lot(part_no, lot_no)` 对应 INV-02；`version` 乐观锁 |

---

## 5. 应用服务与事务边界

```java
@Service
public class DeductInventoryApplicationService {
    @Transactional
    public void handle(DeductInventoryCommand command) {
        idempotencyChecker.check(command.commandId());
        SparePartLot lot = lotRepository.findAvailableByPartNo(command.partNo())
                .orElseThrow(SparePartNotAvailableException::new);
        lot.deduct(command.quantity(), command.source());
        lotRepository.save(lot);
        outboxWriter.write(lot.pullDomainEvents());
    }
}
```

---

## 7. 领域事件、Outbox 与消息发布

| 领域事件 | Java 类 | 发出者 | topic |
|----------|---------|--------|-------|
| InventoryChanged | `domain.event.InventoryChanged` | `SparePartLot` | `spare.inventory.changed` |
| InventoryLowWarned | `domain.event.InventoryLowWarned` | `InventoryReorderDomainService` | `spare.inventory.alert` |

---

## 8. 接口层与防腐层（ACL）

| 订阅主题 | 外部事件 | Listener | ACL 转换 | 本上下文命令 | 幂等键 |
|----------|----------|----------|----------|--------------|--------|
| `repair.parts.consumed` | PartsConsumed | RepairPartsConsumedListener | RepairAclTranslator | DeductInventoryCommand | event_id |

---

## 12. 与领域建模的映射

| 领域建模元素 | 实现元素 | 保证机制 / 备注 |
|--------------|----------|-----------------|
| SparePartLot 聚合根 | `domain.model.sparepartlot.SparePartLot` | `SparePartLotRepository` 保存 |
| `deduct(qty)` | `SparePartLot.deduct(...)` | 校验 INV-01；发出 `SparePartConsumed` |
| INV-02 同一 `(part_no, lot_no)` 唯一 | `uk_part_lot` | 数据库唯一索引 |
| InventoryChanged | `domain.event.InventoryChanged` + Outbox | topic `spare.inventory.changed` |
| 维修出库事件消费 | `RepairPartsConsumedListener` + `RepairAclTranslator` | event_id 幂等 |
````

---

## 10. 自检清单（提交前过一遍）

**前置与边界**
- [ ] 领域建模源文件存在，文档头部“实现依据”链接指向它。
- [ ] 文件落位在正确的 `领域模型/<服务>/实现设计/` 目录下，文件名为 `XX上下文.md`。
- [ ] 没有把其它限界上下文的实现顺手代写进来。
- [ ] §0 明确写了实现目标、核心聚合、事务与集成点。

**DDD 分层**
- [ ] 包结构包含 `interfaces / application / domain / infrastructure` 四层，职责清晰。
- [ ] 领域层未依赖 Spring / MyBatis / MyBatis-Plus / MQ / HTTP。
- [ ] 应用服务只编排事务、加载保存、幂等、Outbox，不写核心业务规则。
- [ ] Controller / Listener 不直接调 Mapper 改状态。

**聚合与对象模型**
- [ ] 每个聚合根有 Java 类骨架、行为方法、事件记录方式。
- [ ] 聚合根行为使用业务动词，无 `update/save/create/delete` 流水账。
- [ ] 实体不暴露独立 Repository，生命周期由聚合根控制。
- [ ] 值对象不可变，ID/状态/数量/编码等没有全部退化为裸 String/Long。

**持久化与一致性**
- [ ] Repository 以聚合根为单位，不是表 Mapper 的机械包装。
- [ ] MyBatis / MyBatis-Plus 使用边界有理由，并说明领域对象与 DO/PO、表之间的转换方式。
- [ ] 每条跨实例不变式有唯一索引、乐观锁、幂等表或其它明确机制。
- [ ] 并发控制策略明确到 version / lock / unique key / retry / reject。

**事件与集成**
- [ ] 领域事件名与领域建模逐字一致。
- [ ] 对外发布事件默认通过 Outbox；若直接 MQ，已说明理由。
- [ ] Outbox 字段、发布流程、失败重试或补偿策略清楚。
- [ ] 外部事件消费经 ACL 翻译，不直接把外部 DTO 传入领域层。
- [ ] 消费外部事件有幂等键与去重位置。

**读模型与测试**
- [ ] 查询场景放在 QueryService / 投影，不污染聚合写模型。
- [ ] 每条核心 INV 至少能落到一个领域单元测试或持久化约束测试。
- [ ] 状态机非法流转有失败用例。
- [ ] 消息重复投递、乱序/迟到（如适用）有测试或补偿说明。

**可追溯与修订**
- [ ] §12 映射表覆盖领域建模所有聚合、实体、值对象、领域服务、应用服务、领域事件、INV、读模型、对外契约、暂缓项。
- [ ] 已发布 API / topic / eventType / 表关键字段未做破坏性改名；若改名已在顶部变更说明中列出迁移方式。
- [ ] 领域建模中的热点或暂缓项未静默消失，已写入“暂缓与扩展点”。
