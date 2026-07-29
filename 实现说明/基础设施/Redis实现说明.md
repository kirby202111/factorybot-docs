# Redis 实现说明

> **定位**：本文是本 MES 项目 **Redis 使用场景识别与实现方式**的权威方案。基于对 [领域模型](../../领域模型/领域总览.md) 全部 14 个限界上下文业务建模的审查，结构化梳理所有需要使用 Redis 的具体业务场景、使用方式（缓存 / 热层快照 / 去重 / 分布式协调）、闪光点与重难点，并明确 Redis **不适用**的边界。
>
> **与既有文档的关系**：
> - 领域契约（聚合根 / 事件 / 不变式编号）的权威源是各上下文的领域建模文档，本文**不重复定义**，只引用其不变式编号（INV-CX-04、BIZ-02、BIZ-07、INV-CX-05 等）。
> - [MES高频数据方案](../高频数据/MES高频数据方案.md) 已在 §6.1（msg_id 去重）与 §7.1（热层实时快照）明确使用 Redis，本文是其**场景展开与重难点深挖**，不重复管道设计。
> - 与 [Outbox设计方案](../业务事件/Outbox设计方案.md) 是**分工关系**：Outbox 管低频业务契约事件的可靠投递（MySQL 事务 + Kafka），Redis 不承担 MQ 角色；本文管 Redis 作为缓存 / 热层 / 去重 / 协调的职责。
> - 与 [MySQL配置说明](./MySQL配置说明.md)（`cache-enabled: false`，显式关闭 MyBatis 二级缓存）、[Kafka配置说明](./Kafka配置说明.md)、[MinIO配置说明](./MinIO配置说明.md) 并列，补齐中间件配置族。
>
> **适用边界**：覆盖 MES 主体三大服务（制造资源 / 设备管理 / 生产执行）的业务建模场景。**Agent / RAG 服务**（Python 侧）的 Redis 使用（ConfirmationStore / 工具结果缓存 / 租户限流信号量）见 [整体技术选型与模块划分](../../整体技术选型与模块划分.md) §2.5，**不在本文范围**。
>
> **可靠性语义**：Redis 在本项目中**只承担读优化投影、热层快照、去重加速、协调辅助**，**永不承担事务一致性事实来源**。事实来源始终是 MySQL 聚合 + Kafka 事件流；Redis 丢失可从温层 / 事件流重建。这是一切 Redis 场景设计的总前提。

---

## 0. 口径纪律

本文出现的命中率、TTL、QPS、内存容量等数值均为**设计容量目标 + 假设**（基于典型 SMT/PCBA + Box Build 单线量级估算），不是线上实绩。**2026-07-28 已就全部 🔴 决策点与用户拍板落定（见附录 A），均为 v1 起步值，上线后按真实集群容量与车间实测量迭代**，不作线上实绩承诺。

---

## 1. Redis 在 MES 中的定位与场景全景

### 1.1 为什么 MES 需要 Redis

MES 业务的三个硬约束把 Redis 推到不可替代的位置：

| 硬约束 | 来源 | 对存储的要求 | Redis 不可替代性 |
|---|---|---|---|
| **过点 SLA ≤1s**（含扫码->防错->判定->记录->推进->累计全链路） | 在制品执行上下文 §4.1 🔴 热点 | 防错校验六维度查询须毫秒级（pipeline 6 查 ~1ms，远小于 1s SLA） | **Redis 共享缓存为主**（多实例一致性）；故障降级 REST 直查源上下文 |
| **过点设备实时数据查询 ≤200ms** | INV-CX-04（[设备数据接入上下文 §4.8](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)） | 跨服务读设备当前温度/程序版本，不能回放 Kafka、不能查 MySQL | **必须 Redis**（跨服务共享热数据） |
| **高频采集"不丢不重"** | [MES高频数据方案 §2.2](../高频数据/MES高频数据方案.md) BIZ-02 | 秒级持续流补传/重发产生重复 msg_id，平台侧需高吞吐去重 | **DB 唯一索引起步**（v1 量级 DB 余量约百倍）；布隆为 DB 扛不住时的预案 |

### 1.2 场景全景表

下表给出 Redis 在 MES 业务建模中的全景，标注使用方式、载体选型与覆盖状态。

| # | 场景 | 所属上下文 | 使用方式 | 载体选型 | 事实来源 | 覆盖状态 |
|---|---|---|---|---|---|---|
| ① | 过点校验缓存族（工艺/设备可用性/质量门禁/工单状态/齐套/首件） | 在制品执行上下文 | 缓存（读优化投影） | **Redis 共享缓存为主 + REST 降级** | 制造资源/设备台账/质量/工单上下文聚合 | ✅ 已落定（§2） |
| ② | 设备实时数据热层快照 | 设备数据接入上下文 | 热层快照（跨服务共享） | **Redis（hash by equipment_id）** | Kafka `dc.*` 流 + 温层 | ✅ 已覆盖（[高频数据方案 §7.1](../高频数据/MES高频数据方案.md)，§3 展开） |
| ③ | 高频采集 msg_id 去重 | 设备数据接入上下文 | 去重（DB 唯一索引 + 布隆预案） | **DB 唯一索引（v1 起步）+ Redis Bloom 预案** | msg_id 唯一性（BIZ-02） | ✅ 已覆盖（[高频数据方案 §6.1](../高频数据/MES高频数据方案.md)，§4 展开） |
| ④ | 在制品位置快照读模型 | 在制品执行上下文 | CQRS 读侧投影 | **MySQL 读模型为主**（不加 Redis 加速） | 写侧 RoutingProgress 聚合 | ✅ 已落定（§5） |
| ⑤ | 工单/产品流水号生成 | 工单管理上下文 | 分布式序号（INCR） | **DB 唯一索引**（不用 Redis） | `(serial_no_prefix, sequence_no)` 唯一索引（BIZ-03） | ✅ 已落定（§6.1） |
| ⑥ | 跨实例全局限流 | 横切（过点入口/ACL 出站） | 分布式限流（令牌桶 Lua） | **Redis + Lua 令牌桶**（起步即启用） | 无（速率控制） | ✅ 已落定（§6.2） |
| ⑦ | 事件消费幂等去重 | 各上下文 consumed_event | 去重 | **MySQL 表（不用 Redis）** | `(event_id, consumer_group)` 主键 | ✅ 已澄清不用 Redis（§7） |
| ⑧ | 可用性重算并发控制 | 设备工装台账上下文 | 并发控制 | **DB 乐观锁（不用 Redis 分布式锁）** | `asset_id + version`（BIZ-07） | ✅ 已澄清不用 Redis（§7） |
| ⑨ | 消息队列 | 全局 | MQ | **Kafka（不用 Redis Pub/Sub / Stream）** | - | ✅ 已澄清不用 Redis（§7） |

