---
name: implementation
description: 在本仓库（factorybot-docs，PCBA + 整机组装混合型车间 MES）下，以某个限界上下文已落地的“领域建模”文档为依据，按 Java + Spring +《编码规范.md》八层分层（facade / facade-impl / controller / application / domain / repository / infrastructure / utility）与 Model/DTO/Info/VO/Query/Request/Converter/DO 命名体系，撰写或修订该限界上下文的“实现设计”文档，输出分层包结构、聚合代码落地、Repository/持久化、应用服务事务、领域事件/Outbox、API/ACL、读模型、测试策略与关键代码骨架。当用户要求“做 / 写 / 补 / 改 XX 限界上下文的代码实现设计 / 实现方案 / Java Spring 落地 / 从领域模型落代码”等任务时启用。
---

# 代码实现设计 Skill（Java + Spring + DDD，对齐《编码规范.md》）

本 skill 规定本仓库**实现设计（Implementation Design）类文档**的统一骨架、分层边界、代码映射规则与质量门槛。目标是把已落地的“领域建模”文档进一步翻译为可指导 Java + Spring 工程编码的设计说明，做到：

- **可追溯**：每个聚合根 / 实体 / 值对象 / 领域服务 / 应用服务 / 领域事件，都能追溯到领域建模文档。
- **可编码**：给出八层包结构、类职责、接口、事务边界、持久化映射、事件发布、测试切面与关键代码骨架。
- **守规范**：分层、POJO 后缀、Bean 命名、方法前缀、Converter 写法严格遵循本目录附属 [编码规范.md](编码规范.md)。
- **不过度设计**：吸收大厂 DDD 工程经验（事务 Outbox / CQRS 读写分离 / 防腐层），但按 MES 场景取舍，不把示例做成框架秀。
- **边界清晰**：领域层保持业务表达，Spring / MyBatis / Kafka / HTTP 等技术细节停留在 facade-impl / application / repository / infrastructure。

> **前置纪律（来自项目 CLAUDE.md）**：当用户指定要做某个模块时，**只读该模块自身文档与其直接对接的领域（domain），不把其它模块杂糅进来**。实现设计只承载本限界上下文的代码落地，不代写相邻限界上下文的实现。

> **术语约定（全文强制，与 [domain-modeling](../domain-modeling/SKILL.md) skill 一致）**：
> - **限界上下文（Bounded Context）**：实现设计的代码边界与服务内模块边界。正文中具体限界上下文统一写作“XX限界上下文”，**文件名**按仓库既有约定写作 `XX上下文.md`。
> - **领域模型**：指 `领域模型/<服务>/领域建模/<限界上下文>.md` 中已确认的聚合、实体、值对象、服务、事件与不变式。
> - **实现设计**：不是完整源码，不写大段可直接复制的业务代码；它说明“应该如何组织代码、哪些类负责什么、关键方法如何表达业务规则、哪些技术边界不能越界”。

> **命名与分层总纲（强制，源自《编码规范.md》）**：
> - **八层分层**：`facade` / `facade-impl` / `controller` / `application` / `domain` / `repository` / `infrastructure` / `utility`。
> - **POJO 后缀**：`DO`（数据库模型）/ `Model`（领域模型）/ `DTO`（对外传输）/ `Info`（内部传输）/ `VO`（值对象）/ `Query`（仓储查询参数）/ `Request`（facade 查询参数）/ `Converter`（转换器）。
> - **Bean 命名**：`Controller` / `Facade`(`FacadeImpl`) / `AppService` / `DomainService` / `Repository`(`RepositoryImpl`) / `Mapper`(`BaseMapper`) / `Client`(`ClientImpl`) / `FacadeClient`(`FacadeClientImpl`) / `Configuration`。
> - **方法前缀（Repository/Mapper/Client/DAO）**：`query` / `list` / `page` / `count` / `insert` / `delete` / `update`。
> - **Converter 写法**：逐字段手写，禁止 `BeanUtils.copyProperties` / `MapStruct`。

---

## 1. 何时使用本 skill

- 用户说“做 / 写 / 补 / 改 XX 限界上下文的实现设计 / 代码实现设计 / Java Spring 实现方案”。
- 用户给出一个限界上下文名称，需要把已落地的领域建模文档翻译为 Java + Spring 八层分层代码方案。
- 用户要求“基于 DDD 领域模型生成代码骨架 / 包结构 / Repository / AppService / DomainEvent / Outbox / ACL / 测试策略”。
- 用户要求判断某个限界上下文落代码时“哪些地方用 DDD、哪些地方不要过度设计”。

**强前置**：本 skill **必须以已存在的领域建模文档为实现依据**。若该限界上下文的领域建模尚不存在，**先停止**，提示用户先走 [domain-modeling](../domain-modeling/SKILL.md) skill 落地领域模型，再回来做实现设计——领域建模是源，实现设计是派生，跳过领域建模直接写实现会导致类、方法、事件、不变式与业务语义脱节。

不适用：
- 纯事件风暴 / 纯领域建模 → 使用对应 skill。
- 纯数据库建表脚本 → 只可作为实现设计的持久化章节补充，不单独用本 skill 生成散乱表结构。
- 纯 API 文档 → 只可作为 facade 层入口章节补充，不能替代领域实现设计。
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

> **路径提示**：本 skill 文件位于 `.claude/skills/implementation/`，指向 `领域模型/` 的相对路径需写作 `../../../领域模型/`（三层 `../` 回到仓库根）。本目录下的附属参考文件（如 [编码规范.md](编码规范.md)）以同目录相对路径引用。

---

## 3. 总体落地取舍（Java + Spring +《编码规范.md》边界）

实现设计必须体现 DDD 的工程落地，但不能堆砌概念。默认采用下列取舍。

### 3.1 默认架构风格（八层分层）

采用《编码规范.md》定义的“**以领域层为核心、依赖倒置**”的八层分层：

```text
facade         对外 SOA/RPC 接口 + DTO + Request（对外契约层）
facade-impl    FacadeImpl 实现 + Model↔DTO Converter（输入解析/校验/转换、输出序列化）
controller     HTTP 入口（可选，微服务一般由网关提供；只调 facade 声明的方法）
application    AppService + Info + 事务控制 + 只读查询仓储 + 领域事件触发监听 + 操作日志 + 安全认证 + 幂等
domain         Model(聚合根充血/实体可贫血) + VO值对象 + DomainService + DomainEvent + Repository接口(读写) + Query + Client接口(中间件ACL) + FacadeClient接口(RPC ACL) + Factory
repository     RepositoryImpl + Mapper(extends BaseMapper) + DO + Model↔DO Converter
infrastructure ClientImpl + FacadeClientImpl + Configuration + Outbox/Kafka/Redis/OSS/设备接口实现
utility        无业务语义工具类 + 全局 Exception
```

