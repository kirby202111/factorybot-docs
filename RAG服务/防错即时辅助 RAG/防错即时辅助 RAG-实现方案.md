# 防错即时辅助 RAG 实现方案（Python 技术栈：3 类高频拦截 MVP）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.4 路线 D（防错即时辅助 RAG）的**实现层落地**，与 [防错即时辅助 RAG-详细设计.md](./防错即时辅助 RAG-详细设计.md) 的关系：
> - **详细设计**是全拦截场景的**设计层**（广）--拦截原因分类、缓存治理、推送架构、与 A/B/L1 协同的全景；
> - **本文**是 3 类高频拦截场景（质量门禁拦截 + 点检超期锁定 + 首件未放行）的**实现层**（深）--把详细设计的骨架补全到可落地的 MVP，新增**依赖清单、Kafka topic 订阅清单、只读 REST 契约、Redis 缓存结构、Docker 部署、测试策略**等实现层内容，并对个别契约按各上下文事件风暴落地口径细化（如点检超期回溯路径，§4.3 🔴）。
> 其余拦截场景按 §11 相同范式扩展，MVP 不展开。
>
> **技术栈**：Python（FastAPI + Redis + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + WebSocket 推送解耦，互不侵入。
> **口径纪律**：防错即时辅助 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了防错 RAG"。MES 领域对错误答案零容忍，所以本文强调**拦后异步推送 + 预计算命中即推 + 主判定仍走规则引擎**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标（3 类高频拦截 MVP）

把 RAG 从"操作工旁边问答工具"变成"**现场防错副驾**"--过点拦截事件触发，自动把"一句话原因 + 一个动作"的短卡片推给工位屏幕。**MVP 范围**：聚焦 3 类高频拦截场景，跑通"拦截事件 -> 缓存命中 -> 工位推送"闭环：

| 拦截场景 | `blocking_reason` | 来源 | MVP 处置模式 | 自动分流？ |
|---------|-------------------|------|-------------|-----------|
| **质量门禁拦截（AOI NG）** | `QualityGateFail` | 过点同步门禁 `QualityGateEvaluated` BLOCK | 缺陷码 + SOP 处置 + 返修分流 | ✅ 质量类自动分流（`UnitRoutedToRework`） |
| **点检超期锁定** | `EquipmentUnavailable`（回溯精因=点检超期） | `pm.inspection.overdue` -> 台账 `MarkAssetUnavailable` -> 过点拦截 | 联系设备工程师完成点检后自动解锁 | ❌ |
| **首件未放行** | `FirstArticleBlocked` | 首件处理 `fai.article.blocked` | 联系质量工程师完成首件检验 | ❌ |

> 其余拦截原因（物料齐套 `KitNotReady`、跳站 `SkipViolation`、设备数据超时 `EquipmentDataTimeout` 等）按 §11 相同范式扩展，MVP 不展开。

典型场景："单件 SN-001 过 AOI 站被 `QualityGateFail` 拦截" -> 工位屏幕弹出"AOI 判 NG（桥接缺陷），按 SOP-WELD-014 处置：目检确认 -> 返修工位分流。近 7 天同类拦截 12 次，其中 8 次确认锡膏批次问题"。

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地（MVP 具体动作） |
|------|------|----------------------|
| **拦后异步，不进过点主事务** | 过点判定（含拦截决策）仍在过点主事务内由规则引擎完成；RAG 只在拦截事件发布之后异步消费、异步推送 | 过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）不受 RAG 影响；卡片到达工位允许秒级延迟 |
| **主判定走规则引擎，RAG 只给处置** | 放行/拦截决策永远是过点引擎基于缓存的规则判定；RAG 不参与判定 | 卡片带 `disclaimer`"不改变拦截决策"；处置动作需人在正式界面执行 |
| **预计算 + 缓存命中即推** | 拦截规则有限集，预先打包卡片缓存，命中即推 | 缓存键 = `(blocking_reason, station_type, product_scope, route_version)`；MVP 3 类拦截预热 |
| **只读 MES，只读文档** | D 服务无任何写 MES 接口 | `ReadOnlyIngestionGate` 启动断言禁止写 MES 调用（§9.7） |
| **版本一致性** | 卡片引用 SOP 带 `route_version`，版本变更刷新缓存 | 订阅 `process.route.lifecycle`/`quality.gate.lifecycle` 失效刷新（§5.3） |
| **权限隔离** | 卡片只推拦截工位会话；历史查询带 `tenant_scope` | WebSocket 会话绑定 `(station_id, tenant_scope)`（§7.1） |
| **可观测兜底** | 卡片带来源引用 + 置信度；低置信度/不命中推"转人工" | `confidence < 0.6` 或无 SOP -> 兜底卡片（§10.3） |

### 1.3 与详细设计、A/B RAG、L1 的关系