> **状态含义**：✅ 已覆盖/已落定 = 文档已明确；🟡 已识别 = 文档未定型，给出专家建议与决策点。（原 🔴 阈值待拍板项已于 2026-07-28 全部落定，见附录 A）

### 1.3 核心认知：Redis 在 MES 的"三不"原则

1. **不承担事实来源**：Redis 是读优化投影/热层快照，丢失可重建；事实来源是 MySQL 聚合 + Kafka 事件流。
2. **不承担 MQ**：业务契约事件走 Outbox + Kafka（[Outbox设计方案](../业务事件/Outbox设计方案.md)）；高频采集走 Kafka 直连（[高频数据方案 §5](../高频数据/MES高频数据方案.md)）。Redis Pub/Sub 无持久化、Stream 吞吐弱，均不替代 Kafka。
3. **不替代 DB 唯一性约束**：跨实例唯一性（msg_id 除外，因其高吞吐）优先用 DB 唯一索引 / 乐观锁 / 聚合内事务，Redis 只在 DB 扛不住吞吐时作加速层。

---

## 2. 场景①：过点校验本地缓存族

### 2.1 业务背景

在制品执行上下文是过点执行枢纽。每次扫码过点，[AntiErrorCheckService](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md) §3.1 并行校验六个维度，每个维度读一个缓存/投影：

| 缓存 | 刷新来源（事件订阅） | 降级查询 | 守护不变式 | 用途 |
|---|---|---|---|---|
| `ProcessRouteCache` | `process.route.lifecycle` (ProcessRouteActivated) | REST 查制造资源服务 ≤500ms | INV-CX-02 | 过点防错项/步骤配置/跳站校验 |
| `EquipmentAvailabilityCache` | `eam.asset.availability` (AssetAvailabilityChanged) | REST 查台账上下文 | BIZ-07（乐观锁） | 设备准入校验（available=true 且在目标工位） |
| `QualityGateCache` | `quality.gate.lifecycle` (QualityGateRuleActivated/Deprecated) | REST 查质量上下文 | rule_id+version 去重 | 质量门禁判定（PASS/HOLD/BLOCK） |
| `WorkOrderStatusCache` | `wo.order.*` (Released/Started/Paused/Resumed/Cancelled...) | REST 查工单管理上下文 | INV-CX-05 | 工单状态校验（仅 RELEASED/IN_PROGRESS 放行） |
| `KitStatusCache` | `wo.kit.status` (KitStatusChanged) | REST 查工单管理上下文 | BIZ-08 | 首次过点齐套防错（kit_ready） |
| `FirstArticleGateProjection` | `fai.article.released/blocked` | - | BIZ-03 | 首件门禁校验 |

缓存未命中/不可信时降级 REST 查源上下文；REST 失败则**保守拦截过点**（INV-CX-05：`blocking_reason ∈ {RouteCacheMiss, EquipmentCacheMiss}`，见 [在制品执行上下文 §2.3 BlockingReason](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)）。

### 2.2 载体选型决策（关键架构判断）

领域总览 §5.1/§5.2 与在制品执行上下文将这六个称为"**本地缓存**"（指本上下文维护的读投影缓存，未指定载体）。微服务多实例部署下有三条路线，**2026-07-28 已拍板选定路线 B（Redis 共享为主）**：

| 路线 | 机制 | 优势 | 劣势 | 状态 |
|---|---|---|---|---|
| A. 进程内缓存（Caffeine）为主 | 每实例独立 Caffeine，事件驱动刷新各刷各的，miss 时 REST 降级查源上下文 | 亚毫秒读、零网络 RTT、无外部依赖、单实例故障不波及缓存 | 多实例内存冗余；冷启动/事件延迟窗口内实例间短暂不一致 | 备选（未选用） |
| **B. Redis 共享缓存为主** ✅ 选定 | 所有实例共享一份 Redis，事件驱动刷新覆盖写，miss 时 REST 降级查源上下文 | 实例间一致性好（共享一份）；无内存冗余；多实例 miss 风暴天然吸收 | 每次过点 6 次 Redis RTT（pipeline ~1ms，远小于 1s SLA）；Redis 是吞吐瓶颈与故障域；故障波及所有过点 | **v1 采用** |
| C. Caffeine + Redis 二级缓存 | L1=Caffeine，L2=Redis；miss 依次查 L1->L2->REST | 兼顾低延迟与多实例一致性 | 复杂度高；L1/L2 一致性需事件广播 + 失效 | 备选（未选用） |

**决策**：采用路线 B（Redis 共享缓存为主 + 事件刷新 + REST 降级），**不引入 Caffeine 进程内缓存**。Redis 在本场景是**过点主路径的一等依赖**（非降级二级缓存）：多实例共享一份缓存保证一致性、无内存冗余，pipeline 6 次查询 ~1ms 在 ≤1s SLA 内充裕。代价是 Redis 成为过点吞吐瓶颈与故障域——由 §6.2 起步即启用的 Redis 分布式限流保护下游、§2.5 的 REST 直查降级兜底可用性。

### 2.3 闪光点

- **多实例共享一份缓存，天然一致**：工艺版本/质量规则/设备状态低频写、过点高频读；事件驱动刷新覆盖写共享 Redis，所有过点实例读同一份，无进程内缓存的多实例一致性窗口与内存冗余。
- **降级查询保守拦截（INV-CX-05）**：缓存未命中 + REST 失败时不是"放过"而是 `CheckpointBlocked`，符合 MES 防错优先（fool-proofing）原则——宁可拦截不过错放。
- **`routeVersion` 快照锁定历史追溯**：过点记录携带 `routeVersion`（[CheckpointRecord §1.1](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)），工艺变更只刷缓存影响后续过点，历史过点不受影响。缓存与事实解耦。
- **DDD 聚合边界设计避免了缓存强一致需求**：工艺/设备/质量/工单分属不同限界上下文，各自是事实来源，生产执行服务只持有读投影。最终一致可接受（过点读缓存秒级延迟无碍），这正是能用共享缓存 + 事件刷新的前提。
- **故障降级路径清晰**：Redis 故障时六维度校验统一降级 REST 直查源上下文（§2.5），降级查询由 §6.2 Redis 分布式令牌桶限流保护源上下文不被打爆。