调用流向：`testsuites / main` → `controller` / `facade` → `facade-impl` → `application` → `domain`；`domain` 定义接口（Repository / Client / FacadeClient），由 `repository` / `infrastructure` 倒置实现（DIP）。`application` 虚线依赖 `repository` / `infrastructure`，但必须经 `domain` 声明的接口访问，不得直接使用 Mapper / ClientImpl 等实现。

**强制边界（对齐规范）**：
- `domain` 不依赖 Spring / MyBatis / Kafka / HTTP / Redis；只定义接口，由下层实现。
- `application` **只查询仓储，不写仓储**（写仓储是领域层的能力）；使用中间件须经 `domain` 声明的接口。
- `repository` 仅做数据持久化，`DO` 只存在于本层，经 Converter 转 `Model` 供上层。
- `infrastructure` 实现 `domain` 的 `Client` / `FacadeClient` 接口 + 中间件集成 + Outbox / Kafka。
- `utility` 无业务语义，被各层直接或间接依赖。
- `controller` 依赖 `facade-impl` 但**只能调用 `facade` 中声明的方法**，防止业务逻辑从 facade-impl 泄露到 controller。

### 3.2 聚合实现取舍（混合模式）

- **聚合根充血**：业务行为写成聚合根方法，方法内部校验不变式并记录领域事件；方法名用业务动词，禁止 `setXxx` / `updateXxx` 流水账。
- **聚合内子实体可贫血**：纯数据子实体可只含字段与必要不变式，生命周期由聚合根控制；不单独暴露 Repository。
- **值对象不可变**：Java `record` 或 final class，命名 `XXVO`；构造时完成格式与范围校验；不把业务含义退化为裸 `String` / `Long`。
- 不默认使用 Event Sourcing；MES 主链更强调可查询、可追溯、可纠错，默认采用**状态存储 + 领域事件 + Outbox**。
- 对实时性高、状态机复杂、审计要求强的对象，可在文档中建议“事件日志/操作流水表”作为补充，但不要把整个上下文默认改成事件溯源。

### 3.3 持久化取舍

本仓库实现设计**仅使用 MyBatis / MyBatis-Plus** 作为持久化方案，不使用 Spring Data JPA / Hibernate。取舍理由：MES 场景通常存在复杂追溯查询、报表投影、历史数据兼容、SQL 可控性与性能调优要求；MyBatis 更适合把写模型聚合重建与读模型 SQL 投影分开处理。

| 选择 | 适用场景 | 约束 |
|------|----------|------|
| MyBatis | 复杂 SQL、聚合装载需要手写 ResultMap、多表/子表装配、追溯查询性能敏感 | 领域对象与 DO 分离；Mapper 只做数据访问；RepositoryImpl 负责装配聚合 |
| MyBatis-Plus | 单表 CRUD、字典/配置/Outbox/幂等表等基础设施表、简单读模型 | 禁止把 ServiceImpl 当应用服务；禁止绕过聚合直接改写核心状态 |

默认建议：**写模型 Repository 用聚合语义，读模型 Query 用 SQL/投影语义**。不要为了“纯 DDD”把所有查询都塞进聚合，也不要为了“开发快”让 Controller 直接调 Mapper 改状态。

**MyBatis 落地边界（对齐规范）**：
- `Mapper` 是仓储层数据访问接口，不是领域 Repository；每个 `XXMapper` 继承其基类 `XXBaseMapper`，基类中定义基本增删改查方法。
- `DO` 是数据库行对象（表字段平铺），只存在于仓储层，不是领域对象。
- `Model2DOConverter` / `DO2ModelConverter` 负责 `DO ↔ Model` 转换，放在仓储层；**逐字段手写，禁止 BeanUtils.copyProperties / MapStruct**（理由：使用此类工具后字段变更无法在编译期感知，易致生产事故）。
- 写侧保存以聚合根为单位；聚合内子实体可拆表，但由同一个 `RepositoryImpl` 统一装配保存。
- 读侧可以直接返回投影 / DTO，不强制还原聚合。

### 3.4 事件与一致性取舍

- 聚合内强一致：单事务内完成。
- 跨聚合协作：领域服务 + 应用服务编排；跨上下文协作通过集成事件 / Saga / 流程编排。
- 对外事件发布：默认采用**事务 Outbox + Kafka**，避免“数据库提交成功但 Kafka 发送失败”。
- 消息队列技术选型强制使用 **Kafka**；实现设计文档中不得写成可替换的 `<MQ>`，也不得引入 RabbitMQ / RocketMQ 等其它消息队列作为默认方案。
- 消费外部事件：必须有幂等键、去重表或消费日志；通过 ACL（`FacadeClient` / `Client`）翻译为本上下文命令或值对象。
- 不默认做分布式事务；MES 现场系统以可恢复、可补偿、可追溯为优先。

### 3.5 写仓储归属（关键规则）

依《编码规范.md》“应用层只能查询仓储，不能写仓储，写仓储是领域层的能力”，结合本 skill 的混合实体模式，写仓储调用按下述归属：

- **`ApplicationService`**：开启事务（`@Transactional`）、幂等校验、安全认证、操作日志、**只读查询仓储加载聚合**、调用 `DomainService`、收集领域事件并通过 `domain` 声明的事件发布接口写 Outbox。**不直接调用 Repository 的 insert/update/delete**。
- **`DomainService`**：承载跨聚合 / 不自然归属单聚合的业务规则，**负责加载聚合、调用聚合行为、调用 Repository 写方法（insert/update/delete）持久化**，并返回产生的领域事件给应用层。
- **`AggregateRoot`（充血）**：业务行为方法、INV 校验、状态流转、`registerEvent`。

> 即：事务边界在应用层，持久化动作在领域服务。应用层不触碰 Repository 写方法。

---

## 4. 从领域建模到代码实现的映射规则（核心）

实现设计不是重新设计业务，而是把领域建模里的元素**逐一对位**到 Java + Spring 八层工程结构。下表是**强制对齐表**，设计时逐行落实，不得遗漏：