- **与详细设计**：详细设计给全拦截场景全景与缓存治理设计；本文把其中 3 类高频场景的 dispatcher、generator、ACL、Redis 结构补全到可落地代码，并新增实现层内容（依赖、DDL、Docker、测试）。
- **与文档型 RAG（B）**：D 卡片生成时调 B 的 `search_docs(query, route_version)` 拉 SOP 片段。MVP 阶段若 B 未就绪，用**离线 SOP 导入**兜底（§4.4 🔴），B 就绪后切换为实时 ACL 调用。
- **与追溯型 RAG（A）**：D 卡片的"历史同类处置"调 A 检索。MVP 阶段若 A 未就绪，用**本地历史拦截记录表**兜底（§4.4 🔴）。
- **与 L1 诊断型 Agent**（[L1诊断型Agent-实现方案.md](../../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）：D 是操作工侧实时短卡片，L1 是工程师侧深度诊断，分层不重复。D 卡片可附 L1 深入诊断入口。

### 1.4 与 Java 技术栈的关系

- D 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只订阅 Kafka 只读事件、调只读 REST（A/B RAG + 各上下文）、WebSocket 推送工位。
- 跨语言物理边界天然强制只读：D 服务无法共享 Java 事务/内存，无法进过点主事务、无法旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、领域事件 envelope（`event_id`/`event_type`/`event_version`/`occurred_at`/`source_service`/`trace_id`/`partition_key`，见 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §4.3）、消费侧幂等模式（§6 同事务幂等 + 手动 ack），不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 版本 | 选型理由 |
|------|------|------|---------|
| 语言 | Python | 3.11+ | 类型提示 + Pydantic 校验，与 A/B RAG 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 0.110+ | 异步、原生 OpenAPI；同时承载 WebSocket 推送与 HTTP 入口 |
| 缓存 | **redis-py (async)** | 5.0+ | 卡片缓存主存（命中即推核心）；短 TTL + 主动失效 |
| 检索编排 | **LlamaIndex**（轻量） | 0.10+ | 未命中时编排"拉 SOP + LLM 综合"管线 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 0.2+ | 模型可插拔，与 A/B 一致 |
| Embedding | **bge-m3**（与 B 共享） | 1.0+ | 仅未命中兜底语义匹配，1024 维 |
| 数据校验 | **Pydantic** | v2 | 拦截事件/卡片 DTO/缓存键 schema 即类型 |
| HTTP 客户端 | **httpx** | 0.27+（异步） | 调 A/B RAG + 各上下文只读 REST |
| 消息 | **aiokafka** | 0.10+ | 订阅拦截事件 + 版本失效事件 |
| 推送通道 | **FastAPI WebSocket** | - | 工位屏幕长连接，按 `(station_id, tenant_scope)` 路由 |
| 元数据持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 2.0+ | 消费位点、幂等表、历史拦截记录 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | - | trace 串联、指标告警 |
| 配置 | pydantic-settings | 2.0+ | 环境变量统一管理 |
| 部署 | 独立微服务 `assist-service`（uvicorn + gunicorn worker） | - | K8s 部署；MVP 可 docker-compose 本地起 |

### 2.2 为什么是"预计算 + 缓存"而非"临时跑 LLM"

- **过点现场不能等 LLM**：临时跑 LLM（拉 SOP + 综合）动辄 3-10 秒，操作工等不起。而**拦截原因是有限集**（`blocking_reason` 枚举有限），同一原因处置高度重复--天然适合预计算。
- **预计算命中即推**：预先把"原因 + SOP 片段 + 历史处置"打包入 Redis，过点拦截事件到达时按四元组键查命中，命中即 WebSocket 推送（毫秒级）。未命中走 LLM 生成后**回填缓存**，首次慢后续秒推。
- **与过点 SLA 解耦**：LLM 慢/挂，命中的卡片照推；缓存全空推"转人工"兜底卡片，绝不阻塞过点。

### 2.3 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4）。**建议**：Redis、Embedding 本地化部署，LLM 视车间安全策略二选一。`BaseChatModel` 抽象保证切换零代码改动。
- 工位 WebSocket 长连接需与车间终端网络可达：D 服务部署在与工位终端同网段，或经车间网关代理。MVP 用 `docker-compose` 本地起 Redis + MySQL + assist-service（§9.9）。

### 2.4 依赖清单（pyproject.toml 片段）

```toml
[project]
name = "assist-service"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "gunicorn>=21.2",
  "pydantic>=2.6",
  "pydantic-settings>=2.2",
  "redis>=5.0",
  "llama-index>=0.10",
  "langchain-core>=0.2",
  "httpx>=0.27",
  "aiokafka>=0.10",
  "sqlalchemy[asyncio]>=2.0",
  "asyncmy>=0.2.9",
  "opentelemetry-api>=1.24",
  "opentelemetry-instrumentation-fastapi>=0.45b",
  "prometheus-client>=0.20",
  "sentence-transformers>=3.0",
  "FlagEmbedding>=1.2",
  "websockets>=12.0",
]
```

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ assist-service（独立微服务，Python + FastAPI + Redis + WebSocket） │
│                                                                    │
│  ┌─────────────────────┐   ┌──────────────────────────────────┐  │
│  │ WebSocket 推送层     │◀──│ AssistCardDispatcher              │  │
│  │ /ws/station/{id}     │   │  CheckpointBlocked -> 缓存查 -> 推  │  │
│  └─────────────────────┘   └────────────┬─────────────────────┘  │
│                                         │                          │
│              ┌──────────────────────────┼───────────────────┐     │
│              ▼                          ▼                   ▼     │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ CardCache (Redis) │  │ CardGenerator       │  │ 版本失效  │ │
│  │ 3类拦截预热卡片    │  │ 未命中: SOP+LLM兜底  │  │ Handler  │ │
│  └────────┬──────────┘  └──────────┬──────────┘  └────┬─────┘ │
│           │                        │                  │       │
│  ┌────────▼────────┐       ┌───────▼────────┐         │       │
│  │ CardWarmer      │       │ ACL 只读调用    │         │       │
│  │ 预热3类高频卡片  │       │ B: search_docs │         │       │
│  └────────┬────────┘       │ A: 历史处置     │         │       │
│           │                │ 台账/点检: 精因  │         │       │
│  ┌────────▼────────┐       └────────────────┘         │       │
│  │ Idempotency     │  ┌─────────────────────┐         │       │
│  │ Table (MySQL)   │  │ consumer offset     │         │       │
│  └─────────────────┘  └─────────────────────┘         │       │
└───────────────────────────────────┼───────────────────┼──────────┘
                                    │ 订阅拦截事件(只读)  │ 订阅版本失效
                          ┌─────────▼──────────┐  ┌──────▼──────────┐
                          │ aiokafka Consumer   │  │ process.route.* │
                          │ mes.checkpoint.     │  │ quality.gate.*  │
                          │ lifecycle           │  │ (刷新缓存片段)   │
                          └─────────────────────┘  └─────────────────┘
                                    ▲
                                    │ 过点引擎 Outbox 投递(至少一次)
              ┌─────────────────────┴─────────────────────┐
              │  生产执行服务(过点) + 制造资源/设备管理服务   │
              │  （Java/Spring，事实源）                    │
              └───────────────────────────────────────────┘
```

### 3.1 关键设计决策

- **缓存即核心，LLM 是兜底**：稳态下 99% 的拦截走缓存命中即推，LLM 只在首次未命中时生成。`CardWarmer`（预热）与 `CardGenerator`（运行时）共用生成逻辑（SRP）。
- **事件驱动 + 位点管理**：消费者维护 `consumer_offset` 落 MySQL，重启从断点续跑；`event_id` 幂等表保证重复投递不重复推卡片（§5.4）。
- **推送与生成分离**：`AssistCardDispatcher`（查缓存推工位）与 `CardGenerator`（未命中生成）解耦，生成慢不阻塞推送。
- **ACL 防腐层**：调 A/B RAG 与各上下文 REST 时经 ACL 适配，外部 DTO -> 内部视图（`SopFragment`/`HistoricalDisposition`），外部 schema 变化不污染卡片核心（CLAUDE.md ACL 约束）。

---

## 4. 拦截场景建模：MVP 3 类

### 4.1 拦截原因分类（MVP 覆盖）

| 类别 | `blocking_reason` | 来源 | MVP 处置模式 |
|------|-------------------|------|-------------|
| **质量类** | `QualityGateFail` | 过点同步门禁 `QualityGateEvaluated` BLOCK（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §2.6） | 缺陷码 + SOP 处置 + 返修分流 |
| **设备类** | `EquipmentUnavailable`（回溯精因=点检超期） | `pm.inspection.overdue` -> 台账 `MarkAssetUnavailable` -> 过点拦截（§4.3） | 联系设备工程师完成点检后自动解锁 |
| **首件类** | `FirstArticleBlocked` | 首件处理 `fai.article.blocked`（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §2.7） | 联系质量工程师完成首件检验 |

> 全拦截原因分类见 [详细设计](./防错即时辅助 RAG-详细设计.md) §4.1。

### 4.2 处置卡片模型（AssistCard）

```python
class ActionType(str, Enum):
    REWORK_SPLIT = "REWORK_SPLIT"        # 返修分流
    CONTACT_ENGINEER = "CONTACT_ENGINEER" # 联系工程师
    WAIT_DATA = "WAIT_DATA"              # 等待数据
    AUTH_OVERRIDE = "AUTH_OVERRIDE"      # 申请授权放行
    MANUAL_FILL = "MANUAL_FILL"          # 手工填报

class CardAction(BaseModel):
    action_type: ActionType
    description: str
    target_system: str | None = None
    sop_ref: str | None = None           # SOP 文档引用（带版本）

class HistoricalDisposition(BaseModel):
    similar_count_7d: int
    confirmed_root_cause: str | None = None
    typical_action: str | None = None

class AssistCard(BaseModel):
    card_id: str
    station_id: str
    work_order_id: str
    sn: str | None = None
    blocking_reason: str
    reason_summary: str                  # 一句话原因(≤30字)
    actions: list[CardAction]            # 1-3 个动作
    history: HistoricalDisposition | None = None
    confidence: float
    source_refs: list[str]               # ["SOP:WELD-014@v3", "intercept:INT-..."]
    route_version: str
    generated_by: str                    # "cache_hit" / "llm_fallback" / "manual"
    disclaimer: str = "辅助信息，不改变拦截决策，处置需在正式界面执行"
    needs_human_review: bool = False
    created_at: datetime
```

- `reason_summary` 限单句（≤30 字），操作工扫一眼就懂。
- `actions` 限 1-3 个，`action_type` 固化为 Enum，避免 LLM 编造动作类型。
- `source_refs` 强制引用 SOP（带版本 `@v3`）+ 历史拦截 ID，可回溯。
- `disclaimer` 不可省，防止操作工误以为 RAG 能放行。

### 4.3 点检超期锁定的跨上下文回溯

MVP 的点检超期锁定是跨上下文链路，`CheckpointBlocked` 事件里只有粗原因 `EquipmentUnavailable`，需回溯精因：

```text
点检保养上下文: InspectionOverdue (pm.inspection.overdue)
        │
        ▼
台账上下文: MarkAssetUnavailable -> AssetAvailabilityChanged (eam.asset.availability)
        │
        ▼
过点执行上下文: EquipmentAvailabilityCache available=false
        │
        ▼
过点: CheckpointBlocked(blocking_reason=EquipmentUnavailable)  ← D 订阅这个
        │
        ▼
D 服务 ReasonEnricher 经 ACL 只读查询：
   ├─ 调台账/点检只读 REST: GET /api/eam/assets/{asset_id}/availability
   │   返回 available=false + blocking_reasons[] 含 "InspectionOverdue"
   └─ 调点检只读 REST: GET /api/pm/overdue?asset_id=
       返回 overdue_since + 责任人 + 任务 ID
        │
        ▼
精因 = 点检超期 -> 卡片处置: "设备 SMT-RF-03 因日点检超期锁定(已超4h)，联系设备工程师张三完成点检后自动解锁"
```

> 🔴 **契约待对齐：点检超期回溯的只读 REST 入口**。MVP 假设台账上下文提供 `GET /api/eam/assets/{asset_id}/availability`（返回 `available` + `blocking_reasons[]`）、点检保养上下文提供 `GET /api/pm/overdue?asset_id=`（返回 `overdue_since` + `assignee`）。这两条只读 REST 契约在台账/点检上下文事件风暴中未明确对外暴露查询入口，待与设备管理服务确认。MVP 兜底：若 REST 不可用，卡片降级为粗因"设备不可用，请联系设备工程师查详细原因"。

### 4.4 与 A/B RAG 的协同（MVP 兜底策略）

> 🔴 **MVP 依赖 A/B RAG 就绪度**。D 卡片生成需调文档型 RAG 拉 SOP 片段、追溯型 RAG 拉历史处置。MVP 阶段 A/B 可能未就绪，采用分级兜底：
> - **B 未就绪**：SOP 片段走**离线导入**（人工把高频拦截原因对应的 SOP 片段导入 `sop_fragment` 表，带 `route_version`），`DocRagAclClient` 降级查本地表。B 就绪后切换为实时 ACL 调用。
> - **A 未就绪**：历史同类处置走**本地历史拦截记录表**（`intercept_history`，D 服务自身消费 `CheckpointBlocked` 累积），`TraceRagAclClient` 降级查本地表。A 就绪后切换为实时检索。
>
> 这两条 gap 在 §7.3 ACL 与 §15 Q&A 都会讲到。MVP 优先验证"拦截事件 -> 缓存命中 -> 工位推送"闭环，A/B 就绪后平滑切换数据源。

---

## 5. 预计算与缓存治理

### 5.1 预计算预热（CardWarmer）

`CardWarmer` 离线把 MVP 3 类高频拦截的卡片打包入 Redis：

```text
预计算 Job（启动时 + 版本失效事件触发）
   │
   ├─ 扫描 MVP 3 类 blocking_reason × 当前 ACTIVATED route_version 的工位/产品组合
   ├─ 对每个 (blocking_reason, station_type, product_scope, route_version):
   │    ├─ 拉 SOP 片段（B ACL 或离线导入表）
   │    ├─ 拉历史处置（A ACL 或本地历史表）
   │    ├─ LLM 综合生成 AssistCard
   │    └─ 写 Redis（键 = 四元组，TTL = 工艺版本有效期）
   └─ 仅对 ACTIVATED 的 route_version 预热
```

- **触发时机**：① 服务启动全量预热当前生效版本；② `ProcessRouteActivated`/`QualityGateRuleActivated` 触发对应组合重算（§5.3）；③ 每班次开始刷新历史处置统计。
- **预热是尽力而为**：预热失败（A/B 不可用）不阻塞启动，运行时未命中走 LLM 兜底。预热覆盖率作为指标（§10.1）。

### 5.2 命中即推（运行时主路径）

```text
CheckpointBlocked 事件到达
   │
   ├─ 1. 幂等检查（event_id）
   ├─ 2. 解析缓存键（点检超期等需回溯精因，§6.2）
   ├─ 3. 查 Redis
   │     ├─ 命中 -> 补实例字段(sn/wo_id) -> WebSocket 推工位（毫秒级）
   │     └─ 未命中 -> 推占位卡片 -> 异步生成 -> 回填缓存 -> 补推正式卡片
   └─ 4. 幂等记录 + 位点推进
```

- **命中路径零 LLM**，毫秒级推送。
- **未命中不阻塞**：先推占位卡片（"处置生成中，请联系工程师"），操作工不干等。

### 5.3 缓存失效与版本一致性

| 失效事件 | 主题 | 缓存失效动作 |
|---------|------|-------------|
| `ProcessRouteActivated` | `process.route.lifecycle` | 删旧 `route_version` 卡片 + 预热新版本 |
| `ProcessRouteDeprecated` | `process.route.lifecycle` | 删该 `route_version` 所有卡片 |
| `QualityGateRuleActivated` | `quality.gate.lifecycle` | 失效质量类（`QualityGateFail`）卡片 + 重算 |
| `QualityGateDeprecated`(`superseded_by=null`) | `quality.gate.lifecycle` | 失效引用该规则的卡片 |
| `DefectCatalogDefined`/`Updated` | `quality.defect.catalog` | 失效质量类卡片 + 重算 |

> **版本一致性三道闸**：① 预热闸--只对 ACTIVATED 的 `route_version` 预热；② 失效闸--版本变更事件主动删旧缓存；③ 检索闸--命中时校验卡片 `route_version` == 过点 `route_version`，不一致重新生成。

### 5.4 幂等与去重

```sql
-- 幂等表：event_id + consumer_group 唯一键（对齐消息处理说明 §6 consumed_event 模式）
CREATE TABLE intercept_idempotency (
  event_id        VARCHAR(64)  NOT NULL,
  consumer_group  VARCHAR(64)  NOT NULL,
  topic           VARCHAR(128) NOT NULL,
  processed_at    DATETIME(3)  NOT NULL,
  PRIMARY KEY (event_id, consumer_group)
);

-- 位点表
CREATE TABLE intercept_offset (
  consumer_group  VARCHAR(64)  NOT NULL,
  topic           VARCHAR(128) NOT NULL,
  partition_no    INT          NOT NULL,
  offset_no       BIGINT       NOT NULL,
  updated_at      DATETIME(3)  NOT NULL,
  PRIMARY KEY (consumer_group, topic, partition_no)
);

-- 历史拦截记录表（A 未就绪时兜底，§4.4 🔴）
CREATE TABLE intercept_history (
  intercept_id    VARCHAR(64)  NOT NULL,
  event_id        VARCHAR(64)  NOT NULL,
  blocking_reason VARCHAR(64)  NOT NULL,
  station_id      VARCHAR(64)  NOT NULL,
  station_type    VARCHAR(32)  NOT NULL,
  product_scope   VARCHAR(32)  NOT NULL,
  route_version   VARCHAR(32)  NOT NULL,
  defect_code     VARCHAR(32)  NULL,
  disposition     VARCHAR(128) NULL,
  tenant_scope    VARCHAR(64)  NOT NULL,
  occurred_at     DATETIME(3)  NOT NULL,
  PRIMARY KEY (intercept_id),
  KEY idx_reason (blocking_reason, station_type, occurred_at),
  KEY idx_event (event_id)
);

-- 离线 SOP 片段表（B 未就绪时兜底，§4.4 🔴）
CREATE TABLE sop_fragment (
  fragment_id     VARCHAR(64)  NOT NULL,
  blocking_reason VARCHAR(64)  NOT NULL,
  station_type    VARCHAR(32)  NOT NULL,
  sop_ref         VARCHAR(128) NOT NULL,
  route_version   VARCHAR(32)  NOT NULL,
  content         TEXT         NOT NULL,
  tenant_scope    VARCHAR(64)  NOT NULL,
  updated_at      DATETIME(3)  NOT NULL,
  PRIMARY KEY (fragment_id),
  KEY idx_lookup (blocking_reason, station_type, route_version)
);
```

- **推送幂等**：`event_id` 幂等表挡重复事件；卡片 `card_id` 含 `event_id`，WebSocket 客户端按 `card_id` 去重。
- **位点管理**：手动 ack，推送事务（推 WebSocket + 幂等记录 + 位点更新）成功后才 ack offset。`enable.auto.commit=false`，避免"已 ack 未推"丢事件（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §12.2）。

---

## 6. 卡片生成与回溯

### 6.1 生成管线（CardGenerator）

```text
CardGenerator.generate(key, intercept_event)
   │
   ├─ 1. 拉处置知识（ACL 只读，A/B 未就绪降级本地表）
   │     ├─ DocRagAclClient.search_docs(blocking_reason + station_type, route_version) -> SopFragment[]
   │     └─ TraceRagAclClient.query_historical_disposition(blocking_reason) -> HistoricalDisposition
   ├─ 2. LLM 综合（with_structured_output(AssistCard) 强制 schema）
   │     系统提示词约束：一句话原因(≤30字) + 1-3 动作(枚举) + 引用 SOP + 不编造
   ├─ 3. 校验兜底
   │     ├─ Pydantic 校验失败 -> 重试 1 次
   │     ├─ 无 SOP / confidence < 0.6 -> needs_human_review=True，推转人工兜底
   │     └─ LLM 超时/失败 -> 推"处置生成失败，请联系工程师"兜底
   └─ 4. 回填缓存 + 返回
```

- **LLM 只做综合不做检索**：SOP 片段与历史处置由 ACL 拉，LLM 只压缩成卡片，降低幻觉。
- **结构化输出强约束**：`action_type` 固化为 Enum，`reason_summary` 限单句。

### 6.2 跨上下文回溯（ReasonEnricher）

`ReasonEnricher` 把粗原因回溯为精因，构造准缓存键与准处置：

```python
class ReasonEnricher:
    """粗 blocking_reason -> 精因，构造准缓存键。MVP 覆盖 EquipmentUnavailable 回溯点检超期。"""

    def __init__(self, asset_acl: AssetAclClient, pm_acl: PmAclClient) -> None:
        self._asset_acl = asset_acl; self._pm_acl = pm_acl

    async def enrich_key(self, event: DomainEvent) -> CardCacheKey:
        p = event.payload
        base = CardCacheKey(
            blocking_reason=p["blocking_reason"],
            station_type=p.get("station_type", ""),
            product_scope=p.get("product_scope", ""),
            route_version=p.get("route_version", ""),
        )
        # 点检超期回溯：EquipmentUnavailable -> 查台账 blocking_reasons
        if base.blocking_reason == "EquipmentUnavailable" and p.get("equipment_id"):
            avail = await self._asset_acl.fetch_availability(
                p["equipment_id"], self._tenant(event)
            )
            if avail and "InspectionOverdue" in (avail.blocking_reasons or []):
                overdue = await self._pm_acl.fetch_overdue(
                    p["equipment_id"], self._tenant(event)
                )
                # 精因进缓存键（同精因后续命中）
                base = base.model_copy(update={"blocking_reason": "EquipmentUnavailable:InspectionOverdue"})
                # overdue.assignee/overdue_since 透传给卡片生成
        return base
```

- 回溯是只读降级查询，不进过点主事务，超时降级为粗因卡片（§4.3 🔴）。

### 6.3 版本一致性保证

三道闸：① 预热闸（只对 ACTIVATED 预热）；② 失效闸（`VersionInvalidationHandler` 订阅版本事件删旧缓存）；③ 检索闸（命中校验 `route_version` 一致）。

---

## 7. 实现方案

### 7.1 卡片分发服务（AssistCardDispatcher）

```python
class AssistCardDispatcher:
    """消费 CheckpointBlocked，查缓存推工位，未命中转生成。"""

    def __init__(
        self,
        cache: CardCache,
        generator: CardGenerator,
        registry: StationSessionRegistry,
        enricher: ReasonEnricher,
        idem_repo: IdempotencyRepo,
        offset_repo: OffsetRepo,
        history_repo: InterceptHistoryRepo,
        metrics: MetricsCollector,
    ) -> None: ...

    async def consume(self, msg: ConsumerRecord, group: str) -> None:
        event = DomainEvent.model_validate_json(msg.value)
        if event.event_type != "CheckpointBlocked":
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 1. 幂等检查
        if await self._idem.exists(event.event_id, group):
            self._metrics.duplicate.inc()
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        p = event.payload
        # 2. 回溯精因 + 构造缓存键
        key = await self._enricher.enrich_key(event)
        station_id = p["station_id"]
        # 3. 记录历史拦截（A 未就绪时兜底数据源，§4.4 🔴）
        await self._history_repo.record(event, key)
        # 4. 查缓存
        card = await self._cache.get(key)
        if card:
            card = self._fill_instance(card, event)
            card.generated_by = "cache_hit"
            await self._registry.push_card(station_id, card)
            self._metrics.cache_hit.inc(key.blocking_reason)
        else:
            # 5. 未命中：先推占位，异步生成
            await self._registry.push_card(station_id, self._placeholder(event))
            self._metrics.cache_miss.inc(key.blocking_reason)
            asyncio.create_task(self._generate_and_push(key, event, station_id))
        # 6. 幂等记录 + 位点推进
        await self._idem.record(event.event_id, group, msg.topic)
        await self._offset.advance(group, msg.topic, msg.partition, msg.offset)

    def _fill_instance(self, card: AssistCard, event: DomainEvent) -> AssistCard:
        """补本次拦截实例字段（缓存里是模板，不带 sn/wo_id）。"""
        p = event.payload
        return card.model_copy(update={
            "card_id": f"CARD-{event.event_id}",
            "station_id": p["station_id"],
            "work_order_id": p.get("work_order_id", ""),
            "sn": p.get("sn"),
        })

    async def _generate_and_push(self, key, event, station_id) -> None:
        card = await self._generator.generate(key, event)
        if card:
            await self._cache.set(key, card)
            card = self._fill_instance(card, event)
            await self._registry.push_card(station_id, card)
        else:
            await self._registry.push_card(station_id, self._fallback(event))
            self._metrics.fallback.inc(key.blocking_reason)
```

- 分发与生成分离（SRP）；未命中不阻塞（先推占位）。

### 7.2 卡片生成器（CardGenerator）

```python
class CardGenerator:
    """未命中时拉 SOP + 历史处置，LLM 综合成卡片，回填缓存。"""

    def __init__(
        self,
        doc_rag: DocRagAclClient,
        trace_rag: TraceRagAclClient,
        llm: BaseChatModel,
        cache: CardCache,
    ) -> None: ...

    async def generate(self, key: CardCacheKey, event: DomainEvent) -> AssistCard | None:
        tenant = self._tenant(event)
        # 1. 拉处置知识（A/B 未就绪时 ACL 内部降级本地表，§4.4 🔴）
        sops = await self._doc_rag.search_docs(
            query=f"{key.blocking_reason} {key.station_type} 处置",
            route_version=key.route_version, tenant=tenant,
        )
        history = await self._trace_rag.query_historical_disposition(
            blocking_reason=key.blocking_reason, tenant=tenant
        )
        if not sops:
            return None  # 无 SOP -> 转人工兜底
        # 2. LLM 综合（结构化输出）
        prompt = self._build_prompt(key, event, sops, history)
        try:
            card = await self._llm.with_structured_output(AssistCard).ainvoke(prompt)
        except Exception:
            return None  # LLM 失败 -> 转人工兜底
        # 3. 校验置信度
        if card.confidence < 0.6:
            card.needs_human_review = True
        card.route_version = key.route_version
        card.generated_by = "llm_fallback"
        return card
```

### 7.3 ACL 防腐层（只读 REST 契约 + 降级）

| 协作方 | 只读 REST | 用途 | 降级 |
|--------|----------|------|------|
| 文档型 RAG | `GET /rag/docs/search?q=&route_version=` | 拉 SOP 片段 | B 未就绪 -> 查本地 `sop_fragment` 表（🔴 §4.4） |
| 追溯型 RAG | `GET /rag/trace/history?blocking_reason=` | 拉历史同类处置 | A 未就绪 -> 查本地 `intercept_history` 表（🔴 §4.4） |
| 台账上下文 | `GET /api/eam/assets/{asset_id}/availability` | 回溯设备锁定原因 | 🔴 契约待对齐（§4.3） |
| 点检保养上下文 | `GET /api/pm/overdue?asset_id=` | 回溯点检超期详情 | 🔴 契约待对齐（§4.3） |

```python
class DocRagAclClient:
    """文档型 RAG 只读 ACL，强制 route_version 过滤。B 未就绪降级本地表。"""

    def __init__(self, http: httpx.AsyncClient, fallback: SopFragmentRepo) -> None:
        self._http = http; self._fallback = fallback

    async def search_docs(
        self, query: str, route_version: str, tenant: TenantContext
    ) -> list[SopFragment]:
        if not route_version:
            raise ValueError("route_version 必填，禁止查无版本 SOP（版本一致性）")
        try:
            resp = await self._http.get(
                "/rag/docs/search", params={"q": query, "route_version": route_version},
                headers=tenant.headers(), timeout=2.0,
            )
            resp.raise_for_status()
            return [SopFragmentMapper.to_view(d) for d in resp.json()]
        except Exception:
            # 降级：查本地离线导入表（🔴 §4.4，B 未就绪兜底）
            return await self._fallback.find(query, route_version, tenant)


class AssetAclClient:
    """台账上下文只读 ACL，回溯设备锁定原因。🔴 契约待对齐（§4.3）。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def fetch_availability(
        self, asset_id: str, tenant: TenantContext
    ) -> AvailabilityView | None:
        try:
            resp = await self._http.get(
                f"/api/eam/assets/{asset_id}/availability",
                headers=tenant.headers(), timeout=1.5,
            )
            resp.raise_for_status()
            return AvailabilityMapper.to_view(resp.json())
        except Exception:
            return None  # 降级：粗因卡片


class TraceRagAclClient:
    """追溯型 RAG 只读 ACL，拉历史同类处置。A 未就绪降级本地表。"""

    def __init__(self, http: httpx.AsyncClient, fallback: InterceptHistoryRepo) -> None:
        self._http = http; self._fallback = fallback

    async def query_historical_disposition(
        self, blocking_reason: str, tenant: TenantContext
    ) -> HistoricalDisposition | None:
        try:
            resp = await self._http.get(
                "/rag/trace/history", params={"blocking_reason": blocking_reason},
                headers=tenant.headers(), timeout=2.0,
            )
            resp.raise_for_status()
            return HistoricalMapper.to_view(resp.json())
        except Exception:
            # 降级：查本地历史拦截表（🔴 §4.4，A 未就绪兜底）
            return await self._fallback.summarize(blocking_reason, tenant)
```

- 外部 DTO 不进卡片核心，只暴露 `SopFragment`/`AvailabilityView`/`HistoricalDisposition`（ACL 约束）。
- 降级查询是兜底，不进过点主事务，超时降级为粗因/兜底卡片。

### 7.4 版本失效处理器（VersionInvalidationHandler）

```python
class VersionInvalidationHandler:
    """订阅版本失效事件，删旧缓存片段 + 预热新版本。"""

    def __init__(
        self, cache: CardCache, warmer: CardWarmer,
        idem_repo: IdempotencyRepo, offset_repo: OffsetRepo,
    ) -> None: ...

    async def consume(self, msg: ConsumerRecord, group: str) -> None:
        event = DomainEvent.model_validate_json(msg.value)
        if await self._idem.exists(event.event_id, group):
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        if event.event_type == "ProcessRouteActivated":
            p = event.payload
            await self._cache.invalidate_route_version(p.get("deprecated_version", ""))
            asyncio.create_task(self._warmer.warm_route(p["route_id"], p["route_version"]))
        elif event.event_type == "QualityGateRuleActivated":
            await self._cache.invalidate_reason("QualityGateFail")
            asyncio.create_task(self._warmer.warm_reason("QualityGateFail"))
        elif event.event_type == "DefectCatalogDefined":
            await self._cache.invalidate_reason("QualityGateFail")
        await self._idem.record(event.event_id, group, msg.topic)
        await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
```

---

## 8. 推荐包结构（Python src layout）

```text
assist_service/
  app/
    api/
      ws_router.py             # WebSocket /ws/station/{id}
      admin_router.py          # /admin/card/refresh
      schemas.py
    application/
      card_dispatcher.py       # AssistCardDispatcher
      card_generator.py        # CardGenerator
      card_warmer.py           # CardWarmer 预热 Job
      reason_enricher.py       # ReasonEnricher 跨上下文回溯
    domain/
      card.py                  # AssistCard / CardAction / HistoricalDisposition / ActionType
      cache_key.py             # CardCacheKey
      intercept_reason.py      # blocking_reason 枚举
      tenant.py                # TenantContext
      projection.py            # VersionInvalidationHandler 协议 / ReadOnlyIngestionGate
    infrastructure/
      redis_/
        card_cache.py          # CardCache（命中即推主存）
        session_registry.py    # StationSessionRegistry（WebSocket 会话）
      ai/
        llm_factory.py
      embedding/
        bge_client.py          # 仅未命中兜底
      acl/
        doc_rag.py             # 文档型 RAG search_docs（降级本地表）
        trace_rag.py           # 追溯型 RAG 历史处置（降级本地表）
        asset_acl.py           # 台账回溯精因（🔴 §4.3）
        pm_acl.py              # 点检回溯精因（🔴 §4.3）
      kafka/
        consumer_group.py
        listeners.py
      persistence/
        models.py              # intercept_idempotency / intercept_offset / intercept_history / sop_fragment
        idempotency_repo.py
        offset_repo.py
        history_repo.py
        sop_fragment_repo.py
      obs/
        tracing.py
        metrics.py
    config.py
    main.py                    # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
  Dockerfile
  docker-compose.yml
```

- `domain/intercept_reason.InterceptReason` 是有限集枚举（ISP），每个原因可挂自己的回溯策略。
- `infrastructure/acl/` 是防腐层，调 A/B RAG 与各上下文只读 REST，降级本地表，外部 DTO 经 Mapper 转内部视图。
- `application/card_warmer` 与 `card_generator` 共用生成逻辑（SRP）。

---

## 9. 关键代码骨架

### 9.1 订阅拓扑与位点管理

按主题分消费者组，MVP 涉及：

| 消费者组 | 订阅主题 | 用途 |
|---------|---------|------|
| `assist-intercept` | `mes.checkpoint.lifecycle` | 拦截事件主路径（过滤 `CheckpointBlocked`） |
| `assist-version` | `process.route.lifecycle`, `quality.gate.lifecycle`, `quality.defect.catalog` | 缓存失效刷新 |

- **位点落 MySQL**：`intercept_offset` 表，重启从断点续跑（§5.4）。
- **手动 ack**：推送事务成功后才 ack offset。
- **`enable.auto.commit=false`**：严禁自动提交（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §12.2）。

### 9.2 卡片缓存（CardCache）

```python
class CardCache:
    """Redis 卡片缓存。键 = 四元组 JSON，值 = AssistCard JSON。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, k: CardCacheKey) -> str:
        return f"assist:card:{k.blocking_reason}:{k.station_type}:{k.product_scope}:{k.route_version}"

    async def get(self, k: CardCacheKey) -> AssistCard | None:
        raw = await self._redis.get(self._key(k))
        if not raw:
            return None
        card = AssistCard.model_validate_json(raw)
        # 检索闸：校验 route_version 一致（不一致视为未命中）
        if card.route_version != k.route_version:
            return None
        return card

    async def set(self, k: CardCacheKey, card: AssistCard, ttl: int = 86400) -> None:
        card.route_version = k.route_version
        await self._redis.set(self._key(k), card.model_dump_json(), ex=ttl)

    async def invalidate_route_version(self, route_version: str) -> None:
        async for key in self._redis.scan_iter(f"assist:card:*:*:*:{route_version}"):
            await self._redis.delete(key)

    async def invalidate_reason(self, blocking_reason: str) -> None:
        async for key in self._redis.scan_iter(f"assist:card:{blocking_reason}:*"):
            await self._redis.delete(key)