### 2.4 重难点

| 重难点 | 风险 | 应对 |
|---|---|---|
| **Redis 故障 = 过点主路径不可用** | 路线 B 下 Redis 是过点主路径，Redis(主从+哨兵)整体故障时六个校验缓存全不可用 | ① **故障降级 REST 直查源上下文**（§2.5，无 Caffeine 兜底）；② 降级查询走 §6.2 Redis 分布式令牌桶限流保护源上下文；③ 源上下文 REST 失败则保守拦截（INV-CX-05）；④ 主从+哨兵自动故障转移缩短不可用窗口 |
| **缓存一致性（事件丢失/延迟）** | 事件订阅刷新失败或延迟，缓存陈旧导致过点放行不该放行的单/拦截不该拦截的单 | ① 事件刷新幂等（source_event_id 去重，见各 CacheRefreshService）；② 缓存带 TTL 兜底（30min，按数据语义），TTL 到期强制 REST 回源；③ 关键校验（如工单状态终态 CLOSED/CANCELLED）可双读校验 |
| **缓存穿透（查不存在的 key）** | 恶意/异常 SN 或不存在的 work_order_id 反复 miss 打源上下文 | ① 空值缓存（null placeholder，短 TTL 如 60s）；② 布隆过滤器前置判存在性（工单/设备 ID 集合）；③ 过点前置校验 SN/工单有效性 |
| **缓存击穿（热点 key 过期瞬间并发回源）** | 换线瞬间某工单/工艺版本缓存刚过期，多个过点终端并发 REST 查源上下文 | ① 共享 Redis 下并发回源由 §6.2 限流收敛；② 互斥锁（Redis SETNX 单飞，其余读旧值/等）；③ 事件刷新覆盖写尽量在 TTL 到期前完成 |
| **缓存雪崩（大量 key 同时过期）** | 批量工单同时下达/同时刷新，TTL 集中到期 | TTL 加随机抖动（基准 ±20%）；分批预热 |
| **Redis 吞吐瓶颈** | 过点高峰 6 维度 × 并发过点终端打 Redis | ① pipeline 合并 6 次查询为一次往返；② 监控 Redis QPS/慢查询；③ 必要时读写分离（从节点分担读） |
| **冷启动 miss 风暴** | Redis 重启后缓存全空，过点高峰打爆源上下文 | ① 启动预热（加载活跃工单/当前生效工艺版本/在制设备可用性回填 Redis）；② 降级查询限流（§6.2）保护源上下文 |

### 2.5 实现要点

```text
CacheRefreshService（[在制品执行上下文 §3.6]）落地为 Redis 共享缓存（无 Caffeine）:

Redis 共享缓存（主路径）:
  - key 前缀: mes:cache:{context}:{type}:{id}  (见 §8.2)
  - 数据结构: String（JSON）
  - TTL: 30min（兜底，防事件丢失陈旧；TTL 到期强制 REST 回源）
  - 事件驱动刷新覆盖写（无 refreshAfterWrite，无 maxSize）

事件刷新链:
  Kafka 事件 -> CacheRefreshService.refreshXxx(source_event_id 去重)
             -> 写 Redis（覆盖，刷新 TTL）
             -> 失败不阻塞过点（靠 TTL + REST 降级兜底）

正常查询链:
  过点读缓存 -> Redis（pipeline 合并六维度查询） -> 命中则继续判定
  Redis miss -> REST 查源上下文 -> 回填 Redis（带 TTL） -> 继续判定

故障降级链（Redis 不可用时）:
  过点读缓存 -> Redis 不可用 -> REST 直查源上下文（六维度各自降级查询）
            -> 降级查询经 §6.2 Redis 分布式令牌桶限流（Redis 不可用时降级本地令牌桶宽松限流）
  REST 失败 -> CheckpointBlocked(RouteCacheMiss/EquipmentCacheMiss)  [保守拦截，INV-CX-05]
```

---

## 3. 场景②：设备实时数据热层快照（跨服务，≤200ms）

### 3.1 业务背景

过点时校验设备实时状态（波峰焊当前温区温度、烧录器当前程序版本、电批扭矩实时值）要求 **≤200ms**（INV-CX-04，[设备数据接入上下文 §4.8 DataQueryAppService.getRealtimeDataPoints](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)）。这不能走 Kafka 回放（秒级流历史），不能走 MySQL 查询（写放大 + 慢），需内存级快照。

关键特征：**数据源（设备数据接入上下文）与消费方（在制品执行上下文过点引擎）跨服务**，且数据由采集流**持续高频写入**——进程内缓存无法跨服务共享写入，**必须用 Redis 作跨服务共享热层**。

### 3.2 Redis 使用方式

| 维度 | 设计 |
|---|---|
| 数据结构 | **Hash**，key = `mes:hot:equip:{equipment_id}`，field = `logical_name`（如 `zone3_temp`、`program_version`），value = 采样值 + `source_ts` |
| 写入 | 平台 `PlatformIngestionService.PersistAndPublish` 时同步刷新热层最新值（**覆盖写**，只留最新）|
| 查询 | 过点执行上下文 REST 查 `DataQueryAppService.getRealtimeDataPoints(equipment_id, logical_names)` -> `HMGET` 多 field |
| 保留 | 只保留最新值（或最近 N 个采样，🔴 按追溯需求定） |
| 失效 | 设备 `OFFLINE` 后保留最后值 + 标记 `offline_at`；资产 `CLOSED` 终态后清除（[高频数据方案 §7.1](../高频数据/MES高频数据方案.md)） |

### 3.3 闪光点

- **跨服务共享热数据，过点零回放延迟**：设备数采服务写 Redis 一次，所有过点实例共享读，避免每个实例各自订阅 Kafka 维护内存快照（重复投影 + 内存冗余）。
- **Hash 结构契合"一设备多点位"查询**：过点只查该设备关心的几个 `logical_name`，`HMGET` 一次往返取齐，比 KV 多次 GET 或 String 存全量 JSON 更高效。
- **热/温/冷分层的关键一环**：热层 Redis 管过点实时查询，温层 MySQL/TSDB 管近期历史与 SPC，冷层对象存储管长期归档（[高频数据方案 §7](../高频数据/MES高频数据方案.md)）。各司其职，Redis 不背历史包袱。
- **覆盖写天然幂等**：采集流重复/补传到达，覆盖写最新值即可，无需去重（去重在 msg_id 层已做，§4）。