| 领域建模元素（源） | 实现设计对应（产物，规范命名） | 落实要点 |
|--------------------|-------------------------------|----------|
| 聚合根 | `domain.model.<aggregate>.<XXModel>`（充血） | 聚合根类、业务行为方法、领域事件记录、Repository 接口 |
| 实体 | 聚合包内 `<XXModel>`（可贫血） | 不提供独立 Repository；生命周期受聚合根控制 |
| 值对象 | `domain.model.<aggregate>.<XXVO>` | 不可变 record / final class；构造时校验；禁止裸 String 满天飞 |
| 聚合根行为 | `<XXModel>.<业务动词>(...)` | 方法名与领域建模一致；校验 INV；状态变更；registerEvent |
| 领域服务 | `domain.service.<XXDomainService>` | 跨聚合/不归属单聚合的规则；加载聚合、调用行为、写仓储、返回事件；不持有 `@Transactional` |
| 应用服务 | `application.service.<XXAppService>`（可细分 `XXWriteAppService` / `XXReadAppService`） | `@Transactional`；只读加载；调 DomainService；收集事件写 Outbox；幂等/安全/日志；**不写仓储** |
| 用例入参（facade 对外） | `facade.<XXRequest>` / `facade.<XXDTO>` | Request 为 facade 查询参数（≥3 参数封装）；DTO 为对外传输对象 |
| 用例入参（application↔domain 内部） | `application.<XXInfo>` | Info 对内传输，与 DTO 功能类似但 DTO 对外、Info 对内 |
| 仓储接口 | `domain.repository.<XXRepository>` + `domain.repository.<XXQuery>` | 读写方法均定义在领域层；Query 封装查询参数（≥3 参数封装） |
| 仓储实现 | `repository.<XXRepositoryImpl>` | 倒置依赖实现；装配聚合；调 Mapper |
| 数据库模型 | `repository.<XXDO>` | 表字段平铺；只存在于仓储层 |
| Mapper | `repository.<XXMapper> extends <XXBaseMapper>` | 最底层数据库操作；只允许仓储层调用 |
| 中间件依赖（ACL） | `domain.acl.<XXClient>` → `infrastructure.<XXClientImpl>` | 防腐设计：接口在领域层，实现在基础设施层 |
| RPC 服务依赖（ACL） | `domain.acl.<XXFacadeClient>` → `infrastructure.<XXFacadeClientImpl>` | 防腐设计：消费外部 RPC 服务 |
| 领域事件 | `domain.event.<XXEvent>` | 聚合内 registerEvent；DomainService 返回；应用层收集写 Outbox；事件名与领域建模逐字一致 |
| 对外契约主题 | Outbox / EventPublisher 配置 | topic、eventType、主消费方照搬领域建模 §对外契约 |
| 转换器 | `XXAA2BBConverter`（逐字段手写） | `Model2DOConverter`/`DO2ModelConverter`(repository)；`Model2DTOConverter`/`DTO2ModelConverter`(facade-impl)；`Model2InfoConverter`/`Info2ModelConverter`(application) |
| 配置 | `<XXConfiguration>` | 各层各自配置：DataSourceConfiguration→repository；WebConfiguration→controller 等 |
| 不变规则 INV | 聚合方法 / DomainService / DB 约束 / 幂等表 / Saga | 每条 INV 说明由哪一层保证，不能只写“代码校验” |
| 上下文外事件消费 | Kafka Listener + ACL(`FacadeClient`/`Client`) + AppService | 外部事件不得直接进领域；先翻译成本上下文命令/值对象 |
| 热点 / 暂缓项 | 实现风险 / 暂缓设计 | 不确定点不得静默消失；列出推荐取舍与后续扩展点 |

> **可追溯性硬约束**：实现设计文档 §12“与领域建模的映射”必须给出一张**领域模型元素 → 实现元素**的对位表，让读者能从任何一个聚合、方法、事件、INV 查到它将落到哪个包、哪个类、哪个方法、哪种技术机制。这是本 skill 的验收门槛之一。

> **反向一致性**：若实现设计过程中发现领域建模有遗漏（如某命令没有应用服务入口、某领域事件无发出者、某 INV 无保证机制），**先回去补领域建模**，再回来改实现设计——不要在实现设计里私自补业务概念。

---

## 5. 方法命名规约（对齐规范【建议】）

`Service` / `Repository` / `Client` / `DAO`(`Mapper`) 方法命名统一前缀：

| 前缀 | 语义 | 示例 |
|------|------|------|
| `query` | 获取单个对象 | `queryById` / `queryByPartNo` |
| `list` | 获取多个对象列表（复数结尾） | `listAvailableLots` |
| `page` | 获取多个对象分页（复数结尾） | `pageSpareParts` |
| `count` | 获取统计值 | `countByState` |
| `insert` | 插入 | `insertDO` |
| `delete` | 删除 | `deleteById` |
| `update` | 修改 | `updateState` |

> **例外**：**聚合根行为方法不套用上述前缀**，仍使用业务动词（如 `accept` / `deduct` / `commission` / `suspend`）。这是 CLAUDE.md“严禁面向过程流水账代码”的硬性约束——CRUD 前缀只用于数据访问层，不用于表达业务行为。

---

## 6. 标准章节骨架（按顺序）

下面是一篇合格实现设计文档的章节模板。**章节标题与编号必须保持一致**，便于不同限界上下文横向比较。

> **编号说明**：以下 `## 0` ~ `## 12` 是**目标文档**的章节结构，不是本 skill 自身的章节编号。

````markdown
# <XX>上下文 — 实现设计（Java + Spring，对齐《编码规范.md》）

> **限界上下文**：<XX>
> **所属服务**：<制造资源服务 / 生产执行服务 / 设备管理服务>
> **实现依据**：[领域建模 — <XX>上下文](../领域建模/<XX>上下文.md)
> **技术栈取舍**：Java 17+ / Spring Boot 3.x / MyBatis 或 MyBatis-Plus / Outbox / Kafka

---

## 0. 实现总览

（一段话讲清本上下文代码实现目标、核心聚合、关键事务、关键集成点。）

### 分层边界

```text
facade / controller  ->  facade-impl  ->  application  ->  domain
                                              |              ^
                                              v              |
                                        repository / infrastructure
```

**本实现坚持**：
- 领域层不依赖 Spring / MyBatis / Kafka。
- 应用服务负责编排与事务、只读加载、事件收集与 Outbox，不写仓储，不写业务规则。
- 领域服务负责跨聚合规则、加载聚合、调用行为、写仓储持久化。
- 仓储层 / 基础设施层实现 Repository、Outbox、ACL、Query。

---

## 1. 模块与包结构