```

### 9.3 WebSocket 推送（StationSessionRegistry）

```python
class StationSessionRegistry:
    """工位 WebSocket 会话注册表，按 station_id 路由卡片。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._sessions: dict[str, set[WebSocket]] = {}  # station_id -> ws 集合（多终端）

    async def register(self, station_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._sessions.setdefault(station_id, set()).add(ws)
        # 补推暂存卡片（断线期间未读）
        async for raw in self._redis.lrange(f"assist:pending:{station_id}", 0, -1):
            await ws.send_text(raw)
        await self._redis.delete(f"assist:pending:{station_id}")

    async def unregister(self, station_id: str, ws: WebSocket) -> None:
        self._sessions.get(station_id, set()).discard(ws)

    async def push_card(self, station_id: str, card: AssistCard) -> bool:
        payload = card.model_dump_json()
        sessions = self._sessions.get(station_id, set())
        if not sessions:
            # 会话不在线：暂存 Redis 待补推（短 TTL）
            await self._redis.rpush(f"assist:pending:{station_id}", payload)
            await self._redis.expire(f"assist:pending:{station_id}", 300)
            return False
        for ws in sessions:
            await ws.send_text(payload)
        return True
```

- 多终端支持（一工位多屏幕）；会话不在线暂存补推（§7.1）。

### 9.4 预热 Job（CardWarmer）

```python
class CardWarmer:
    """离线预热高频拦截卡片。与 CardGenerator 共用生成逻辑。"""

    def __init__(self, generator: CardGenerator, cache: CardCache, settings: Settings) -> None:
        self._generator = generator; self._cache = cache; self._settings = settings

    async def warm_all(self, active_routes: list[RouteBrief]) -> None:
        """启动时全量预热当前 ACTIVATED 版本。"""
        for reason in self._settings.mvp_blocking_reasons:  # ["QualityGateFail", ...]
            for route in active_routes:
                for combo in self._enumerate_combos(reason, route):
                    key = CardCacheKey(blocking_reason=reason, **combo, route_version=route.route_version)
                    if await self._cache.get(key):
                        continue  # 已预热
                    card = await self._generator.generate_template(key, combo)
                    if card:
                        await self._cache.set(key, card)

    async def warm_route(self, route_id: str, route_version: str) -> None:
        """ProcessRouteActivated 触发新版本预热。"""
        ...

    async def warm_reason(self, blocking_reason: str) -> None:
        """QualityGateRuleActivated 触发该原因重算。"""
        ...
```

### 9.5 启动断言（只读校验）

```python
# app/domain/projection.py
class ReadOnlyIngestionGate(Exception):
    """启动时发现写 MES 的调用，拒绝启动。"""

# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    registry = app.state.acl_registry
    # 启动断言：所有 ACL 客户端只读，无写 MES 接口
    registry.assert_read_only()            # 扫描 ACL 方法，禁止 POST/PUT/DELETE 到 MES
    # 启动断言：消费者组只订拦截 + 版本事件，未误订其他
    registry.assert_no_write_topic()
    # 预热高频卡片（尽力而为，失败不阻塞）
    await app.state.warmer.warm_all(await app.state.route_repo.list_active())
    async with app.state.kafka_consumer_groups as groups:
        for g in groups:
            asyncio.create_task(g.run())
        yield
```

- `assert_read_only` 扫描所有 ACL 客户端，禁止任何写 MES 的 HTTP 方法（POST/PUT/DELETE）--红线靠启动断言兜底（与追溯型 `ReadOnlyProjectionGate`、L1 `ReadOnlyToolGate` 同思路）。
- 预热失败不阻塞启动（尽力而为）。

### 9.6 FastAPI 入口

```python
# app/api/ws_router.py
router = APIRouter()

@router.websocket("/ws/station/{station_id}")
async def station_ws(
    ws: WebSocket, station_id: str,
    token: str = Query(...),
    registry: StationSessionRegistry = Depends(get_registry),
    tenant: TenantContext = Depends(tenant_from_token),
):
    # 权限校验：token 的 tenant_scope 与 station_id 归属
    if not tenant.owns_station(station_id):
        await ws.close(code=4403)
        return
    await registry.register(station_id, ws)
    try:
        while True:
            await ws.receive_text()  # 保持连接（可接收心跳/ack）
    except WebSocketDisconnect:
        await registry.unregister(station_id, ws)
```

- WebSocket 握手校验 `tenant_scope` 与工位归属，防止跨车间订阅。

### 9.7 配置与部署

```python
# app/config.py
class Settings(BaseSettings):
    # Redis
    redis_url: str = "redis://redis:6379/0"
    # MySQL
    mysql_dsn: str = "mysql+asyncmy://root:root@mysql:3306/assist?charset=utf8mb4"
    # Kafka
    kafka_bootstrap: str = "kafka:9092"
    # A/B RAG（未就绪时降级本地表）
    doc_rag_base_url: str = "http://doc-rag-service:8000"
    trace_rag_base_url: str = "http://rag-service:8000"
    # 各上下文只读 REST
    eam_base_url: str = "http://eam-service:8080"
    pm_base_url: str = "http://eam-service:8080"
    # LLM
    llm_provider: str = "deepseek"  # 可切换 claude/qwen/local
    llm_api_key: str = ""
    # MVP 拦截原因
    mvp_blocking_reasons: list[str] = ["QualityGateFail", "EquipmentUnavailable", "FirstArticleBlocked"]
    # 缓存
    card_ttl_seconds: int = 86400
    confidence_threshold: float = 0.6

    class Config:
        env_prefix = "ASSIST_"
```

```yaml
# docker-compose.yml（MVP 本地起）
version: "3.9"
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: assist
    ports: ["3306:3306"]
    volumes:
      - ./sql/init.sql:/docker-entrypoint-initdb.d/init.sql
  assist-service:
    build: .
    depends_on: [redis, mysql]
    environment:
      ASSIST_REDIS_URL: redis://redis:6379/0
      ASSIST_MYSQL_DSN: mysql+asyncmy://root:root@mysql:3306/assist?charset=utf8mb4
      ASSIST_KAFKA_BOOTSTRAP: kafka:9092
    ports: ["8002:8000"]
  # bge-m3 embedding（本地化，可选）
  embedding:
    image: ghcr.io/huggingface/text-embeddings:latest
    ports: ["8080:80"]
```

- MVP 用 `docker-compose` 本地起 Redis + MySQL + assist-service，验证"拦截事件 -> 缓存命中 -> 工位推送"闭环后再上 K8s。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `assist_intercept_total` | 拦截事件数（按 blocking_reason label） |
| `assist_cache_hit_total` / `assist_cache_miss_total` | 缓存命中/未命中 |
| `assist_cache_hit_ratio` | 缓存命中率（健康度核心，目标 >95%） |
| `assist_card_push_latency_seconds` | 卡片推送延迟（事件到达到推送，Histogram） |
| `assist_llm_generate_latency_seconds` | LLM 生成延迟（仅未命中路径） |
| `assist_low_confidence_total` | 低置信度转人工次数 |
| `assist_fallback_total` | 兜底卡片次数（无 SOP/LLM 失败） |
| `assist_version_invalidation_total` | 版本失效刷新次数 |
| `assist_ws_session_active` | 在线工位会话数 |
| `assist_warmer_coverage_ratio` | 预热覆盖率 |

### 10.2 trace 串联

- 每次拦截处理一个 `trace_id`，从 Kafka 事件头透传，OpenTelemetry 在 `AssistCardDispatcher`、ACL、LLM、WebSocket 推送都注入 span。
- 卡片 `source_refs` + `card_id` 让工程师从工位卡片回溯到拦截事件、SOP 文档、历史拦截记录。

### 10.3 兜底

- **缓存未命中**：先推占位卡片，异步生成后补推；生成失败推"转人工"兜底。
- **LLM 失败/低置信度**：`confidence < 0.6` 或 LLM 异常 -> `needs_human_review` 或兜底卡片，不硬答。
- **A/B RAG 不可用**：ACL 降级本地表（§4.4 🔴）；本地表也无 -> 推"转人工"兜底。
- **WebSocket 断线**：会话不在线暂存 Redis（5 分钟 TTL），重连补推。
- **D 服务故障**：不影响过点（过点早已在主事务内拦截），D 恢复后从 Kafka 位点续跑。

---

## 11. 实现步骤

### 阶段一：骨架与拦截事件订阅（1 周）

1. 搭 `assist_service` 骨架（FastAPI + uvicorn），对齐 §8 包结构。
2. 接 aiokafka 订阅 `mes.checkpoint.lifecycle`，过滤 `CheckpointBlocked`，幂等表 + 位点表跑通（§5.4）。
3. 实现 `StationSessionRegistry` + WebSocket 端点（§9.3、§9.6）。
4. 实现 `AssistCardDispatcher` 主路径（查 Redis 命中即推，未命中推占位）（§7.1）。
5. 实现 `ReadOnlyIngestionGate` 启动断言（§9.5）。

### 阶段二：预计算与缓存治理（1-2 周）

6. 实现 `CardCache`（Redis 四元组键 + TTL + 失效）（§9.2）。
7. 实现 `CardWarmer` 预热 Job（§9.4）。
8. 实现 `VersionInvalidationHandler` 订阅 `process.*`/`quality.*` 失效刷新（§7.4）。
9. 验证版本一致性：工艺升版后旧卡片失效、新版本首次生成回填。

### 阶段三：卡片生成与 ACL（1-2 周）

10. 实现 `CardGenerator`（拉 SOP + 历史 + LLM 综合 + 结构化输出）（§7.2）。
11. 实现 `DocRagAclClient`/`TraceRagAclClient` ACL（含本地表降级）（§7.3）。
12. 实现 `ReasonEnricher` 点检超期回溯精因（§6.2、§4.3 🔴）。
13. 接 OpenTelemetry + prometheus 指标（§10.1），缓存命中率告警。

### 阶段四：加固、评测与试点（1 周）

14. 沉淀评测集（3 类拦截场景 + 预期卡片），回归模型/提示词变更。
15. 兜底链路全测（无 SOP/LLM 失败/低置信度/WebSocket 断线/A/B 不可用）。
16. 灰度一个高频拦截场景（如 AOI 质量门禁拦截）试点，收集操作工反馈。
17. 确认 🔴 决策点（§4.3 点检 REST 契约、§4.4 A/B 就绪切换、§4.1 其余拦截扩展）。

---

## 12. 约束落地检查清单

- [ ] D 服务不进过点主事务（§1.2），过点 P99 ≤200ms 不受影响；卡片推送允许秒级延迟。
- [ ] 主判定走规则引擎，RAG 只给处置；每张卡片带 `disclaimer`"不改变拦截决策"。
- [ ] 预计算 + 缓存命中即推（§5.2），稳态命中率目标 >95%；未命中走 LLM 兜底。
- [ ] `event_id + consumer_group` 幂等表，重复投递不重复推卡片；卡片 `card_id` 客户端去重。
- [ ] 消费者位点落 MySQL，重启从断点续跑，推送事务成功后才 ack offset；`enable.auto.commit=false`。
- [ ] 缓存键含 `route_version`；版本失效事件触发缓存片段失效重算（§5.3）。
- [ ] 命中时校验卡片 `route_version` == 过点 `route_version`，不一致重新生成（检索闸）。
- [ ] D 服务无任何写 MES 接口；`ReadOnlyIngestionGate` 启动断言禁止写调用。
- [ ] WebSocket 会话绑定 `(station_id, tenant_scope)`，握手校验权限；历史查询前置 `tenant_scope` 过滤。
- [ ] LLM 输出经 Pydantic `AssistCard` 校验，`action_type` 固化为 Enum，`reason_summary` 限单句。
- [ ] 无 SOP / LLM 失败 / `confidence < 0.6` -> 推"转人工"兜底卡片，不硬答。
- [ ] A/B RAG 不可用时 ACL 降级本地表（`sop_fragment`/`intercept_history`），B/A 就绪后切换。
- [ ] 卡片带 `source_refs`（SOP 带版本 + 历史拦截 ID），证据可回溯。
- [ ] D 服务故障不影响过点；WebSocket 断线暂存补推。

---

## 13. 面试防守 Q&A

**Q：防错即时辅助 RAG 怎么做到既进生产主线又不拖慢过点？**
A：核心是**拦后异步 + 预计算缓存**。过点 P99 ≤200ms 是硬约束，RAG 绝不进过点主事务--拦截决策由规则引擎在主事务内完成，D 服务订阅 `CheckpointBlocked` 是拦后异步消费。延迟靠预计算缓存兜住：拦截原因是有限集（`QualityGateFail`/`EquipmentUnavailable`/`FirstArticleBlocked` 等），预先把"原因 + SOP 片段 + 历史处置"打包成卡片入 Redis，过点拦截事件到达时按四元组键 `(blocking_reason, station_type, product_scope, route_version)` 查命中，命中即 WebSocket 推送（毫秒级）。稳态 99% 走缓存，LLM 只兜底首次未命中。D 服务挂了，过点照常拦截，只是工位少了处置建议--绝不影响生产主线。

**Q：MVP 选了哪三类拦截场景？为什么？**
A：选了质量门禁拦截（AOI NG 的 `QualityGateFail`）、点检超期锁定（`EquipmentUnavailable` 回溯点检超期）、首件未放行（`FirstArticleBlocked`）三类高频场景。原因：① 这三类是车间最高频的拦截，操作工最需要"为什么拦 + 怎么办"的即时辅助；② 覆盖了三种不同的回溯模式--质量类直接带缺陷码（自动分流返修）、设备类需跨上下文回溯精因（点检超期链路）、首件类是门禁状态类；③ 验证了"事件驱动 + 缓存命中 + 工位推送"完整闭环。其余拦截（物料齐套/跳站/设备数据超时）按相同范式扩展。

**Q：点检超期锁定这种跨上下文的拦截怎么处理？**
A：过点事件里只有粗原因 `EquipmentUnavailable`，D 需回溯精因。链路是：点检保养发 `pm.inspection.overdue` -> 台账 `MarkAssetUnavailable` -> 过点 `EquipmentAvailabilityCache` available=false -> 过点 `CheckpointBlocked(EquipmentUnavailable)`。D 的 `ReasonEnricher` 经 ACL 只读查台账 `GET /api/eam/assets/{asset_id}/availability`（返回 `blocking_reasons[]` 含 `InspectionOverdue`）+ 点检 `GET /api/pm/overdue?asset_id=`（返回 `overdue_since` + 责任人），精因进缓存键，处置为"联系设备工程师完成点检后自动解锁"。回溯是只读降级查询，不进过点主事务，超时降级为粗因卡片。🔴 这两条只读 REST 契约待与设备管理服务确认（§4.3）。

**Q：MVP 阶段文档型/追溯型 RAG 没建好怎么办？**
A：分级兜底。B（文档型）未就绪时，SOP 片段走离线导入表（人工把高频拦截原因对应的 SOP 片段导入 `sop_fragment` 表，带 `route_version`），`DocRagAclClient` 降级查本地表；A（追溯型）未就绪时，历史同类处置走本地 `intercept_history` 表（D 自身消费 `CheckpointBlocked` 累积）。B/A 就绪后切换为实时 ACL 调用，零代码改动（ACL 内部降级逻辑切换）。MVP 优先验证"拦截 -> 缓存命中 -> 工位推送"闭环，A/B 就绪后平滑切换数据源。这就是"先 B 后 D"的依赖关系（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。

**Q：缓存里的卡片是模板，怎么处理每次拦截的实例数据？**
A：模板与实例分离。缓存里的卡片不带 `sn`/`work_order_id`/`card_id`（这些是本次拦截实例字段），推送时 `AssistCardDispatcher._fill_instance` 补本次拦截的 `sn`/`work_order_id`，`card_id` 用 `f"CARD-{event_id}"` 保证幂等。这样同一缓存模板可复用于所有同四元组的拦截实例，缓存命中率最大化。WebSocket 客户端按 `card_id` 去重，断线重连补推时不重复展示。

**Q：推的处置错了怎么办？**
A：三重兜底。一是卡片标注 `disclaimer`"辅助信息，不改变拦截决策，处置需在正式界面执行"--RAG 不代劳任何写操作。二是 LLM 只做综合不做检索，输入是 ACL 拉的真实 SOP 片段，降低幻觉。三是 `confidence < 0.6` 或无 SOP 片段时推"转人工"兜底卡片，不硬答。`action_type` 固化为 Enum，LLM 编不出乱七八糟的动作类型。MES 领域错答案零容忍，宁可让人判。

**Q：上线了吗？**
A：这是设计阶段规划，不是已落地。重点是三条架构判断：① 不进过点主事务、拦后异步推、主判定走规则引擎；② 拦截原因有限集，预计算 + 缓存命中即推把稳态做到毫秒级，LLM 只兜底首次未命中；③ 版本一致性从领域模型兜上来，订阅 `ProcessRouteActivated`/`QualityGateRuleActivated` 刷新缓存片段。MVP 选 3 类高频拦截场景验证闭环，依赖文档型 RAG 先就绪（SOP 片段数据源）。诚实 + 体现架构判断力，比硬吹"已上线防错 AI"得分高。

---

## 14. 一句话定位

"防错即时辅助 RAG 把 RAG 从旁边问答变成现场防错副驾--过点引擎判 `CheckpointBlocked` 时，D 服务拦后异步把'一句话原因 + 一个动作'的短卡片推给工位屏幕。MVP 覆盖质量门禁拦截、点检超期锁定、首件未放行三类高频场景：靠拦截原因有限集做预计算 + Redis 缓存命中即推（稳态毫秒级），LLM 只兜底首次未命中；点检超期经 ACL 回溯台账/点检精因；A/B RAG 未就绪时降级本地表；卡片引用 SOP 带 `route_version`、订阅版本失效事件刷新缓存；低置信度或无 SOP 转人工兜底--全程不进过点主事务（P99 ≤200ms 硬约束）、不写 MES、不改变拦截决策，是让 RAG 能进生产主线的防错副驾。"

---

## 15. 与 A/B RAG、各上下文的契约对齐与待办

| 契约 | 状态 | 待办 |
|------|------|------|
| 文档型 RAG `GET /rag/docs/search` | 🔴 B 未就绪时降级本地 `sop_fragment` 表 | B 就绪后切换实时 ACL |
| 追溯型 RAG `GET /rag/trace/history` | 🔴 A 未就绪时降级本地 `intercept_history` 表 | A 就绪后切换实时 ACL |
| 台账 `GET /api/eam/assets/{id}/availability` | 🔴 契约待对齐 | 与设备管理服务确认只读查询入口 |
| 点检 `GET /api/pm/overdue?asset_id=` | 🔴 契约待对齐 | 与设备管理服务确认只读查询入口 |
| 缓存键粒度（是否进 `product_scope`） | 🔴 待确认 | 按命中率数据调整（详细设计 §4.3） |
| 其余拦截原因扩展 | ⏳ §11 | 物料齐套/跳站/设备数据超时等按同范式扩展 |