### 3.4 重难点

| 重难点 | 风险 | 应对 |
|---|---|---|
| **数据一致性（热层是投影非事实源）** | Redis 数据可能与温层/Kafka 短暂不一致；Redis 宕机数据丢失 | ① 明确热层是**读优化投影**，事实源是 Kafka + 温层，丢失可重建（[高频数据方案 §7.1](../高频数据/MES高频数据方案.md)）；② 写入时附带 `source_ts`，过点查询可判新鲜度，超时数据计 `EquipmentDataTimeout` 拦截（INV-10） |
| **过期/陈旧策略** | 设备掉线后热层残留最后值，过点误判设备"正常" | ① `OFFLINE` 后保留最后值但标记 `offline_at`，过点校验设备可用性走 `EquipmentAvailabilityCache`（§2）而非热层；② 热层 `source_ts` 超过 **30s** 视为陈旧，触发 `EquipmentDataTimeout` 拦截 |
| **重建（冷启动 / Redis 故障后）** | Redis 重启后热层全空，过点查询全 miss | ① 从 Kafka `dc.*` 主题重放最近窗口（`auto-offset-reset=latest` + 回放近 N 分钟）重建最新值；② 重建期间过点降级查温层（慢但可用）或保守拦截；③ Redis 持久化（§8.3）缩短重建窗口 |
| **大点位设备内存膨胀** | 某些设备（如多温区波峰焊）点位多，Hash 大 | ① 只存过点关心的 `logical_name`（按工艺配置裁剪）；② 监控 Hash 内存，超阈值告警 |
| **跨服务写入竞态** | 多网关/多平台实例并发写同一设备热层 | 覆盖写以 `source_ts` 最新为准（写入时 `source_ts <= 现值` 则丢弃，CAS 语义），保序由 `partition_key=equipment_id` 保证（[高频数据方案 §5.5](../高频数据/MES高频数据方案.md)） |

### 3.5 实现要点

```text
写入侧（设备数据接入上下文 PlatformIngestionService）:
  PersistAndPublish 落温层后:
    -> HSET mes:hot:equip:{equipment_id} {logical_name} "{value}|{source_ts}"
    -> 若 source_ts < 现有 field 的 source_ts -> 丢弃（乱序矫正已在上游做，此处兜底）
    -> 设备 OFFLINE: HSET mes:hot:equip:{equipment_id} __offline_at__ {ts}
    -> 资产 CLOSED: DEL mes:hot:equip:{equipment_id}

查询侧（过点执行上下文 -> DataQueryAppService.getRealtimeDataPoints）:
  HMGET mes:hot:equip:{equipment_id} {logical_name1} {logical_name2} ...
  -> 解析 value|source_ts
  -> 若 source_ts 距今 > 30s -> 计 EquipmentDataTimeout（INV-10，保守拦截）
  -> 否则返回实时值供防错校验

重建（Redis 故障恢复后）:
  订阅 dc.process.sample.raw / dc.station.event.raw
  -> auto-offset-reset=latest + 回溯近 5min
  -> 按 equipment_id 覆盖写最新值
```

---

## 4. 场景③：高频采集 msg_id 去重（DB 唯一索引 + 布隆预案）

### 4.1 业务背景

高频设备采集走"边缘缓冲 + Kafka 直连"，可靠性模型从"同事务原子"降级为"不丢不重"（[高频数据方案 §2](../高频数据/MES高频数据方案.md)）。补传/重发会产生重复 `msg_id`，平台侧 `PlatformIngestionService` 按 `msg_id` 去重（BIZ-02，[设备数据接入上下文 §3.6](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)）。

**v1 量级下 DB 唯一索引即可扛**：典型 10 线 × 10 设备 × 1Hz ≈ 100 TPS，7 天 ≈ 6000 万 msg_id，MySQL 唯一索引 INSERT + 判重余量约百倍（轻松上万 TPS）。故 v1 以 **DB 唯一索引** 为起步主路径，布隆过滤器列为"DB 扛不住时的加速预案"（非 v1 启用）。这兑现 §1.3 第3条"Redis 只在 DB 扛不住吞吐时作加速层"与 §11 第7条"每处 Redis 必答为什么不是 MySQL"的既有原则。

### 4.2 使用方式

**v1 主路径（DB 唯一索引）**：

```text
PacketReceived(msg_id, ...)
  -> SELECT 去重表 WHERE msg_id = ?
     命中  -> DuplicateDiscarded（BIZ-02，丢弃，不落库不分发）
     未命中 -> PacketAccepted
            -> INSERT 去重表(msg_id)  （唯一索引冲突即判重，防并发重复）
            -> 进入乱序矫正 / 落库 / 分发
```

两层防御：**DB `msg_id` 唯一索引（精确判重 + 防并发 INSERT 冲突）-> 消费端 msg_id 幂等（漏网兜底）**，叠加等效"不重"。

**布隆预案（非 v1，DB 扛不住时启用）**：

当实测持续去重 TPS 进入 **1000~10000** 区间、DB 行锁/写延迟成为瓶颈时，前置布隆过滤器作加速层，DB 唯一索引降为兜底：

```text
PacketReceived(msg_id, ...)
  -> BF.EXISTS mes:dedup:dc:{date_bucket} {msg_id}
     命中（可能假阳性） -> 查 DB msg_id 唯一索引确认
        DB 命中   -> DuplicateDiscarded
        DB 未命中 -> 布隆假阳性，放行
     未命中 -> PacketAccepted
            -> BF.ADD mes:dedup:dc {msg_id}（占位，防并发重复）
            -> INSERT DB msg_id 唯一索引（兜底，并发最后防线）
            -> 进入乱序矫正 / 落库 / 分发
```

启用后升为三层防御：**Redis 布隆（快速判重，少量假阳性）-> DB 唯一索引（精确兜底）-> 消费端幂等（漏网兜底）**。

### 4.3 闪光点

- **DB 方案精确无假阳性**：唯一索引判重零误判，无需假阳性回退逻辑；与 MySQL 事务原子，无 Redis/DB 一致窗口。
- **无 Redis 依赖，故障域减一**：v1 去重不依赖 Redis，Redis 宕机不影响去重（过点缓存/热层另有降级，§2.5/§3.4），符合"Redis 是有代价的依赖"原则。
- **演进路径平滑**：DB 扛不住时无缝前置布隆，三层防御就位，去重主流程不变，仅多一道加速层。
- **两层防御已保"不重"**：DB 唯一索引（精确 + 防并发）+ 消费端幂等（兜底），满足"不丢不重"降级模型；布隆只是吞吐加速，非正确性依赖。