```text
com.factorybot.<service>.<context>
├── facade            对外 SOA/RPC 接口：XXFacade / XXWriteFacade / XXReadFacade + XXDTO + XXRequest
├── facade-impl       FacadeImpl + Model↔DTO Converter
├── controller        XXController（HTTP 入口，可选）
├── application
│   ├── service       XXAppService（XXWriteAppService / XXReadAppService）
│   ├── info          XXInfo（application↔domain 传输）
│   └── event         领域事件监听 / Outbox 写入
├── domain
│   ├── model.<aggregate>   XXModel（聚合根充血 / 子实体可贫血） + XXVO + Factory
│   ├── service             XXDomainService
│   ├── event               XXEvent
│   ├── repository          XXRepository 接口 + XXQuery
│   └── acl                 XXClient 接口 + XXFacadeClient 接口
├── repository        XXRepositoryImpl + XXMapper + XXBaseMapper + XXDO + Model2DOConverter/DO2ModelConverter
├── infrastructure    XXClientImpl + XXFacadeClientImpl + Configuration + Outbox/Kafka/Redis/OSS/设备接口
└── utility           Utils + 全局 Exception
```

> `facade` / `facade-impl` 可作为独立 Maven 模块对外发布；一个应用可按用途设计多个 facade 模块，由一个或多个 facade-impl 实现。

| 层 | 职责 | 禁止事项 |
|----|------|----------|
| `facade` | 对外契约：Facade 接口、DTO、Request | 禁止写业务实现 |
| `facade-impl` | FacadeImpl、输入解析/校验/转换、输出序列化 | 禁止承载 INV 业务判断；禁止直接调 Mapper |
| `controller` | HTTP 入口、协议适配 | 只能调 facade 声明的方法；禁止写业务规则 |
| `application` | 用例编排、事务、幂等、只读加载、事件/Outbox、安全、操作日志 | 禁止写仓储；禁止承载 INV 业务判断 |
| `domain` | Model、VO、DomainService、DomainEvent、Repository/Client/FacadeClient 接口、Query、Factory | 禁止依赖 Spring / MyBatis / MQ |
| `repository` | RepositoryImpl、Mapper、DO、Model↔DO Converter | 禁止反向定义业务语义；DO 不得上抛领域层 |
| `infrastructure` | ClientImpl、FacadeClientImpl、Configuration、Outbox、Kafka、Redis | 禁止承载业务规则 |
| `utility` | 无业务语义工具类、全局 Exception | 必须无业务语义 |

---

## 2. 聚合代码落地

每个聚合根按下列子结构写：

### 2.N <XXModel>（<中文名>）

**来源**：领域建模 §1.N  
**一致性边界**：照搬领域建模，补充代码层如何保证。  
**实现形态**：`domain.model.<aggregate>.<XXModel>`（充血）

```java
public class <XXModel> {
    private <XXVO> id;          // 值对象，不可变
    private <StateVO> state;
    // 子实体可贫血：仅字段 + 必要不变式

    public void doSomething(...) {
        // 1. guard invariant
        // 2. change state
        // 3. registerEvent(new <XX>Event(...))
    }
}
```

**行为映射**：
| 领域行为 | Java 方法 | 校验 INV | 发出事件 | 备注 |
|----------|-----------|----------|----------|------|
| `doSomething(args)` | `<XXModel>.doSomething(...)` | INV-XX | `<XX>Event` | ... |

**实现注意**：
- 聚合根方法使用业务动词，禁止 `setXxx` / `updateXxx` 流水账。
- 聚合内部实体只能由聚合根创建或变更。
- 聚合根只保存其它聚合 ID，不持有其它聚合对象引用。
- 子实体可贫血，但状态变更入口必须经聚合根方法。

---

## 3. 值对象与枚举实现

| 值对象 | Java 类型 | 不变校验 | 持久化方式 | 备注 |
|--------|-----------|----------|------------|------|
| <XX>Id | `record <XX>IdVO(String value)` | 非空 / 格式 | varchar | 聚合根 ID |
| <XX>State | `enum <XX>StateVO` | 状态机由聚合方法控制 | varchar | 禁止外部直接改状态 |

**值对象原则**：
- 优先不可变：`record` / final class，命名 `XXVO`。
- 构造时完成格式与范围校验。
- 不把业务含义退化为裸 `String` / `Long` 在各层传递。

---

## 4. Repository 与持久化设计

### 4.1 Repository 接口（domain 层）

```java
public interface <XXModel>Repository {
    Optional<<XXModel>> queryById(<XXIdVO> id);          // 只读
    List<<XXModel>> listByXxx(<XXQuery> query);          // 只读
    void insert(<XXModel> aggregate);                    // 写：由 DomainService 调用
    void update(<XXModel> aggregate);                    // 写：由 DomainService 调用
}
```

> 写方法（insert/update/delete）由 `DomainService` 调用，不由 `ApplicationService` 调用。

### 4.2 持久化实现（repository 层）

**技术选择**：<MyBatis / MyBatis-Plus>  
**选择理由**：...（复杂聚合装载优先 MyBatis；简单基础设施表或简单读模型可用 MyBatis-Plus）

| 领域对象 | DO / 表 | Mapper | 转换器 | 说明 |
|----------|---------|--------|--------|------|
| `<XXModel>` | `<XX>DO` / `<table_name>` | `<XX>Mapper extends <XX>BaseMapper` | `<XX>Model2DOConverter` / `<XX>DO2ModelConverter` | 聚合根主表 |
| 子实体 | `<Child>DO` / `<child_table>` | `<Child>Mapper` | 同上 | 聚合内子表，由 RepositoryImpl 统一装配 |

**Converter 写法**：逐字段手写，禁止 `BeanUtils.copyProperties` / `MapStruct`。

**数据库约束对应 INV**：
| 约束 | 对应 INV | 实现机制 |
|------|----------|----------|
| uk_xxx | INV-XX | 唯一索引 |
| version | INV-XX | 乐观锁 |

---

## 5. 应用服务与事务边界

每个命令用例按下列结构写：

### 5.N <UseCase>AppService

```java
@Service
public class <UseCase>WriteAppService {
    @Transactional
    public <Result> handle(<XX>Info info) {
        // 1. 幂等校验（commandId / requestId）
        // 2. 安全认证、操作日志埋点
        // 3. 调用 DomainService（其内部：只读加载聚合 -> 调用聚合行为 -> 写仓储持久化 -> 返回领域事件）
        List<<XX>Event> events = domainService.process(info);
        // 4. 收集领域事件，经 domain 声明的事件发布接口写 Outbox
        outboxWriter.write(events);
    }
}
```

| 用例 | Info / Request | 事务边界 | 调用领域对象 | 产出事件 | 幂等策略 |
|------|----------------|----------|--------------|----------|----------|
| ... | `<XX>Info` | 单聚合事务 / 跨聚合编排 | `XXDomainService` | ... | ... |

**边界要求**：
- `@Transactional` 放在应用服务，不放领域对象。
- 应用服务**只读查询仓储 + 调 DomainService**，不直接调 Repository 写方法。
- 应用服务不写 INV 业务规则；需要跨聚合读取/判断时，交给领域服务表达业务语义。