### 4.4 重难点

| 重难点 | 风险 | 应对 |
|---|---|---|
| **去重表膨胀** | 7 天保留下 6000 万行级（满载），表/索引膨胀影响写入与查询 | ① 按 `received_ts` 定期清理超保留期 msg_id（保留期 **≥7 天**，对齐最长补传窗口 + 安全余量，与死信保留窗口对齐）；② 必要时按天分区 |
| **补传突发写入** | 网关断连恢复后批量补传，瞬时涌入大量 INSERT | ① 补传背压（[高频数据方案 §8](../高频数据/MES高频数据方案.md)）保护 DB；② 批量 INSERT；③ 监控去重表写入 QPS |
| **并发 INSERT 冲突** | 两实例同时判未命中，并发 INSERT 同一 msg_id | 唯一索引冲突拦截其一，冲突重试即可（开销小）；消费端 msg_id 幂等再兜底 |
| **保留期与补传窗口对齐** | 去重表保留期 < 最长补传窗口，补传的重复 msg_id 被当成新消息 | 保留期 **≥ 最长补传窗口 + 安全余量**（≥7 天，[高频数据方案 §6.1](../高频数据/MES高频数据方案.md)） |
| **布隆预案触发核定**（预案） | 何时启用布隆需实测判断，过早启用是过度设计 | 触发参考：持续去重 TPS 1000~10000 评估启用，>10000 必要；上线后按 `dc_dedup_conflict_total`、INSERT p99、行锁等待实测核定 |
| **布隆膨胀 / 假阳性**（预案） | 启用后 msg_id 持续累积，位图满后误判率飙升 | 滚动按天分桶 `mes:dedup:dc:{YYYY-MM-DD}`，保留 7 桶滚动删最旧；误判率 0.1%，假阳性回退 DB（§4.2 预案流程） |

### 4.5 实现要点

**v1（DB 唯一索引）**：
- 去重表 `msg_id` 唯一索引；按 `received_ts` 建索引支撑保留期清理任务（定时删超 7 天记录）。
- 并发 INSERT 冲突按唯一索引冲突重试，无需分布式锁。
- 两层防御就位：DB 唯一索引 + 消费端 msg_id 幂等。

**布隆预案（非 v1，触发后启用）**：
- 使用 **RedisBloom 模块**（`BF.ADD` / `BF.EXISTS` / `BF.RESERVE`），需 Redis ≥ 4.0 + 模块加载。
- 容量预估：按实测 msg_id 速率 × 7 天 × 误判率 0.1% 算位图；参考满载 ≤10 线、百条/s/线 ≈ 6 亿 msg_id，位图 ≤1.1GB（10 个 hash 函数）--为预案上限估值，非 v1 占用。
- 膨胀治理：滚动按天分桶 `mes:dedup:dc:{YYYY-MM-DD}`，保留 7 桶滚动删最旧，简单可靠，无需计数布隆模块。
- 触发后 DB 唯一索引仍保留作兜底，不删除。

---

## 5. 场景④：在制品位置快照读模型（CQRS 读侧）

### 5.1 业务背景

在制品执行上下文采用**上下文内写侧/读侧 CQRS**：5 个写侧聚合发领域事件，读侧投影聚合 `WipUnit` + 读模型 `WipLocationSnapshot` / `BatchLocationView` 进程内订阅投影（[在制品执行上下文 §1.6 / §2.14](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)）。`WipProjectionService`（§3.10）消费写侧事件投影位置/状态/流转历史，幂等去重（`source_event_id`，BIZ-07）。

### 5.2 载体选型

**2026-07-28 已拍板：路线 A（MySQL 读模型为主），不额外投影 Redis 加速。**

| 路线 | 机制 | 状态 |
|---|---|---|
| **A. MySQL 读模型表** ✅ 选定 | `WipLocationSnapshot` / `WipHistoryProjection` 落 MySQL 表，事件驱动刷新 | **v1 采用** |
| B. Redis 读模型 / 加速 | 位置快照落 Redis Hash 供大屏秒级刷新 | 未选用（可视化查询秒级/分钟级，MySQL 够用） |

**决策**：`WipHistoryEntry` 只增不可改不可删（INV-04），需长期追溯，MySQL 更合适；车间可视化查询频率不高（秒级/分钟级），MySQL 读模型足够，**不额外投影 Redis 加速**，避免增加 Redis 投影与一致性维护。若未来上高频实时大屏，再评估路线 B 作为可视化加速层（非事实源）。

### 5.3 闪光点

- **CQRS 读写分离，读侧可独立选型**：写侧聚合守护不变式，读侧投影按查询模式选 MySQL/Redis，互不污染（[在制品执行上下文 §0](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)）。
- **幂等重放重建（BIZ-07 / INV-CX 降级 BIZ-09）**：`WipUnit` 可从事件流完全重放重建，Redis/MySQL 读模型丢失无碍，投影是派生数据。

### 5.4 重难点

| 重难点 | 风险 | 应对 |
|---|---|---|
| **事件乱序** | `UnitRoutedToRework` 与 `UnitReworkReentered` 投递乱序，投影位置错乱 | ① 维护事件序号/过点时间戳，乱序暂存缓冲按序重放（[在制品执行上下文 §3.10 🔴 热点](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)）；② `source_event_id` 幂等去重保证重放安全 |
| **Redis 读模型一致性** | Redis 投影与 MySQL 写侧短暂不一致 | Redis 投影是派生数据，可重建；可视化容忍秒级延迟 |
| **历史膨胀** | `WipHistoryEntry` 只增，高频过点下历史增长快 | MySQL 按工单分区归档；Redis 只存最新位置快照，不存全量历史 |

### 5.5 实现要点

- MySQL 读模型：`wip_location_snapshot`（按 `work_order_id`+`sn` 唯一）、`wip_history`（按 `sn`+`timestamp` 索引），事件驱动 upsert/insert。
- Redis 加速：**v1 不启用**（§5.2 决策）。若未来启用，`mes:wip:snapshot:{work_order_id}` Hash 存该工单在制位置分布，可视化大屏 `HGETALL` 一次取齐；TTL 按工单生命周期。

---

## 6. 场景⑤⑥：分布式协调（可选/优化项）