---

## 6. 领域服务实现

| 领域服务 | Java 类 | 使用场景 | 依赖端口 | 为什么不在聚合根内 |
|----------|---------|----------|----------|--------------------|
| `XXDomainService` | `domain.service.XXDomainService` | ... | Repository 接口 / Client 接口 | 跨聚合 / 不自然归属单聚合 |

```java
public class <XX>DomainService {
    public List<<XX>Event> process(<XX>Info info) {
        // 1. 只读加载聚合（queryXxx）
        // 2. 调用聚合行为（业务规则在此表达）
        // 3. 写仓储持久化（insert / update）
        // 4. 返回产生的领域事件
    }
}
```

> 领域服务可以依赖 Repository 接口或其它领域端口（Client / FacadeClient），但不直接依赖 Mapper、Kafka Client、REST Client 实现。**写仓储调用发生在领域服务**。

---

## 7. 领域事件、Outbox 与消息发布

### 7.1 领域事件类

| 领域事件 | Java 类 | 发出者 | 关键字段 | 对外发布 |
|----------|---------|--------|----------|----------|
| `<XX>Event` | `domain.event.<XX>Event` | `<XXModel>` / `XXDomainService` | ... | 是 / 否 |

### 7.2 Outbox 设计

```text
聚合根 registerEvent
        ↓
DomainService 调用聚合行为 + 写仓储，返回事件
        ↓
应用服务收集事件，同事务写 outbox_event
        ↓
OutboxPublisher 异步发布 Kafka
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

| 接口 | Facade / Controller | 入参 (Request/DTO) | Info | AppService |
|------|---------------------|--------------------|-----|------------|
| POST /xxx | `XXController` → `XXFacade` | `XXRequest` / `XXDTO` | `XXInfo` | `XXWriteAppService` |

> Controller 只调 facade 声明的方法；FacadeImpl 完成 DTO↔Info/Model 转换后调 AppService。

### 8.2 Kafka 事件消费入口

| 订阅主题 | 外部事件 | Listener | ACL 转换（FacadeClient/Client） | 本上下文命令/Info | 幂等键 |
|----------|----------|----------|---------------------------------|-------------------|--------|
| ... | ... | `XXListener` | `XXFacadeClient` + Converter | `XXInfo` | event_id |

**ACL 原则**：
- 外部事件 / DTO 不直接进入领域层。
- `FacadeClient` / `Client` 接口定义在领域层，实现在基础设施层；ACL 把外部模型翻译为本上下文统一语言。
- 消费外部事件必须记录幂等，避免设备重复上报、Kafka 重投导致重复过账。

---

## 9. 查询模型与读侧设计

| 查询场景 | ReadAppService / QueryService | 数据来源 | 是否读模型投影 | 说明 |
|----------|-------------------------------|----------|----------------|------|
| ... | `XXReadAppService` | SQL / projection table | 是 / 否 | ... |

**读写取舍**：
- 写侧围绕聚合根保护不变式（DomainService 写仓储）。
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
| 领域单元测试 | `XXModel` / `XXVO` / `XXDomainService` | INV、状态机、事件发出、写仓储调用 |
| 应用服务测试 | `XXAppService` | 事务编排、只读加载、DomainService 调用、Outbox 写入、不触达 Repository 写方法 |
| 持久化测试 | `XXRepositoryImpl` / Mapper | DO↔Model 映射、唯一索引、乐观锁、Converter 逐字段 |
| 消息测试 | Listener / OutboxPublisher | 幂等、重投、ACL 转换 |
| 契约测试 | 发布事件 / Facade API | topic、payload、DTO 兼容性 |

每条核心 INV 至少对应一个失败用例；每个状态机终态至少有一个“拒绝非法命令”的测试。

---

## 12. 与领域建模的映射

| 领域建模元素 | 实现元素（规范命名） | 保证机制 / 备注 |
|--------------|----------------------|-----------------|
| 聚合根 XX | `domain.model.xxx.<XX>Model` | Repository: `XXRepository` / `XXRepositoryImpl` |
| 行为 `doSomething()` | `<XX>Model.doSomething()` | 校验 INV-XX，发出 `<XX>Event` |
| 领域事件 `<XX>Event` | `domain.event.<XX>Event` + Outbox topic | topic: `svc.xxx.<event>` |
| INV-XX | 聚合方法 / 唯一索引 / 幂等表 | ... |
| 领域服务 XXService | `domain.service.<XX>DomainService` | 写仓储在此层 |
| 查询读模型 XXView | `application.service.<XX>ReadAppService` | ... |
| 外部 RPC 依赖 | `domain.acl.<XX>FacadeClient` → `infrastructure.<XX>FacadeClientImpl` | ... |
| 中间件依赖 | `domain.acl.<XX>Client` → `infrastructure.<XX>ClientImpl` | ... |

---

## 暂缓与扩展点（可选）

按当前阶段不展开的实现能力，留待后续，并指明不影响当前实现的原因：
- 例如：暂不引入 Event Sourcing，仅保留 Outbox + 操作流水；未来如审计要求提升，可从 `domain_event_log` 扩展。
- 例如：暂不引入 CQRS 独立读库，先用同库投影表；后续追溯查询压力上来再拆 Elasticsearch / ClickHouse。
````

---

## 7. 写作规范（关键）

1. **以领域建模为源，不重新发明业务**：聚合、行为、事件、INV 名称必须与领域建模对齐；要改业务名，先改领域建模。
2. **面向对象，不是面向表脚本**：聚合根行为是方法，不是 `updateStatus/save/delete` 流水账。表结构是持久化结果，不是领域模型本身。
3. **领域层不依赖框架**：领域对象禁止 `@TableName`、`@TableId`、`@Service`、`@Autowired`、`@Transactional`、`@KafkaListener` 等框架注解；MyBatis / MyBatis-Plus 注解只允许出现在仓储层 DO 或 Mapper 上。
4. **应用层只编排、不写仓储**：应用服务可以校验入参、事务、权限、幂等、只读加载、写 Outbox；业务判断下沉聚合根或领域服务；**写仓储由领域服务负责**。
5. **Repository 是聚合语义，不是 Mapper 包装**：写侧 Repository 以聚合根为单位；复杂查询不要硬塞进 Repository，放 ReadAppService / QueryService。
6. **POJO 后缀严格按规范**：`DO`（数据库）/ `Model`（领域）/ `DTO`（对外）/ `Info`（对内）/ `VO`（值对象）/ `Query`（仓储查询）/ `Request`（facade 查询）/ `Converter`（转换器）。不得自创 Command / ViewObject / PO 等后缀。
7. **Bean 命名严格按规范**：`Controller` / `Facade`(`Impl`) / `AppService`(`Write`/`Read`) / `DomainService` / `Repository`(`Impl`) / `Mapper`(`BaseMapper`) / `Client`(`Impl`) / `FacadeClient`(`Impl`) / `Configuration`。
8. **值对象不可变且有语义**：ID、编码、数量、状态、工位、设备号等不要全用裸 String/Long；用 `XXVO` 承载校验与语言。
9. **领域事件在聚合行为中产生**：状态改变的方法必须记录领域事件；DomainService 返回事件，应用服务收集并写 Outbox，不在 Controller / FacadeImpl 里拼事件。
10. **消息队列统一使用 Kafka**：实现设计中的事件发布、事件消费、Outbox Publisher、Listener 均按 Kafka 设计；不得把消息队列写成 `<MQ>` 占位，也不得默认引入 RabbitMQ / RocketMQ 等其它消息队列。
11. **Outbox 默认优先于直接发 Kafka**：只要事件与数据库状态必须一致，默认使用事务 Outbox；直接发 Kafka 需要说明为什么可接受。
12. **跨上下文只走 ACL**：外部 DTO / Event / RPC 不进入 domain；经 `FacadeClient` / `Client` 翻译成本上下文命令、值对象或领域端口结果。
12. **Converter 逐字段手写**：禁止 `BeanUtils.copyProperties` / `MapStruct`；字段变更须在编译期可见。
13. **方法前缀按规范**：Repository / Mapper / Client / DAO 用 `query/list/page/count/insert/delete/update`；聚合根行为用业务动词。
14. **幂等是一等设计**：MES 场景的扫码、设备数据、Kafka 消费、ERP 回调都可能重复；必须说明幂等键与去重位置。
15. **并发控制要落到机制**：只写“保证并发安全”不合格；要说明乐观锁 version、唯一索引、悲观锁、串行队列或业务锁。
16. **不变式必须能定位实现层**：每条 INV 都要对应聚合方法、领域服务、数据库约束、幂等表、Saga 或补偿任务之一。
17. **接口 DTO 不等于领域对象**：Request/DTO/Info 可贫血，领域 Model 不可贫血（聚合根充血）；Converter 职责要明确。
18. **大厂方案要取舍，不照搬**：可借鉴 COLA/六边形/整洁架构思想，但分层以规范八层为准，不制造无意义抽象。
19. **ASCII 图保持单一方向**：分层、事件流、Outbox 流程图统一从上到下或从左到右。

---

## 8. 常见反模式（拒绝写法）

| 反模式 | 为什么不行 | 改怎么写 |
|--------|------------|----------|
| 没有领域建模就直接写实现 | 类与方法没有业务来源，容易退化成 CRUD | 先走 domain-modeling skill |
| `ApplicationService` 直接调 Repository.insert/update | 违反“应用层不写仓储” | 写仓储下沉 `DomainService`，应用层只读加载 + 调 DomainService |
| Controller / FacadeImpl 直接调 Mapper 改状态 | 绕过应用服务、领域服务与聚合不变式 | Controller → Facade → FacadeImpl → AppService → DomainService → Aggregate |
| AppService 里堆 if/else 业务规则 | 应用层变成过程脚本，领域贫血 | 规则下沉聚合根或领域服务 |
| 聚合根只有 getter/setter（纯贫血） | 没有行为，无法保护不变式 | 聚合根充血，用业务方法表达命令和状态流转 |
| 用 `BeanUtils.copyProperties` / `MapStruct` 转换 | 字段变更编译期不可见，易致生产事故 | Converter 逐字段手写 |
| 自创 Command / PO / ViewObject 后缀 | 命名体系混乱，跨模块不可读 | 严格用 DO/Model/DTO/Info/VO/Query/Request/Converter |
| Repository 为每张表建一套 | 破坏聚合边界，实体被外部随意修改 | Repository 以聚合根为单位 |
| 为所有对象都建 DomainService | 领域服务滥用，模型失去内聚 | 单聚合规则放聚合根，跨聚合才建领域服务 |
| 直接把外部事件 / DTO 传进领域方法 | 上下文边界塌陷 | Listener → FacadeClient/Client + Converter → Info/VO |
| 事务内直接发 Kafka | DB 成功 Kafka 失败会丢事件 | 同事务写 Outbox，异步发布 Kafka |
| 所有查询都加载聚合再拼 DTO | 性能差，读写职责混淆 | ReadAppService + 投影 / SQL |
| 只写“使用乐观锁”但不说明 version | 无法编码与测试 | 写清 version 字段、冲突异常、重试/拒绝策略 |
| INV 只出现在文档，不映射代码 | 不可验证，设计无法落地 | 在 §12 映射到类/方法/索引/幂等表 |
| 把分层照搬成十几层或随意合并 | 抽象过度或边界模糊 | 维持规范八层即可 |
| 一篇实现设计覆盖多个限界上下文 | 代码边界塌陷，后续无法微服务化 | 每个限界上下文一篇，通过 ACL / 事件契约协作 |

---

## 9. 撰写流程（操作步骤）

### 9.A 新建文档

1. **确认前置**：确认领域建模源文件存在；不存在则停止，提示用户先做领域建模。
2. **确认归属**：根据 [领域总览](../../../领域模型/领域总览.md) 与领域建模路径，确定服务与目标路径 `领域模型/<服务>/实现设计/XX上下文.md`。
3. **通读领域建模**：完整抽出聚合根、实体、值对象、领域服务、应用服务、领域事件、INV、对外契约、读模型、暂缓项。
4. **确定工程取舍**：说明 Java/Spring 基线、八层分层、MyBatis / MyBatis-Plus 使用边界、事件方案（Outbox）、读模型策略、是否需要 ACL（FacadeClient / Client）。
5. **设计包结构**：按八层给出包名与职责，明确禁止事项。
6. **落聚合代码**：每个聚合根给 `XXModel` 充血骨架、行为方法映射、事件记录方式、子实体（可贫血）与 `XXVO` 实现方式。
7. **落 Repository**：定义 `XXRepository` 接口 + `XXQuery`（domain）；说明 `XXRepositoryImpl`、`XXMapper`/`XXBaseMapper`、`XXDO`、`Model2DOConverter`/`DO2ModelConverter`（repository）、唯一索引、乐观锁。
8. **落应用服务**：把领域建模中的应用服务和命令逐个映射成 `XXInfo` + `XXAppService` 方法，标事务边界、幂等策略、Outbox 写入点；**不写仓储**。
9. **落领域服务**：为每个领域服务写 `XXDomainService` 类、依赖端口、加载/调用/写仓储/返回事件，并回答“为什么不放聚合根内”。
10. **落事件机制**：`XXEvent` 类、Outbox 表、发布流程、topic 映射、失败重试与补偿。
11. **落接口与 ACL**：facade/controller 入口、DTO/Request/Info 转换、`FacadeClient`/`Client` 外部事件翻译、幂等键。
12. **落读模型**：`XXReadAppService`、投影表/SQL、与写模型的同步方式。
13. **落异常并发**：重复命令、重复事件、并发状态变更、乱序/迟到事件的处理。
14. **落测试策略**：每条 INV 对应测试层级；状态机非法边必须有失败用例。
15. **补映射表**：§12 把领域建模所有元素逐行对位到实现元素（规范命名），这是验收门槛。
16. **diff 自检**：用 §11 自检清单过一遍。

### 9.B 修订既有文档

1. **先读旧实现设计 + 最新领域建模**：标出已发布的包名、类名、事件类名、topic、表名、INV 映射。
2. **界定改动范围**：只动用户指定限界上下文；相邻上下文即使“顺手”也不代写。
3. **保留契约稳定性**：已对外发布的 Facade API、topic、eventType、表关键字段尽量不改。若必须改，顶部加**变更说明**（旧名 → 新名、影响下游、迁移方式）。
4. **实现映射随领域模型同步**：领域建模新增/删除/改名的聚合、事件、INV，必须同步到 §12。
5. **不变式编号延续**：沿用领域建模 INV 编号；废弃项不删映射，标“已废止（原因）”。
6. **技术取舍变更要说明成本**：如 MyBatis 与 MyBatis-Plus 使用边界调整、直接 MQ 改 Outbox、同库读模型改独立读库，要说明迁移影响。
7. **diff 自检**：重点确认领域层未被框架污染、应用层未堆业务且未写仓储、写仓储在领域服务、事件发布仍有 Outbox/幂等保障、Converter 逐字段手写。

---

## 10. 最小 Demo 片段（参考骨架）

下方是一段缩水版“备件管理限界上下文”实现设计片段，演示八层包结构、聚合代码、Repository、应用服务、领域服务、Outbox、ACL 与映射表的形态。**真实文档每节都要更完整**。备件管理限界上下文为示意，仓库中尚无该文件。

````markdown
# 备件管理上下文 — 实现设计（Java + Spring，对齐《编码规范.md》）

> **限界上下文**：备件管理
> **所属服务**：制造资源服务
> **实现依据**：[领域建模 — 备件管理上下文](../领域建模/备件管理上下文.md)
> **技术栈取舍**：Java 17 / Spring Boot 3.x / MyBatis / Outbox / Kafka

---

## 0. 实现总览

备件管理限界上下文以 `SparePartLotModel` 聚合保护单批次库存不变式，以 `ReorderRuleModel` 聚合维护再订货规则。聚合根充血承载状态流转与事件记录；`SparePartDomainService` 负责加载聚合、调用行为、写仓储持久化；`DeductInventoryWriteAppService` 负责事务、幂等、只读加载、Outbox 发布。读侧通过库存投影表支撑维修领料查询。

---

## 1. 模块与包结构

```text
com.factorybot.resource.spareparts
├── facade            SparePartFacade / SparePartLotDTO / DeductInventoryRequest
├── facade-impl       SparePartFacadeImpl + Model2DTOConverter/DTO2ModelConverter
├── application
│   ├── service       DeductInventoryWriteAppService / InventoryReadAppService
│   ├── info          DeductInventoryInfo
│   └── event         Outbox 事件写入
├── domain
│   ├── model.sparepartlot   SparePartLotModel（充血）+ LotIdVO/LotStateVO + Factory
│   ├── model.reorderrule    ReorderRuleModel
│   ├── service             SparePartDomainService
│   ├── event               LotAcceptedEvent / SparePartConsumedEvent / InventoryChangedEvent
│   ├── repository          SparePartLotRepository 接口 + SparePartLotQuery
│   └── acl                 RepairFacadeClient 接口
├── repository        SparePartLotRepositoryImpl + SparePartLotMapper + SparePartLotBaseMapper
│                     + SparePartLotDO + Model2DOConverter/DO2ModelConverter
├── infrastructure    RepairFacadeClientImpl + Outbox/MQ/Redis + Configuration
└── utility           SparePartException
```

---

## 2. 聚合代码落地

### 2.1 SparePartLotModel（备件批次，充血）

```java
public class SparePartLotModel {
    private LotIdVO id;
    private PartNoVO partNo;
    private LotNoVO lotNo;
    private QuantityVO quantity;
    private LotStateVO state;