### 6.1 场景⑤：工单/产品流水号生成（Redis INCR 可选优化）

**业务背景**：[MesSerialNumberService](../../领域模型/生产执行服务/领域建模/工单管理上下文.md) §3.4 按前缀递增生成全局唯一 `MesSerialNo`，`ProductSerialNoService` §3.5 批量生成产品流水号。当前由 `(serial_no_prefix, sequence_no)` DB 唯一索引保证并发安全（BIZ-03/BIZ-06/BIZ-07）。

**Redis 使用方式（可选优化）**：

```text
方案 A（当前，DB 唯一索引）:
  sequence_no = SELECT MAX(sequence_no) + 1 FOR UPDATE
  INSERT ... 唯一索引冲突重试

方案 B（Redis INCR 优化，可选）:
  sequence_no = INCR mes:seq:{prefix}
  -> 仍落 DB 唯一索引兜底（Redis INCR 与 DB 不原子）
```

| 闪光点 | 重难点 |
|---|---|
| Redis `INCR` 原子递增，免 DB 行锁，高并发下达号更快 | **Redis 与 DB 不原子**：INCR 成功但 DB INSERT 失败则号段空洞；需 DB 唯一索引兜底 + 接受空洞（流水号不连续可接受，文档已确认"不可回收"） |
| `INCR` 单线程串行保证同前缀递增 | Redis 故障降级回 DB `MAX+1 FOR UPDATE`；故障期间号段可能回退（与"不可回收"冲突），需 Redis 持久化（AOF appendfsync everysec）缩短窗口 |

**决策**：工单下达是低频操作（[工单管理上下文 §3.2](../../领域模型/生产执行服务/领域建模/工单管理上下文.md) 已确认"低频，建议默认不缓存"），**保持 DB 唯一索引方案 A，不引入 Redis**。仅当批量发号成为瓶颈时评估方案 B。Redis-first 不等于处处用 Redis——低频场景仍用 DB 唯一索引（与事务原子、无号段空洞）。

### 6.2 场景⑥：跨实例全局限流（Redis 令牌桶 Lua）

**业务背景**：过点入口（如换班高峰）、ACL 出站 REST（调源上下文降级查询，含 §2.5 Redis 故障降级时的并发回源）、SAP 工单接收等场景，多实例部署下需**跨实例统一限流**保护下游。单实例令牌桶（Resilience4j）无法感知其它实例的消耗。

**Redis 使用方式**：Redis + Lua 脚本实现分布式令牌桶（`INCRBY` + `EXPIRE` 原子判桶）。

| 闪光点 | 重难点 |
|---|---|
| Lua 脚本在 Redis 单线程内原子执行，跨实例精确限流 | Redis 故障时限流失效；降级为本地令牌桶（Resilience4j，宽松限流）兜底可用性 |
| 集群级配额统一管控（与 [Outbox §9.4](../业务事件/Outbox设计方案.md) Kafka 配额互补） | 限流是"自律"非"强制"，需下游也有自我保护（熔断/背压） |

**决策**：**起步即启用 Redis 分布式令牌桶**（非单实例起步）。理由：① §2 选定 Redis 共享缓存为主，Redis 已是一等依赖，限流复用同一 Redis（主从+哨兵）零额外中间件；② §2.5 Redis 故障降级时并发 REST 回源需跨实例限流保护源上下文，单实例令牌桶此时无法兜底。Redis 故障时限流降级本地令牌桶（宽松）+ 下游熔断/背压。Outbox Publisher 限流仍维持单实例令牌桶 + Kafka 集群配额（[Outbox §9](../业务事件/Outbox设计方案.md)），二者是不同限流场景，不强制统一。

### 6.3 为什么 MES 业务建模基本不需要 Redis 分布式锁

这是 DDD 聚合边界设计的**正向收益**——以下场景天然不需要分布式锁：

| 场景 | 常见误用 | MES 正确做法 | 为什么不需要 Redis 锁 |
|---|---|---|---|
| 同线已确认槽位不重叠（排产 INV-01） | Redis 分布式锁锁产线 | `LineSchedule` 聚合内事务强一致校验（[排产上下文 §1.1](../../领域模型/生产执行服务/领域建模/排产上下文.md)） | 聚合边界把"同线槽位"收敛到单聚合，事务内校验 |
| 可用性重算并发（台账 BIZ-07） | Redis 分布式锁锁资产 | DB 乐观锁 `asset_id + version`（[设备工装台账上下文](../../领域模型/设备管理服务/领域建模/设备工装台账上下文.md)） | 乐观锁无锁竞争，冲突重试即可 |
| 设备单活跃通道（BIZ-01） | Redis 分布式锁锁设备 | `(equipment_id, status=ACTIVE)` DB 唯一索引 | 唯一索引是数据库级强制约束，比 Redis 锁更可靠 |
| 工单状态机流转 | Redis 分布式锁锁工单 | `WorkOrder` 聚合内状态机 + 版本号 | 聚合内事务保证，跨实例由乐观锁/唯一索引兜底 |

**结论**：MES 通过 DDD 聚合边界 + DB 唯一索引/乐观锁，把并发控制收敛到聚合内事务或 DB 约束，**规避了 Redis 分布式锁的复杂性**（锁过期/续约/脑裂/不可重入）。这是架构成熟度的体现，应作为设计原则坚守。

---

## 7. 明确不用 Redis 的场景（边界澄清）

为避免 Redis 滥用，下表明确 MES 中**看似适合 Redis 但实际不该用**的场景：

| 场景 | 误用倾向 | 正确选型 | 理由 |
|---|---|---|---|
| **消息队列** | Redis Pub/Sub / Stream | **Kafka**（业务事件 Outbox + 采集直连） | Redis Pub/Sub 无持久化、Stream 吞吐弱；Kafka 持久化 + 高吞吐 + 分区有序，是 MES 事件骨干（[Outbox设计方案](../业务事件/Outbox设计方案.md) / [高频数据方案](../高频数据/MES高频数据方案.md)） |
| **事件消费幂等表** `consumed_event` | Redis SET 去重 | **MySQL 表** `(event_id, consumer_group)` 主键 | 幂等记录必须与业务处理**同本地事务原子**（[Outbox §3.3/§8.3](../业务事件/Outbox设计方案.md)）；Redis 无法参与 MySQL 事务，跨存储无法原子 |
| **可用性重算并发** | Redis 分布式锁 | **DB 乐观锁** `asset_id + version`（BIZ-07） | 乐观锁无锁竞争、冲突重试简单；Redis 锁有过期/脑裂问题 |
| **单活跃通道/流水号唯一性** | Redis SETNX | **DB 唯一索引**（BIZ-01/BIZ-03） | DB 唯一索引是持久化强制约束，比 Redis 锁/SETNX 更可靠，且与事务原子 |
| **Outbox Publisher 限流** | Redis 令牌桶 | **单实例令牌桶 + Kafka 集群配额**（[Outbox §9](../业务事件/Outbox设计方案.md)） | Publisher 多实例各自令牌桶 + 集群级配额兜底已足够；引入 Redis 增加依赖 |
| **聚合状态/事务数据** | Redis 存聚合 | **MySQL 聚合表** | 聚合是事实来源，必须事务持久化；Redis 不可作事实源（§1.3 三不原则） |