    public void accept() {
        requireState(LotStateVO.INCOMING);
        this.state = LotStateVO.IN_STOCK;
        registerEvent(new LotAcceptedEvent(id, partNo, lotNo));
    }

    public void deduct(QuantityVO qty, ConsumptionSourceVO source) {
        requireState(LotStateVO.IN_STOCK);
        this.quantity = this.quantity.subtract(qty);
        if (this.quantity.isZero()) {
            this.state = LotStateVO.DEPLETED;
        }
        registerEvent(new SparePartConsumedEvent(id, qty, source));
        registerEvent(new InventoryChangedEvent(partNo, this.quantity));
    }
}
```

| 领域行为 | Java 方法 | 校验 INV | 发出事件 |
|----------|-----------|----------|----------|
| `accept()` | `SparePartLotModel.accept()` | INV-03 | `LotAcceptedEvent` |
| `deduct(qty)` | `SparePartLotModel.deduct(...)` | INV-01, INV-04 | `SparePartConsumedEvent`, `InventoryChangedEvent` |

---

## 4. Repository 与持久化设计

```java
// domain.repository（接口，读写均定义在领域层）
public interface SparePartLotRepository {
    Optional<SparePartLotModel> queryById(LotIdVO id);                         // 只读
    Optional<SparePartLotModel> queryAvailableByPartNo(PartNoVO partNo);       // 只读
    List<SparePartLotModel> listByQuery(SparePartLotQuery query);              // 只读
    void update(SparePartLotModel lot);                                        // 写：由 DomainService 调用
}
```

| 领域对象 | DO / 表 | Mapper | 转换器 | 约束 |
|----------|---------|--------|--------|------|
| `SparePartLotModel` | `SparePartLotDO` / `spare_part_lot` | `SparePartLotMapper extends SparePartLotBaseMapper` | `SparePartLotModel2DOConverter` / `SparePartLotDO2ModelConverter`（逐字段手写） | `uk_part_lot(part_no, lot_no)` 对应 INV-02；`version` 乐观锁 |

---

## 5. 应用服务与事务边界

```java
@Service
public class DeductInventoryWriteAppService {
    @Transactional
    public void handle(DeductInventoryInfo info) {
        idempotencyChecker.check(info.commandId());          // 幂等
        // 调领域服务：内部只读加载聚合 -> 调聚合行为 -> 写仓储持久化 -> 返回事件
        List<Object> events = sparePartDomainService.deduct(info);
        outboxWriter.write(events);                          // 收集事件写 Outbox
    }
}
```

> 应用服务不直接调 `SparePartLotRepository.update`。

---

## 6. 领域服务实现

```java
public class SparePartDomainService {
    public List<Object> deduct(DeductInventoryInfo info) {
        SparePartLotModel lot = lotRepository
                .queryAvailableByPartNo(info.partNo())
                .orElseThrow(SparePartNotAvailableException::new);
        lot.deduct(info.quantity(), info.source());          // 聚合行为（业务规则）
        lotRepository.update(lot);                           // 写仓储在领域服务
        return lot.pullDomainEvents();
    }
}
```

---

## 7. 领域事件、Outbox 与消息发布

| 领域事件 | Java 类 | 发出者 | topic |
|----------|---------|--------|-------|
| `InventoryChangedEvent` | `domain.event.InventoryChangedEvent` | `SparePartLotModel` | `spare.inventory.changed` |
| `InventoryLowWarnedEvent` | `domain.event.InventoryLowWarnedEvent` | `SparePartDomainService` | `spare.inventory.alert` |

---

## 8. 接口层与防腐层（ACL）

| 订阅主题 | 外部事件 | Listener | ACL 转换 | 本上下文 Info | 幂等键 |
|----------|----------|----------|----------|---------------|--------|
| `repair.parts.consumed` | PartsConsumedDTO | `RepairPartsConsumedListener` | `RepairFacadeClient` + `DTO2InfoConverter` | `DeductInventoryInfo` | event_id |

---

## 12. 与领域建模的映射

| 领域建模元素 | 实现元素（规范命名） | 保证机制 / 备注 |
|--------------|----------------------|-----------------|
| SparePartLot 聚合根 | `domain.model.sparepartlot.SparePartLotModel` | `SparePartLotRepository` / `SparePartLotRepositoryImpl` 保存 |
| `deduct(qty)` | `SparePartLotModel.deduct(...)` | 校验 INV-01；发出 `SparePartConsumedEvent`；写仓储在 `SparePartDomainService` |
| INV-02 同一 `(part_no, lot_no)` 唯一 | `uk_part_lot` | 数据库唯一索引 |
| `InventoryChangedEvent` | `domain.event.InventoryChangedEvent` + Outbox | topic `spare.inventory.changed` |
| 维修出库事件消费 | `RepairPartsConsumedListener` + `RepairFacadeClient`(`Impl`) | event_id 幂等 |
````

---

## 11. 自检清单（提交前过一遍）

**前置与边界**
- [ ] 领域建模源文件存在，文档头部“实现依据”链接指向它。
- [ ] 文件落位在正确的 `领域模型/<服务>/实现设计/` 目录下，文件名为 `XX上下文.md`。
- [ ] 没有把其它限界上下文的实现顺手代写进来。
- [ ] §0 明确写了实现目标、核心聚合、事务与集成点。

**八层分层**
- [ ] 包结构包含 `facade / facade-impl / controller / application / domain / repository / infrastructure / utility` 八层，职责清晰。
- [ ] 领域层未依赖 Spring / MyBatis / MyBatis-Plus / MQ / HTTP。
- [ ] 应用服务只编排事务、只读加载、幂等、Outbox，不写核心业务规则，**不写仓储**。
- [ ] 写仓储调用发生在 `DomainService`，不在 `AppService`。
- [ ] Controller / FacadeImpl 不直接调 Mapper 改状态；Controller 只调 facade 声明的方法。

**聚合与对象模型**
- [ ] 每个聚合根有 `XXModel` 充血骨架、行为方法、事件记录方式；子实体可贫血但变更经聚合根。
- [ ] 聚合根行为使用业务动词，无 `update/save/create/delete` 流水账。
- [ ] 实体不暴露独立 Repository，生命周期由聚合根控制。
- [ ] 值对象 `XXVO` 不可变，ID/状态/数量/编码等没有全部退化为裸 String/Long。

**命名规约**
- [ ] POJO 后缀严格用 DO/Model/DTO/Info/VO/Query/Request/Converter，无自创后缀。
- [ ] Bean 命名严格用 Controller/Facade(FacadeImpl)/AppService(Write/Read)/DomainService/Repository(RepositoryImpl)/Mapper(BaseMapper)/Client(ClientImpl)/FacadeClient(FacadeClientImpl)/Configuration。
- [ ] Repository/Mapper/Client/DAO 方法用 query/list/page/count/insert/delete/update 前缀；聚合根行为用业务动词。

**持久化与一致性**
- [ ] Repository 以聚合根为单位，不是表 Mapper 的机械包装。
- [ ] MyBatis / MyBatis-Plus 使用边界有理由；Mapper 继承 BaseMapper；DO≠Model。
- [ ] Converter 逐字段手写，未用 BeanUtils.copyProperties / MapStruct。
- [ ] 每条跨实例不变式有唯一索引、乐观锁、幂等表或其它明确机制。
- [ ] 并发控制策略明确到 version / lock / unique key / retry / reject。

**事件与集成**
- [ ] 领域事件名与领域建模逐字一致。
- [ ] 对外发布事件默认通过 Outbox；若直接 Kafka，已说明理由。
- [ ] Outbox 字段、发布流程、失败重试或补偿策略清楚。
- [ ] 外部事件消费经 `FacadeClient`/`Client` + Converter 翻译，不直接把外部 DTO 传入领域层。
- [ ] 消费外部事件有幂等键与去重位置。

**读模型与测试**
- [ ] 查询场景放在 `ReadAppService` / 投影，不污染聚合写模型。
- [ ] 每条核心 INV 至少能落到一个领域单元测试或持久化约束测试。
- [ ] 状态机非法流转有失败用例。
- [ ] 消息重复投递、乱序/迟到（如适用）有测试或补偿说明。

**可追溯与修订**
- [ ] §12 映射表覆盖领域建模所有聚合、实体、值对象、领域服务、应用服务、领域事件、INV、读模型、对外契约、暂缓项，且使用规范命名。
- [ ] 已发布 Facade API / topic / eventType / 表关键字段未做破坏性改名；若改名已在顶部变更说明中列出迁移方式。
- [ ] 领域建模中的热点或暂缓项未静默消失，已写入“暂缓与扩展点”。