---

## 8. Redis 部署与配置

### 8.1 部署形态

**2026-07-28 已拍板：主从 + 哨兵（Sentinel），≤10 线/单 MES 部署。**

| 形态 | 适用 | 状态 |
|---|---|---|
| **主从 + 哨兵（Sentinel）** ✅ 选定 | 中小规模（单车间/单 MES 部署，≤10 线） | **v1 采用**；主从自动故障转移，哨兵监控选主，满足过点 SLA 与采集去重可用性 |
| Cluster 集群 | 大规模（多车间/数据量超单机内存） | 未选用；若未来超单机内存或多车间隔离再评估（Hash tag 保同 `equipment_id` 同槽） |

热层（§3）、去重（§4）、过点校验缓存（§2）、限流（§6.2）共用同一 Redis（主从+哨兵），DB 号隔离（§8.2）。

### 8.2 数据隔离与 Key 命名

统一 key 前缀 `mes:{domain}:{type}:{id}`，按场景分 DB 号：

| 场景 | DB 号 🔴 | Key 前缀 | 数据结构 |
|---|---|---|---|
| 过点校验缓存（§2，主路径） | 0 | `mes:cache:{context}:{type}:{id}` | String（JSON） |
| 设备实时数据热层（§3） | 1 | `mes:hot:equip:{equipment_id}` | Hash |
| msg_id 去重（§4） | 2（预案预留） | `mes:dedup:dc[:{date_bucket}]` | DB 唯一索引（v1）/ Bloom（预案，RedisBloom 模块） |
| 在制品位置快照（§5，可选） | 3 | `mes:wip:snapshot:{work_order_id}` | Hash |
| 流水号（§6.1，可选） | 4 | `mes:seq:{prefix}` | String（INCR） |
| 全局限流（§6.2，可选） | 5 | `mes:limit:{resource}:{window}` | String + Lua |

### 8.3 持久化策略（按场景差异化）

| 场景 | 持久化 | 理由 |
|---|---|---|
| 热层快照（§3） | **可关闭**或 AOF everysec | 投影数据，丢失可从 Kafka 重建；重启快 |
| msg_id 去重（§4） | v1 无 Redis 持久化；布隆预案启用时 **AOF everysec** | v1 走 DB 唯一索引，无 Redis 依赖；预案启用后 AOF 缩短布隆重建窗口，DB 唯一索引仍是事实源 |
| 过点校验缓存（§2） | 关闭 | 纯缓存，丢失回源（REST 降级） |
| 流水号（§6.1） | 不适用 | 已选 DB 唯一索引，不用 Redis，无 Redis 持久化 |

### 8.4 内存治理

- **maxmemory + 淘汰策略**：热层/缓存用 `allkeys-lru`；去重布隆（预案启用时）不淘汰（按天滚动分桶治理）。
- `maxmemory` = **4GB**（2026-07-28 拍板）。v1 估算明细：热层 <10MB + 过点缓存 <100MB ≈ <200MB（去重走 DB 唯一索引，不占 Redis）；布隆 ≤1.1GB（10 线满载 × 0.1% 误判）为预案上限估值，非 v1 占用。4GB 较 v1 实占留宽余量应对线数/点位增长与预案启用。上线后按 `used_memory` 实测量迭代。
- 监控 `used_memory` / `evicted_keys`；布隆过滤器误判率（预案启用时）。

---

## 9. 可观测性

| 指标 | 含义 |
|---|---|
| `redis_cache_hit_total{cache}` / `_miss_total` | 过点校验缓存命中率（应 >99%，[在制品执行上下文 §3.6 🔴](../../领域模型/生产执行服务/领域建模/在制品执行上下文.md)） |
| `redis_cache_fallback_rest_total{cache}` | 缓存未命中降级 REST 查询次数（持续高则告警，源上下文被打） |
| `redis_hot_query_latency_seconds{equipment_id}` | 热层查询延迟（应 ≤200ms，INV-CX-04） |
| `redis_hot_stale_data_total` | 热层 `source_ts` 超时计为陈旧的次数（触发 `EquipmentDataTimeout` 拦截） |
| `dc_duplicate_discarded_total` | msg_id 去重命中次数（[高频数据方案 §10.1](../高频数据/MES高频数据方案.md)） |
| `dc_dedup_conflict_total` | DB 去重唯一索引冲突次数（并发重复拦截，v1 主指标） |
| `dc_dedup_table_rows` | 去重表行数（膨胀治理，应随保留期清理回落） |
| `redis_bloom_false_positive_total` | 布隆假阳性回退 DB 次数（误判率监控，布隆预案启用时） |
| `redis_used_memory` / `evicted_keys` | 内存使用与淘汰 |

告警：缓存命中率 <99% 持续；热层查询 p99 >200ms；`used_memory` 接近 `maxmemory`（布隆误判率超预期为预案启用时告警项）。

---

## 10. 实施检查清单

**过点校验缓存（§2）**
- [ ] 六个过点校验缓存落地 **Redis 共享缓存为主**（无 Caffeine），事件驱动刷新覆盖写 + `source_event_id` 幂等。
- [ ] 缓存未命中降级 REST 查询，REST 失败保守拦截（INV-CX-05）。
- [ ] TTL 30min 兜底 + 空值缓存防穿透 + §6.2 限流防击穿。
- [ ] **Redis 故障降级 REST 直查源上下文**（无 Caffeine 兜底），降级查询经 §6.2 限流保护源上下文。

**设备实时数据热层（§3）**
- [ ] Redis Hash 按 `equipment_id` 维护最新数据点，平台 `PersistAndPublish` 同步覆盖写。
- [ ] 过点查询走 `DataQueryAppService.getRealtimeDataPoints` -> Redis，≤200ms（INV-CX-04）。
- [ ] `source_ts` 超时计 `EquipmentDataTimeout` 拦截（INV-10）；`OFFLINE`/`CLOSED` 失效策略落地。
- [ ] Redis 故障重建链路（Kafka 回放）验证。

**msg_id 去重（§4）**
- [ ] v1 落地 DB `msg_id` 唯一索引 + 消费端幂等两层防御。
- [ ] 去重表按 `received_ts` 定期清理，保留期 ≥ 最长补传窗口（≥7 天）。
- [ ] 补传突发背压保护 DB（[高频数据方案 §8](../高频数据/MES高频数据方案.md)）。
- [ ] 布隆预案预留：触发阈值（1000~10000 评估）已定，预案参数（0.1%/≤1.1GB/滚动分桶）就绪，非 v1 启用。

**分布式协调（§6）**
- [ ] 流水号保持 DB 唯一索引（低频不引入 Redis INCR），除非发号成瓶颈。
- [ ] 跨实例限流**起步即启用 Redis 分布式令牌桶**，Redis 故障降级本地令牌桶。
- [ ] 确认所有并发控制场景用聚合内事务/DB 唯一索引/乐观锁，未引入 Redis 分布式锁。

**部署与可观测（§8/§9）**
- [ ] 部署形态定稿（主从+哨兵 / Cluster），DB 号与 key 前缀隔离落地。
- [ ] 持久化策略按场景差异化配置。
- [ ] 指标 + 告警就位（命中率、热层延迟、内存；布隆误判率为预案启用时项）。

---

## 11. 关键原则总结

1. **Redis 是读优化投影/热层/去重/协调辅助，永不承担事实来源**--事实来源是 MySQL 聚合 + Kafka 事件流，Redis 丢失可重建（§1.3 三不原则）。
2. **过点校验缓存以 Redis 共享缓存为主 + REST 降级**（2026-07-28 翻转原 Caffeine 为主）——多实例共享一份保证一致性、无内存冗余，pipeline 6 查 ~1ms 在 ≤1s SLA 内；Redis 是过点主路径一等依赖，故障降级 REST 直查源上下文，不引入 Caffeine（§2）。
3. **设备实时数据热层必须 Redis**--跨服务共享 + 持续高频写入 + ≤200ms 查询，进程内缓存无法跨服务共享（§3）。
4. **高频采集去重 v1 用 DB 唯一索引 + 消费端幂等，布隆为 DB 扛不住时的预案**--v1 量级 DB 余量约百倍，布隆非起步必需；DB 扛不住时前置布隆 O(1) 判重升三层防御保"不重"（§4）。
5. **MES 通过 DDD 聚合边界规避了 Redis 分布式锁**--同线不重叠/可用性重算/单活跃通道/状态机均用聚合内事务 + DB 唯一索引/乐观锁解决，是架构成熟度体现，应坚守（§6.3）。
6. **Redis 不承担 MQ、不替代 DB 唯一性约束、不参与 MySQL 事务原子**--消息走 Kafka，幂等表/聚合状态走 MySQL，跨存储无法原子（§7）。
7. **Redis-first 但不滥用**——过点缓存/限流等热路径选 Redis 作一等依赖（多实例一致性/统一配额），但低频场景（流水号）仍用 DB 唯一索引。每处 Redis 必答"为什么不是 MySQL/Kafka"，Redis 是有代价的依赖（故障域 + 一致性窗口 + 运维成本）。

---

## 附录 A：决策点落定（2026-07-28 拍板）

> 原 🔴 待拍板项已全部由用户拍板落定，下表为最终决策。均为 **v1 起步值，上线后按真实集群容量与车间实测量迭代**（口径见 §0）。

| 决策点 | 落定值 | 说明 |
|---|---|---|
| 过点校验缓存载体路线 | **Redis 共享为主 + REST 降级（无 Caffeine）** | 翻转原 Caffeine 为主；Redis 作过点主路径一等依赖（§2） |
| ~~过点校验缓存 L2 Redis 启用阈值~~ | **作废** | Redis 已是主路径，无 L1/L2 分阶段 |
| 缓存 TTL | **30min** | refreshAfterWrite 作废（无 Caffeine）；TTL 兜底防事件丢失陈旧（§2.4） |
| 热层设备数据陈旧阈值 | **30s** | `source_ts` 超 30s 计 `EquipmentDataTimeout`（§3） |
| 热层重建回放窗口 | **5min** | Redis 故障后从 Kafka dc.* 回放近 5min 重建（§3.5） |
| msg_id 去重保留期 | **≥7 天** | 与死信保留窗口对齐（§4） |
| 布隆误判率 / 容量 | **0.1% / ≤1.1GB（10 线满载）** | **预案参数（非 v1 启用）**；10 个 hash 函数；按线数 × 速率 × 7 天算（§4） |
| 布隆膨胀治理 | **滚动按天分桶** | **预案参数（非 v1 启用）**；保留 7 桶滚动删最旧（§4.5） |
| 去重起步载体 | **DB 唯一索引 + 消费端幂等** | v1 起步；布隆为 DB 扛不住时预案，触发阈值 1000~10000 评估（§4） |
| 在制品位置快照载体 | **MySQL 为主，不加 Redis 加速** | 可视化秒级/分钟级，MySQL 够用（§5） |
| 流水号生成方案 | **DB 唯一索引（不用 Redis）** | 低频下达不优化；与事务原子、无号段空洞（§6.1） |
| 跨实例限流启用时机 | **起步即 Redis 分布式令牌桶** | 翻转原单实例起步；复用同一 Redis，故障降级本地令牌桶（§6.2） |
| Redis 部署形态 | **主从 + 哨兵** | ≤10 线/单 MES；超单机内存再评估 Cluster（§8.1） |
| Redis 持久化策略 | **差异化：去重 AOF everysec / 热层可关 / 缓存关** | 流水号不用 Redis 无持久化（§8.3） |
| Redis `maxmemory` / 淘汰 | **4GB / 热层·缓存 allkeys-lru，去重不淘汰** | 估算 ~1.2GB，留宽余量（§8.4） |
| Redis 故障过点降级（新增） | **REST 直查源上下文（无 Caffeine 兜底）** | 因 Redis 进主路径新增；降级查询经 §6.2 限流保护源上下文（§2.5） |
| 车间规模（辅） | **≤10 线 / 单 MES** | 校验主从+哨兵够用，无需 Cluster |
