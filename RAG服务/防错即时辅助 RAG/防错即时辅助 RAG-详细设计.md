# 防错即时辅助 RAG 详细设计（事件驱动拦截推送 + 预计算缓存）

> 本文是 [RAG服务引入路线.md](../RAG服务引入路线.md) §2.4 路线 D（防错即时辅助 RAG）的落地展开，输出**技术栈、拦截场景建模、预计算缓存治理、辅助卡片生成、工位推送架构、包结构、关键代码骨架与约束落地**。
> **技术栈**：Python（FastAPI + Redis + LlamaIndex + Pydantic）。RAG 服务与三大 MES 服务（Java/Spring）跨语言共存，通过 Kafka 只读事件 + WebSocket 推送解耦，互不侵入。
> **口径纪律**：防错即时辅助 RAG 在本项目中是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../../面试指南/项目亮点与指标卡片.md) §0 的口径纪律--说"规划方向 / 设计取舍"，不说"我们已经做了防错 RAG"。MES 领域对错误答案零容忍（错给一条已失效处置会直接导致批量不良或安全事故），所以本文强调**拦后异步推送 + 预计算命中即推 + 主判定仍走规则引擎**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标

把 RAG 从"操作工旁边一个问答工具"变成"**现场防错副驾**"--不是等操作工主动问，而是**过点拦截事件触发** RAG：过点引擎判拦截时（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §2.1 的 `CheckpointBlocked`），自动拉取**拦截原因 + 对应 SOP 片段 + 历史同类处置**，打包成**一句话原因 + 一个动作**的短卡片，**推给工位屏幕**。

典型场景：

1. **质量门禁拦截**：单件过 AOI 站被 `QualityGateFail` 拦截 -> 工位屏幕弹出"AOI 判 NG（桥接缺陷），按 SOP-WELD-014 处置：目检确认 -> 返修工位分流。近 7 天同类拦截 12 次，其中 8 次确认锡膏批次问题"
2. **点检超期锁定**：回流焊点检超期，台账 `MarkAssetUnavailable` -> 过点 `EquipmentUnavailable` 拦截 -> 弹出"设备 SMT-RF-03 因日点检超期锁定（已超 4h），联系设备工程师张三完成点检后自动解锁"
3. **首件未放行**：换线后首件未放行，过点 `FirstArticleBlocked` -> 弹出"工单 WO-1234 首件未放行，需质量工程师完成首件检验（当前进度 3/5 站）"
4. **设备数据超时**：关键工序设备实时数据查询超时，保守拦截 -> 弹出"回流焊温区数据查询超时，可申请工程师授权放行（带原因）或等待数据恢复"

### 1.2 硬边界（一开口就要讲）

| 边界 | 说明 | 落地 |
|------|------|------|
| **拦后异步，不进过点主事务** | 过点判定（含拦截决策）仍在过点主事务内由规则引擎完成；RAG 只在**拦截事件发布之后**异步消费、异步推送 | 过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）不受 RAG 影响；卡片到达工位允许秒级延迟（操作工此时已看到拦截红，几秒后弹处置） |
| **主判定走规则引擎，RAG 只给处置** | 放行/拦截决策永远是过点引擎基于 `QualityGateCache`/`EquipmentAvailabilityCache` 的规则判定；RAG 不参与判定，只解释"为什么拦 + 怎么办" | RAG 推送的卡片明确标注"辅助信息，不改变拦截决策"；处置动作需操作工/工程师在正式界面执行 |
| **预计算 + 缓存命中即推** | 拦截规则是有限集（`blocking_reason` 枚举有限），预先把"原因 + SOP 片段 + 历史处置"打包成卡片缓存，命中即推 | 缓存键 = `(blocking_reason, station_type, product_scope, route_version)`；命中即 WebSocket 推送，未命中走轻量 LLM 生成后回填缓存 |
| **只读 MES，只读文档** | RAG 只读 MES 事件 + 只读文档型 RAG 的 SOP 片段，不写任何 MES 数据；处置动作的"写"由人在正式界面做 | D 服务无任何写 MES 的接口；`ReadOnlyIngestionGate` 启动断言禁止任何写 MES 的调用 |
| **版本一致性** | 处置卡片引用的 SOP 片段必须带 `route_version`，工艺/规则/缺陷字典变更时刷新对应缓存片段 | 订阅 `ProcessRouteActivated`/`QualityGateRuleActivated`/`DefectCatalogDefined` 触发缓存片段失效与重算 |
| **权限隔离** | 卡片只推给拦截发生工位的会话；历史处置数据带 `tenant_scope` 过滤 | WebSocket 会话绑定 `(station_id, tenant_scope)`；历史查询前置 `tenant_scope` 过滤 |
| **可观测兜底** | 每张卡片带来源引用（SOP 文档/历史拦截 ID）+ 置信度；低置信度或不命中时推"转人工"提示而非硬答 | `confidence < 0.6` 或缓存未命中且生成失败 -> 推"请联系工艺/设备工程师"兜底卡片；与 MES 防错理念一致：宁可让人判 |

### 1.3 与过点执行上下文的关系（核心边界）

D 服务的触发源是过点执行上下文的 `CheckpointBlocked` 事件（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §2.1、对外契约 `mes.checkpoint.lifecycle`）。两者是**生产者-消费者**的只读解耦关系：

| 维度 | 过点执行上下文（事实源） | 防错即时辅助 RAG（本文） |
|------|----------------------|------------------------|
| 职责 | 判定放行/拦截 + 记录过点事实 | 解释拦截原因 + 给处置建议 |
| 时序 | 拦截决策在过点主事务内（≤200ms） | 拦截事件发布后异步消费（秒级延迟） |
| 权威性 | 拦截决策权威（规则引擎） | 处置建议辅助（不改变决策） |
| 数据流 | 发布 `CheckpointBlocked` | 订阅 `CheckpointBlocked`（只读） |

> 这条边界要讲清楚：**过点主事务里没有任何 RAG 调用**。拦截是规则引擎判的，RAG 只是在拦截**之后**异步把"为什么 + 怎么办"推给工位。操作工先看到拦截红（规则引擎同步给出），几秒后看到处置卡片（RAG 异步推送）。如果 RAG 服务挂了，过点照常拦截，只是工位少了处置建议--绝不影响生产主线。

### 1.4 与追溯型 RAG、文档型 RAG、L1 Agent 的关系

| 协同方 | 关系 | 数据流 |
|--------|------|--------|
| **文档型 RAG（路线 B）** | D 的 SOP 片段来源 | D 卡片生成时调 B 的 `search_docs(query, route_version_filter)` 拉 SOP 片段；预计算阶段批量拉取高频拦截原因的 SOP 片段入缓存 |
| **追溯型 RAG（路线 A）** | D 的历史同类处置来源 | D 卡片的"历史同类处置"调 A 的检索（按 `blocking_reason` + `defect_code` 查历史拦截记录与处置结果） |
| **L1 诊断型 Agent** | 分层，不重复 | D 是**操作工侧实时短卡片**（一句话原因+一个动作，秒级）；L1 是**工程师侧深度诊断**（多步 ReAct 推理，数十秒）。D 推卡片时可附"如需深入诊断，工程师可用 L1"的入口 |

- **先 B 后 D**：D 依赖 B 的 SOP 片段检索能力（[RAG服务引入路线.md](../RAG服务引入路线.md) §3"先 B 后 A，D 作为高价值试点"）。B 没建起来时，D 的 SOP 片段只能离线导入，无法随工艺版本自动刷新。
- **D 不替代 L1**：操作工看不懂长答案，工位要的是短卡片（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 面试 Q&A"操作工看不懂 LLM 长答案怎么办"）。D 把答案压缩成卡片，L1 留给工程师深度追问。

### 1.5 与 Java 技术栈的关系

- D 服务用 Python，**不替换** MES 三大服务的 Java/Spring 栈--只订阅 Kafka 只读事件、调只读 REST（文档型/追溯型 RAG）、WebSocket 推送工位。
- 跨语言物理边界天然强制只读：D 服务无法共享 Java 事务/内存，无法进过点主事务、无法旁路应用服务写路径。
- 复用 [实现说明](../../实现说明/) 既有的 Kafka topic、领域事件 envelope（`event_id`/`event_type`/`occurred_at`/`partition_key`，见 [消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §4.3）、消费侧幂等模式，不造新契约。

---

## 2. 技术栈

### 2.1 选型总览

| 层次 | 选型 | 选型理由 |
|------|------|---------|
| 语言 | Python 3.11+ | 类型提示 + Pydantic 校验，AI 生态最成熟，与文档型/追溯型 RAG 同栈复用 LLM 抽象 |
| Web 框架 | **FastAPI** | 异步、原生 OpenAPI；同时承载 WebSocket 推送端点与卡片生成 HTTP 入口 |
| 缓存 | **redis-py (async)** | 卡片缓存主存（命中即推的核心）；短 TTL + 主动失效双保险 |
| 检索编排 | **LlamaIndex**（轻量） | 缓存未命中时编排"拉 SOP 片段 + LLM 综合"的轻量管线 |
| LLM 抽象 | `langchain-core` 的 `BaseChatModel` | 模型可插拔，配置切换 Claude/通义千问/DeepSeek/本地化模型，与 A/B 一致 |
| Embedding | **bge-m3**（与 B 共享） | 拦截原因/SOP 片段语义匹配（仅未命中兜底时用） |
| 数据校验 | **Pydantic v2** | 拦截事件、卡片 DTO、缓存键的 schema 即类型 |
| HTTP 客户端 | **httpx**（异步） | 调文档型 RAG `search_docs`、追溯型 RAG 历史检索 |
| 消息 | **aiokafka** | 订阅 `mes.checkpoint.lifecycle` 拦截事件 + 版本失效事件 |
| 推送通道 | **FastAPI WebSocket** | 工位屏幕长连接，按 `(station_id, tenant_scope)` 路由卡片 |
| 元数据持久化 | **SQLAlchemy 2.0 (async) + asyncmy** | 消费位点、`event_id` 幂等表、历史拦截处置记录 |
| 可观测 | **OpenTelemetry Python** + `prometheus-client` | trace 串联、指标告警 |
| 配置 | pydantic-settings | 环境变量 / 配置文件统一管理 |
| 部署 | 独立微服务 `assist-service`（uvicorn + gunicorn worker） | 与三大服务同网格，K8s 部署 |

### 2.2 为什么是"预计算 + 缓存"而非"临时跑 LLM"

- **过点现场不能等 LLM**：工位场景要的是秒级响应，临时跑 LLM（拉 SOP + 综合）动辄 3-10 秒，操作工等不起。而**拦截原因是有限集**（`blocking_reason` 枚举有限：质量类/设备类/物料类/工艺类/首件类），同一原因在不同工位/产品的处置高度重复--天然适合预计算。
- **预计算命中即推**：预先把"拦截原因 + SOP 片段 + 历史处置"打包成卡片入 Redis，过点拦截事件到达时按缓存键查命中，命中即 WebSocket 推送（毫秒级）。
- **未命中兜底走轻量 LLM**：缓存未命中（新拦截原因/新工艺版本首次出现）才走 LLM 生成，生成后**回填缓存**供下次命中。同一拦截原因首次慢（秒级），后续都秒推。这把 LLM 的延迟摊销到首次，把稳态命中做到毫秒级。
- **与过点 SLA 解耦**：即使 LLM 慢/挂，命中的卡片照推；缓存全空时推"转人工"兜底卡片，绝不阻塞过点（过点早已在主事务内完成拦截，D 只是补充处置）。

### 2.3 为什么事件驱动而非轮询

- 过点拦截是**事件**（`CheckpointBlocked` 发布到 Kafka），D 订阅即触发，无需轮询工位状态。
- 事件驱动天然解耦：D 服务故障不影响过点引擎发布事件；D 恢复后从 Kafka 位点续跑，期间错过的拦截事件可补推（幂等）或丢弃（卡片是辅助，丢一次不致命）。
- 版本失效也是事件：`ProcessRouteActivated`/`QualityGateRuleActivated` 驱动缓存片段失效重算，与文档型/追溯型 RAG 共享同一套版本契约（§5.3）。

### 2.4 部署形态（车间网隔离）

- 车间网络常与办公网隔离（[RAG服务引入路线.md](../RAG服务引入路线.md) §4 硬约束）。**建议**：Redis、Embedding（bge-m3）本地化部署，LLM 视车间安全策略二选一（云端 API 或本地化模型）。`BaseChatModel` 抽象保证两者切换零代码改动。
- **工位 WebSocket 长连接**需与车间终端网络可达：D 服务部署在与工位终端同网段，或经车间网关代理。工位终端断线重连后补推期间未读卡片（按 `station_id` 暂存）。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────────────────────────┐
│ assist-service（独立微服务，Python + FastAPI + Redis + WebSocket） │
│                                                                    │
│  ┌─────────────────────┐   ┌──────────────────────────────────┐  │
│  │ WebSocket 推送层     │◀──│ AssistCardDispatcher              │  │
│  │ /ws/station/{id}     │   │  拦截事件 -> 缓存查 -> 推工位      │  │
│  └─────────────────────┘   └────────────┬─────────────────────┘  │
│                                         │                          │
│              ┌──────────────────────────┼───────────────────┐     │
│              ▼                          ▼                   ▼     │
│  ┌───────────────────┐  ┌─────────────────────┐  ┌──────────┐ │
│  │ CardCache (Redis) │  │ CardGenerator       │  │ 版本失效  │ │
│  │ 命中即推(毫秒级)   │  │ 未命中: SOP+LLM兜底  │  │ Handler  │ │
│  └────────┬──────────┘  └──────────┬──────────┘  └────┬─────┘ │
│           │                        │                  │       │
│  ┌────────▼────────┐       ┌───────▼────────┐         │       │
│  │ 预计算预热 Job   │       │ ACL 只读调用    │         │       │
│  │ 离线打包高频卡片  │       │ B: search_docs │         │       │
│  └────────┬────────┘       │ A: 历史处置检索  │         │       │
│           │                └────────────────┘         │       │
│  ┌────────▼────────┐  ┌─────────────────────┐         │       │
│  │ Idempotency     │  │ consumer offset     │         │       │
│  │ Table (MySQL)   │  │ (MySQL)             │         │       │
│  └─────────────────┘  └─────────────────────┘         │       │
└───────────────────────────────────┼───────────────────┼──────────┘
                                    │ 订阅拦截事件(只读)  │ 订阅版本失效事件
                          ┌─────────▼──────────┐  ┌──────▼──────────┐
                          │ aiokafka Consumer   │  │ process.*       │
                          │ mes.checkpoint.     │  │ quality.*       │
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

- **缓存即核心，LLM 是兜底**：稳态下 99% 的拦截走缓存命中即推，LLM 只在首次未命中时生成。这与文档型/追溯型"每次检索都跑 LLM"形成鲜明对比--D 把 LLM 用在预计算阶段，把运行时压到毫秒级。
- **事件驱动 + 位点管理**：消费者维护 `consumer offset` 落 MySQL，重启从断点续跑；`event_id` 幂等表保证重复投递不重复推卡片（§5.4）。
- **推送与生成分离**：`AssistCardDispatcher`（查缓存推工位）与 `CardGenerator`（未命中生成）解耦，生成慢不阻塞推送--生成完成后回填缓存并补推。
- **ACL 防腐层**：调文档型/追溯型 RAG 时经 ACL 适配，外部 DTO -> 内部视图（`SopFragment`/`HistoricalDisposition`），外部 schema 变化不污染卡片核心。符合 CLAUDE.md 的低耦合/ACL 约束。

---

## 4. 拦截场景建模：对齐 blocking_reason 有限集

D 服务的"领域模型"不是聚合根，而是**拦截场景分类 + 处置卡片模型**。拦截原因严格对齐过点执行上下文 `CheckpointBlocked.blocking_reason` 的有限枚举（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) §2.1/§2.4/§2.6/§3 + 点检保养上下文 `pm.*.overdue`）。

### 4.1 拦截原因分类（blocking_reason 有限集）

每个拦截原因对应一组确定的处置模式，不需要 LLM 猜处置方向：

| 类别 | `blocking_reason` | 来源上下文 | 典型处置模式 | 是否自动分流返修 |
|------|-------------------|-----------|-------------|----------------|
| **质量类** | `QualityGateFail` / `QualityHold` | 质量（同步门禁 `QualityGateEvaluated` BLOCK/HOLD） | 缺陷码 + SOP 处置 + 返修分流 | 质量类自动分流（`UnitRoutedToRework`） |
| **质量类** | `TestResultTimeout` | 过点（TestResult 等待窗口超时） | 等待数据/手工填报/工程师确认 | 否 |
| **设备类** | `EquipmentUnavailable` | 过点（`EquipmentAvailabilityCache` available=false） | 联系设备工程师/查看锁定原因（点检超期/维修中/故障） | 否 |
| **设备类** | `EquipmentDataOutOfRange` / `EquipmentDataTimeout` | 过点（设备实时数据校验） | 关键工序参数异常/超时保守拦截，可申请授权放行 | 否 |
| **物料类** | `KitNotReady` | 工单管理（`KitStatusCache` kit_ready=false） | 齐套未齐，缺料清单 + 联系物控 | 否 |
| **物料类** | `BomMismatch` / `FixtureMismatch` | 过点（防错校验物料/工装） | 核对 BOM/工装绑定 | 否 |
| **工艺类** | `RouteCacheMiss` | 过点（工艺缓存未命中降级查询失败） | 工艺版本异常，联系工艺工程师 | 否 |
| **工艺类** | `SkipViolation`（跳站/漏站） | 过点（站序校验） | 退回正确工位/申请授权跳站 | 否 |
| **首件类** | `FirstArticleBlocked` | 首件处理（`fai.article.blocked`） | 首件未放行，联系质量工程师完成首件检验 | 否 |

> **点检超期锁定的链路**：点检保养上下文发布 `pm.inspection.overdue`（`InspectionOverdue`）-> 台账上下文 `MarkAssetUnavailable` -> 过点执行上下文 `EquipmentAvailabilityCache` available=false -> 过点 `CheckpointBlocked(EquipmentUnavailable)`。D 服务订阅的是过点的 `CheckpointBlocked`，但卡片需**回溯**点检超期原因（调台账/点检只读 REST 查 `asset_id` 的锁定原因），给出"联系设备工程师完成点检"的处置。这条跨上下文回溯是 D 的一个设计要点（§6.2）。

### 4.2 处置卡片模型（AssistCard）

卡片是 D 的核心值对象--工位要的是**一句话原因 + 一个动作**，不是长文：

```python
class CardAction(BaseModel):
    """一个可执行动作（需人在正式界面操作，RAG 不代劳）。"""
    action_type: ActionType           # REWORK_SPLIT / CONTACT_ENGINEER / WAIT_DATA / AUTH_OVERRIDE / MANUAL_FILL
    description: str                  # "按 SOP-WELD-014 目检后返修分流"
    target_system: str | None = None  # "返修工位" / "设备工程师张三"
    sop_ref: str | None = None        # SOP 文档引用（带版本）

class HistoricalDisposition(BaseModel):
    """历史同类拦截的处置统计（来自追溯型 RAG / 历史记录）。"""
    similar_count_7d: int             # 近 7 天同类拦截次数
    confirmed_root_cause: str | None  # 已确认根因（如"锡膏批次 B-77 异常"）
    typical_action: str | None        # 典型处置

class AssistCard(BaseModel):
    """推给工位屏幕的辅助卡片。"""
    card_id: str                      # 卡片唯一 ID（幂等用）
    station_id: str                   # 目标工位（推送路由）
    work_order_id: str
    sn: str | None = None
    blocking_reason: str              # 权威拦截原因（来自 CheckpointBlocked）
    reason_summary: str               # "AOI 判 NG（桥接缺陷）"--一句话原因
    actions: list[CardAction]         # 处置动作列表
    history: HistoricalDisposition | None = None
    confidence: float                 # 0.0~1.0
    source_refs: list[str]            # ["SOP:WELD-014@v3", "intercept:INT-2026-0710-001"]
    route_version: str                # 卡片所引 SOP 的工艺版本（版本一致性）
    generated_by: str                 # "cache_hit" / "llm_fallback" / "manual"
    disclaimer: str = "辅助信息，不改变拦截决策，处置需在正式界面执行"
    needs_human_review: bool = False
    created_at: datetime
```

- **`reason_summary` 一句话**：操作工扫一眼就懂。LLM 生成时被约束为单句（≤30 字）。
- **`actions` 限 1-3 个**：工位不是阅读器，动作要精炼。`action_type` 固化为 Enum，避免 LLM 编造动作类型。
- **`history` 可选**：有历史同类处置时附上，给操作工"这种拦截以前怎么处理的"参考。
- **`source_refs` 强制引用**：SOP 文档（带版本 `@v3`）+ 历史拦截 ID，工程师可点开回溯。
- **`disclaimer` 不可省**：每张卡片都标注"不改变拦截决策"，防止操作工误以为 RAG 能放行。

### 4.3 缓存键设计

卡片缓存键 = `(blocking_reason, station_type, product_scope, route_version)`：

```python
class CardCacheKey(BaseModel):
    blocking_reason: str      # "QualityGateFail"
    station_type: str         # "AOI" / "REFLOW" / "FCT"
    product_scope: str        # 产品段 PCBA / 整机 或产品编码
    route_version: str        # 工艺版本（SOP 片段版本绑定）
```

- **为什么是四元组**：同一拦截原因在不同工位类型（AOI vs 回流焊）、不同产品段（PCBA vs 整机）、不同工艺版本下，SOP 片段与处置不同。四元组保证命中的卡片版本正确。
- **`route_version` 进键**：工艺升版后旧版本卡片不命中（键不同），触发新版本首次生成 -> 回填新键。旧版本卡片 TTL 过期自动清理，或被版本失效 Handler 主动删（§5.3）。
- **粒度权衡**：🔴 **缓存键粒度是否进 `product_scope`** 待与工艺/质量团队确认。若按产品编码粒度，缓存命中率低（每种产品一份）；若按产品段（PCBA/整机）粒度，命中率高但可能不够精准。MVP 建议先按产品段，按命中率数据调整。

---

## 5. 预计算与缓存治理

缓存是 D 的命脉。这一节定义"卡片如何预打包、如何命中、如何随版本失效刷新"。

### 5.1 预计算预热（离线打包高频卡片）

`CardWarmer` 是预计算 Job，离线把高频拦截原因的卡片打包入缓存：

```text
预计算 Job（定时/事件触发）
   │
   ├─ 扫描拦截原因有限集（§4.1 表）
   ├─ 对每个 (blocking_reason, station_type, product_scope, route_version) 组合：
   │    ├─ 调文档型 RAG search_docs(拦截原因+工位, route_version) 拉 SOP 片段
   │    ├─ 调追溯型 RAG / 历史记录 查近 7 天同类拦截的处置统计
   │    ├─ LLM 综合生成 AssistCard（一句话原因 + 动作 + 历史）
   │    └─ 写入 Redis（键 = 四元组，TTL = 工艺版本有效期）
   │
   └─ 仅对"当前 ACTIVATED 的 route_version"预热（不预热历史版本）
```

- **触发时机**：① 服务启动时全量预热当前生效版本；② `ProcessRouteActivated`/`QualityGateRuleActivated` 事件触发对应组合的重算（§5.3）；③ 定时（如每班次开始）刷新历史处置统计。
- **预热是尽力而为**：预热失败（B/A 服务不可用）不阻塞服务启动，运行时未命中走 LLM 兜底生成。预热覆盖率作为可观测指标（§10.1）。
- **预热与运行时生成共用 `CardGenerator`**：区别只是预热是离线批量、运行时是单条实时。生成逻辑单一（SRP）。

### 5.2 命中即推（运行时主路径）

`AssistCardDispatcher` 消费 `CheckpointBlocked` 事件后的主路径：

```text
CheckpointBlocked 事件到达
   │
   ├─ 1. 解析 blocking_reason + station_type + product_scope + route_version
   ├─ 2. 组缓存键，查 Redis
   │     ├─ 命中 -> 取 AssistCard，补 sn/work_order_id 等本次实例字段 -> WebSocket 推工位（毫秒级）
   │     └─ 未命中 -> 转 CardGenerator 异步生成（§6）
   │                  └─ 生成期间先推"处置生成中，请联系工程师"占位卡片（不阻塞操作工）
   │                  └─ 生成完成回填缓存 + 补推正式卡片
   │
   └─ 3. event_id 幂等记录 + 位点推进（同 MySQL 事务）
```

- **命中路径零 LLM**：稳态下 99% 走这里，毫秒级推送。
- **未命中不阻塞**：先推占位卡片（"处置生成中"），操作工不至于干等；正式卡片异步补推。
- **实例字段补全**：缓存里的卡片是"模板"（不带 sn/work_order_id），推送时补本次拦截实例字段。模板与实例分离，提高缓存复用率。

### 5.3 缓存失效与版本一致性

工艺/规则/缺陷字典变更时，对应缓存片段必须失效重算。D 服务订阅版本失效事件，与文档型/追溯型 RAG 共享同一套版本契约：

| 失效事件 | 来源 | 缓存失效动作 |
|---------|------|-------------|
| `ProcessRouteActivated` | 工艺管理 `process.route.lifecycle` | 删除旧 `route_version` 对应卡片（TTL 兜底）+ 预热新 `route_version` 高频组合 |
| `ProcessRouteDeprecated` | 工艺管理 `process.route.lifecycle` | 删除该 `route_version` 所有卡片（历史版本不再推送） |
| `QualityGateRuleActivated` | 质量 `quality.gate.lifecycle` | 失效质量类拦截原因（`QualityGateFail`/`QualityHold`）对应卡片 + 重算 |
| `QualityGateDeprecated` | 质量 `quality.gate.lifecycle`（`superseded_by=null`） | 失效引用该规则的卡片 |
| `DefectCatalogDefined`/`Updated` | 质量 `quality.defect.catalog` | 失效质量类卡片（缺陷码映射可能变）+ 重算 |
| `InspectionStandardUpdated` | 质量 `quality.standard.lifecycle` | 失效相关质量类卡片 |

> **版本一致性三道闸（与 A/B 同构）**：① **预热闸**--只对 `ACTIVATED` 的 `route_version` 预热；② **失效闸**--版本变更事件主动删旧缓存片段；③ **检索闸**--运行时命中时校验卡片 `route_version` 与当前过点 `route_version` 一致，不一致视为未命中重新生成。版本一致性不是 D 自己保证的，是从领域模型兜上来的（过点记录绑 `routeVersion`、工艺/规则有生命周期、变更事件驱动失效）。

### 5.4 幂等与去重

事件经各上下文 Transactional Outbox **至少一次**投递（[消息处理实现说明.md](../../实现说明/业务事件/消息处理实现说明.md) §1），D 侧必须幂等消费，否则重复投递会重复推卡片。

```sql
-- 幂等表：event_id + consumer_group 唯一键
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
```

- **推送幂等**：`event_id` 幂等表挡重复事件；卡片 `card_id` 含 `event_id`，WebSocket 客户端按 `card_id` 去重（断线重连补推时不重复展示）。
- **位点管理**：手动 ack，推送事务（推 WebSocket + 幂等记录 + 位点更新）成功后才 ack offset。崩溃窗口可回退到上次成功位点重放（幂等兜住重复推送）。

---

## 6. 辅助卡片生成（未命中兜底）

缓存未命中时走 `CardGenerator` 轻量生成。这是 D 唯一调用 LLM 的路径，必须快、准、可兜底。

### 6.1 生成管线

```text
CardGenerator.generate(key, intercept_event)
   │
   ├─ 1. 拉处置知识（ACL 只读）
   │     ├─ 调文档型 RAG search_docs(blocking_reason + station_type, route_version) -> SopFragment[]
   │     └─ 调追溯型 RAG / 历史记录 查同类拦截处置 -> HistoricalDisposition
   │
   ├─ 2. LLM 综合（结构化输出 AssistCard）
   │     ├─ 系统提示词约束：一句话原因(≤30字) + 1-3 个动作(枚举) + 引用 SOP + 不编造
   │     └─ with_structured_output(AssistCard) 强制 schema
   │
   ├─ 3. 校验与兜底
   │     ├─ Pydantic 校验失败 -> 重试 1 次
   │     ├─ SOP 片段为空 / confidence < 0.6 -> needs_human_review=True，推"转人工"兜底卡片
   │     └─ LLM 超时/失败 -> 推"处置生成失败，请联系工程师"兜底卡片（不硬答）
   │
   └─ 4. 回填缓存 + 返回
        └─ 写 Redis（键 = 四元组，TTL）供下次命中
```

- **LLM 只做综合，不做检索**：SOP 片段与历史处置由 ACL 拉（B/A 服务），LLM 只把它们压缩成卡片。这降低 LLM 幻觉风险--LLM 编不出不存在的 SOP，因为输入就是真实片段。
- **结构化输出强约束**：`with_structured_output(AssistCard)` 让模型只能返回符合 schema 的卡片，`action_type` 固化为 Enum，`reason_summary` 限单句。
- **生成与推送解耦**：`CardGenerator` 只生成卡片，推送由 `AssistCardDispatcher` 完成（生成回填缓存后触发补推）。单一职责（SRP）。

### 6.2 跨上下文回溯（点检超期等链路场景）

部分 `blocking_reason` 在过点事件里只有粗原因（如 `EquipmentUnavailable`），D 需回溯精因（点检超期/维修中/故障）才能给准处置。`ReasonEnricher` 经 ACL 只读查询补齐：

| 粗原因 | 回溯查询 | 精因 | 处置 |
|--------|---------|------|------|
| `EquipmentUnavailable` | 台账/点检只读 REST 查 `asset_id` 锁定原因 | 点检超期 / 维修中 / 故障停用 | 联系设备工程师完成点检 / 等维修完成 / 报修 |
| `QualityGateFail` | 质量只读 REST 查 `defect_code` + 规则 | 具体缺陷码 + 严重度 | 按 SOP 处置 + 返修分流 |
| `KitNotReady` | 工单管理只读 REST 查 `missing_items` | 缺料清单 | 联系物控补料 |

- **回溯是只读降级查询**，不进过点主事务（过点早已完成），超时降级为推粗因卡片 + "请联系工程师查详细原因"。
- **回溯结果可入缓存**：精因进缓存键或卡片字段，下次同精因直接命中。

---

## 7. 工位推送通道

### 7.1 WebSocket 会话管理

工位终端连接 `ws://assist-service/ws/station/{station_id}`，D 服务维护 `(station_id, tenant_scope) -> WebSocket` 会话表：

```python
class StationSessionRegistry:
    """工位 WebSocket 会话注册表，按 station_id 路由卡片。"""
    async def register(self, station_id: str, ws: WebSocket, tenant: TenantContext) -> None: ...
    async def unregister(self, station_id: str) -> None: ...
    async def push_card(self, station_id: str, card: AssistCard) -> bool:
        """推卡片到工位会话；会话不在线则暂存待补推。"""
```

- **会话不在线暂存**：工位终端断线时，卡片按 `station_id` 暂存 Redis（短 TTL，如 5 分钟），终端重连后补推。补推按 `card_id` 去重。
- **权限校验**：WebSocket 握手时校验 token 的 `tenant_scope` 与 `station_id` 归属，防止跨车间订阅。
- **多终端**：同一工位可能有多个屏幕（主屏+副屏），会话表支持一对多，卡片广播到该工位所有会话。

### 7.2 推送协议

卡片以 JSON 推送，终端渲染短卡片 UI：

```json
{
  "type": "assist_card",
  "card_id": "CARD-2026-0710-001",
  "reason_summary": "AOI 判 NG（桥接缺陷）",
  "actions": [
    {"action_type": "REWORK_SPLIT", "description": "按 SOP-WELD-014 目检后返修分流", "sop_ref": "SOP:WELD-014@v3"}
  ],
  "history": {"similar_count_7d": 12, "confirmed_root_cause": "锡膏批次 B-77 异常"},
  "confidence": 0.82,
  "disclaimer": "辅助信息，不改变拦截决策，处置需在正式界面执行"
}
```

- **卡片 UI 由应用层渲染**：D 服务只产 JSON 卡片，终端/前端负责渲染成短卡片样式。这与"过点终端 UI 交互归应用层"的边界一致（[过点执行上下文.md](../../领域模型/生产执行服务/事件风暴/过点执行上下文.md) 暂缓模块说明）。

---

## 8. 实现方案

### 8.1 卡片分发服务（AssistCardDispatcher）

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
        metrics: MetricsCollector,
    ) -> None: ...

    async def consume(self, msg: ConsumerRecord, group: str) -> None:
        event = DomainEvent.model_validate_json(msg.value)
        if event.event_type != "CheckpointBlocked":
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 1. 幂等检查
        if await self._idem.exists(event.event_id, group):
            await self._offset.advance(group, msg.topic, msg.partition, msg.offset)
            return
        # 2. 解析缓存键（回溯精因）
        key = await self._enricher.enrich_key(event)
        # 3. 查缓存
        card = await self._cache.get(key)
        if card:
            card = self._fill_instance(card, event)  # 补 sn/wo_id 等实例字段
            card.generated_by = "cache_hit"
            await self._registry.push_card(key.station_id, card)
            self._metrics.cache_hit.inc(key.blocking_reason)
        else:
            # 4. 未命中：先推占位卡片，异步生成
            await self._registry.push_card(key.station_id, self._placeholder(event))
            self._metrics.cache_miss.inc(key.blocking_reason)
            asyncio.create_task(self._generate_and_push(key, event))
        # 5. 幂等记录 + 位点推进
        await self._idem.record(event.event_id, group, msg.topic)
        await self._offset.advance(group, msg.topic, msg.partition, msg.offset)

    async def _generate_and_push(self, key: CardCacheKey, event: DomainEvent) -> None:
        card = await self._generator.generate(key, event)
        if card:
            await self._cache.set(key, card)          # 回填缓存
            card = self._fill_instance(card, event)
            await self._registry.push_card(key.station_id, card)  # 补推正式卡片
        else:
            await self._registry.push_card(key.station_id, self._fallback(event))  # 转人工兜底
```

- 分发与生成分离：`AssistCardDispatcher` 只管查缓存推工位，`CardGenerator` 只管生成（SRP）。
- 未命中不阻塞：先推占位卡片，异步生成后补推。

### 8.2 卡片生成器（CardGenerator）

```python
class CardGenerator:
    """未命中时拉 SOP + 历史处置，LLM 综合成卡片，回填缓存。"""

    def __init__(
        self,
        doc_rag: DocRagAclClient,        # 文档型 RAG ACL
        trace_rag: TraceRagAclClient,    # 追溯型 RAG ACL（历史处置）
        llm: BaseChatModel,
        cache: CardCache,
    ) -> None: ...

    async def generate(self, key: CardCacheKey, event: DomainEvent) -> AssistCard | None:
        # 1. 拉处置知识
        sops = await self._doc_rag.search_docs(
            query=f"{key.blocking_reason} {key.station_type} 处置",
            route_version=key.route_version,
            tenant=self._tenant(event),
        )
        history = await self._trace_rag.query_historical_disposition(
            blocking_reason=key.blocking_reason, tenant=self._tenant(event)
        )
        if not sops:
            return None  # 无 SOP 片段 -> 转人工兜底
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

- LLM 只做综合，输入是 ACL 拉的真实片段，降低幻觉。
- 无 SOP / LLM 失败 / 低置信度都走兜底，不硬答。

### 8.3 ACL 防腐层

```python
class DocRagAclClient:
    """文档型 RAG 只读 ACL，强制 route_version 过滤。"""
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
    async def search_docs(
        self, query: str, route_version: str, tenant: TenantContext
    ) -> list[SopFragment]:
        if not route_version:
            raise ValueError("route_version 必填，禁止查无版本 SOP（版本一致性）")
        resp = await self._http.get(
            "/rag/docs/search", params={"q": query, "route_version": route_version},
            headers=tenant.headers(), timeout=2.0,
        )
        resp.raise_for_status()
        return [SopFragmentMapper.to_view(d) for d in resp.json()]
```

- 外部 DTO 不进卡片核心，只暴露 `SopFragment`--防腐层核心职责（CLAUDE.md ACL 约束）。
- 强制 `route_version` 入参，与文档型 RAG 的版本过滤对齐。

### 8.4 版本一致性保证

三道闸，不靠口头约束：

1. **预热闸**：`CardWarmer` 只对 `ACTIVATED` 的 `route_version` 预热。
2. **失效闸**：`VersionInvalidationHandler` 订阅 `ProcessRouteActivated`/`QualityGateRuleActivated` 等，删旧缓存片段 + 预热新版本。
3. **检索闸**：`AssistCardDispatcher` 命中时校验卡片 `route_version` == 过点 `route_version`，不一致视为未命中重新生成。

---

## 9. 推荐包结构（Python src layout）

```text
assist_service/
  app/
    api/
      ws_router.py             # WebSocket /ws/station/{id}
      admin_router.py          # /admin/card/refresh（手动刷新缓存）
      schemas.py
    application/
      card_dispatcher.py       # AssistCardDispatcher
      card_generator.py        # CardGenerator
      card_warmer.py           # 预计算预热 Job
      reason_enricher.py       # 跨上下文回溯精因
    domain/
      card.py                  # AssistCard / CardAction / HistoricalDisposition
      cache_key.py             # CardCacheKey
      intercept_reason.py      # blocking_reason 枚举 / 拦截原因分类
      tenant.py                # TenantContext
      projection.py            # VersionInvalidationHandler 协议
    infrastructure/
      redis_/
        card_cache.py          # CardCache（命中即推主存）
        session_registry.py    # StationSessionRegistry（WebSocket 会话）
      ai/
        llm_factory.py
      embedding/               # bge-m3（仅未命中兜底用）
        bge_client.py
      acl/                     # 只读调用 A/B RAG + 各上下文 REST
        doc_rag.py             # 文档型 RAG search_docs
        trace_rag.py           # 追溯型 RAG 历史处置
        asset_acl.py           # 台账/点检回溯精因
        quality_acl.py         # 缺陷码/规则回溯
      kafka/
        consumer_group.py
        listeners.py
      persistence/
        models.py              # intercept_idempotency / intercept_offset / card_audit
        idempotency_repo.py
        offset_repo.py
      obs/
        tracing.py
        metrics.py
    config.py
    main.py                    # FastAPI app + lifespan 启动断言
  tests/
  pyproject.toml
```

- `domain/intercept_reason.InterceptReason` 是有限集枚举（ISP），每个原因可挂自己的回溯策略。
- `infrastructure/acl/` 是防腐层，调 A/B RAG 与各上下文只读 REST，外部 DTO 经 Mapper 转内部视图。
- `application/card_warmer` 与 `card_generator` 共用生成逻辑，区别是离线批量 vs 运行时单条。

---

## 10. 可观测性与兜底

### 10.1 指标（prometheus-client）

| 指标 | 含义 |
|------|------|
| `assist_intercept_total` | 拦截事件数（按 blocking_reason label） |
| `assist_cache_hit_total` | 缓存命中数（命中即推主路径） |
| `assist_cache_miss_total` | 缓存未命中数（走 LLM 兜底） |
| `assist_cache_hit_ratio` | 缓存命中率（健康度核心指标，目标 >95%） |
| `assist_card_push_latency_seconds` | 卡片推送延迟（事件到达到推送，Histogram） |
| `assist_llm_generate_latency_seconds` | LLM 生成延迟（仅未命中路径） |
| `assist_low_confidence_total` | 低置信度转人工次数 |
| `assist_fallback_total` | 兜底卡片次数（无 SOP/LLM 失败） |
| `assist_version_invalidation_total` | 版本失效刷新次数（按事件类型） |
| `assist_ws_session_active` | 在线工位会话数 |
| `assist_warmer_coverage_ratio` | 预热覆盖率（已预热组合/应有组合） |

### 10.2 trace 串联

- 每次拦截处理一个 `trace_id`，从 Kafka 事件头透传，OpenTelemetry 在 `AssistCardDispatcher`、ACL 客户端、LLM 调用、WebSocket 推送都注入 span。
- 卡片 `source_refs` + `card_id` 让工程师从工位卡片回溯到拦截事件、SOP 文档、历史拦截记录--证据链可点开回溯。

### 10.3 兜底

- **缓存未命中兜底**：先推占位卡片，异步生成后补推；生成失败推"转人工"兜底卡片。
- **LLM 失败兜底**：超时/异常 -> 推"处置生成失败，请联系工程师"卡片，不硬答。
- **低置信度兜底**：`confidence < 0.6` -> `needs_human_review=True`，卡片标注"建议人工确认"。
- **A/B RAG 不可用兜底**：ACL 超时 -> 无 SOP 片段 -> 推"转人工"兜底卡片；D 服务自身故障不影响过点（过点早已拦截）。
- **WebSocket 断线兜底**：会话不在线暂存 Redis，重连补推。

---

## 11. 实现步骤

### 阶段一：骨架与拦截事件订阅（1 周）

1. 搭 `assist_service` 骨架（FastAPI + uvicorn），对齐 §9 包结构。
2. 接 aiokafka 订阅 `mes.checkpoint.lifecycle`，过滤 `CheckpointBlocked`，幂等表 + 位点表跑通（§5.4）。
3. 实现 `StationSessionRegistry` + WebSocket 端点 `/ws/station/{id}`（§7.1）。
4. 实现 `AssistCardDispatcher` 主路径（查 Redis 命中即推，未命中推占位）（§8.1）。
5. 实现 `ReadOnlyIngestionGate` 启动断言（禁止任何写 MES 调用）。

### 阶段二：预计算与缓存治理（1-2 周）

6. 实现 `CardWarmer` 预热 Job（离线打包高频拦截原因卡片）（§5.1）。
7. 实现 `CardCacheKey` 四元组 + TTL（§4.3）。
8. 实现 `VersionInvalidationHandler` 订阅 `process.*`/`quality.*` 失效刷新（§5.3）。
9. 验证版本一致性：工艺升版后旧卡片失效、新版本首次生成回填。

### 阶段三：卡片生成与 ACL（1-2 周）

10. 实现 `CardGenerator`（拉 SOP + 历史 + LLM 综合 + 结构化输出）（§8.2）。
11. 实现 `DocRagAclClient`/`TraceRagAclClient` ACL（调文档型/追溯型 RAG）（§8.3）。
12. 实现 `ReasonEnricher` 跨上下文回溯精因（点检超期等链路）（§6.2）。
13. 接 OpenTelemetry + prometheus 指标（§10.1），缓存命中率告警。

### 阶段四：加固、评测与试点（1 周）

14. 沉淀评测集（典型拦截场景 + 预期卡片），回归模型/提示词变更。
15. 兜底链路全测（无 SOP/LLM 失败/低置信度/WebSocket 断线）。
16. 灰度一个高频拦截场景（如 AOI 质量门禁拦截）试点，收集操作工反馈。
17. 按命中率数据调整缓存键粒度（🔴 §4.3）。

---

## 12. 约束落地检查清单

- [ ] D 服务不进过点主事务（§1.2），过点 P99 ≤200ms 不受影响；卡片推送允许秒级延迟。
- [ ] 主判定走规则引擎，RAG 只给处置；每张卡片带 `disclaimer`"不改变拦截决策"。
- [ ] 预计算 + 缓存命中即推（§5.2），稳态命中率目标 >95%；未命中走 LLM 兜底。
- [ ] `event_id + consumer_group` 幂等表，重复投递不重复推卡片；卡片 `card_id` 客户端去重。
- [ ] 消费者位点落 MySQL，重启从断点续跑，推送事务成功后才 ack offset。
- [ ] 缓存键含 `route_version`；版本失效事件（`ProcessRouteActivated`/`QualityGateRuleActivated`/`DefectCatalogDefined`）触发缓存片段失效重算（§5.3）。
- [ ] 命中时校验卡片 `route_version` == 过点 `route_version`，不一致重新生成（检索闸）。
- [ ] D 服务无任何写 MES 接口；`ReadOnlyIngestionGate` 启动断言禁止写调用。
- [ ] WebSocket 会话绑定 `(station_id, tenant_scope)`，握手校验权限；历史查询前置 `tenant_scope` 过滤。
- [ ] LLM 输出经 Pydantic `AssistCard` 校验，`action_type` 固化为 Enum，`reason_summary` 限单句。
- [ ] 无 SOP 片段 / LLM 失败 / `confidence < 0.6` -> 推"转人工"兜底卡片，不硬答。
- [ ] 卡片带 `source_refs`（SOP 文档带版本 + 历史拦截 ID），证据可回溯。
- [ ] D 服务故障不影响过点（过点早已在主事务内拦截）；WebSocket 断线暂存补推。

---

## 13. 面试防守 Q&A

**Q：防错即时辅助 RAG 和普通问答 RAG 有什么本质区别？**
A：普通问答 RAG 是"用户主动问，RAG 答"；防错即时辅助是"**过点拦截事件触发，RAG 主动推**"。它不是等操作工问，而是过点引擎判拦截（`CheckpointBlocked`）时，自动把"为什么拦 + 怎么办"推给工位屏幕。更关键的是它**不进过点主事务**--过点 P99 ≤200ms 是硬约束，拦截决策由规则引擎在主事务内完成，RAG 只在拦截**之后**异步推处置。主判定走规则引擎，RAG 只给处置建议，每张卡片标注"不改变拦截决策"。这是把 RAG 从旁边问答变成能进生产主线的防错副驾（[RAG服务引入路线.md](../RAG服务引入路线.md) §2.4）。

**Q：过点现场等不起 LLM，你怎么解决延迟？**
A：靠**预计算 + 缓存命中即推**。拦截原因是有限集（`blocking_reason` 枚举有限：质量类/设备类/物料类/工艺类/首件类），同一原因在不同工位/产品的处置高度重复。预先把"原因 + SOP 片段 + 历史处置"打包成卡片入 Redis，过点拦截事件到达时按四元组键 `(blocking_reason, station_type, product_scope, route_version)` 查命中，命中即 WebSocket 推送（毫秒级）。只有缓存未命中（新原因/新工艺版本首次）才走 LLM 生成，生成后回填缓存供下次命中。稳态下 99% 走缓存，LLM 的延迟摊销到首次，把稳态命中做到毫秒级。LLM 慢/挂也不影响--命中的卡片照推，全空时推"转人工"兜底卡片。

**Q：会不会拖慢过点？**
A：不会进过点主事务。过点 P99 ≤200ms（[领域总览.md](../../领域模型/领域总览.md) §4.1）是硬约束，过点主事务零分布式事务（§5.3），RAG 不得进写路径。D 服务订阅 `CheckpointBlocked` 是拦后异步消费，与过点判定完全解耦。操作工先看到拦截红（规则引擎同步给出），几秒后看到处置卡片（RAG 异步推送）。D 服务挂了，过点照常拦截，只是工位少了处置建议--绝不影响生产主线。

**Q：拦截原因那么多，怎么保证推的处置是对的版本？**
A：版本一致性靠三道闸，和追溯型/文档型 RAG 共享同一套契约。① 预热闸--只对 `ACTIVATED` 的 `route_version` 预热卡片；② 失效闸--订阅 `ProcessRouteActivated`/`QualityGateRuleActivated`/`DefectCatalogDefined` 事件，版本变更时主动删旧缓存片段 + 预热新版本；③ 检索闸--命中时校验卡片 `route_version` == 过点 `route_version`，不一致重新生成。版本一致性不是 D 自己保证的，是从领域模型兜上来的--过点记录绑 `routeVersion`、工艺/规则有生命周期、变更事件驱动失效。错给一条已失效 SOP 处置会直接导致批量不良，所以这条必须兜死。

**Q：RAG 推的处置错了怎么办？操作工照着做出事谁负责？**
A：三重兜底。一是卡片明确标注 `disclaimer`"辅助信息，不改变拦截决策，处置需在正式界面执行"--RAG 不代劳任何写操作，处置动作（返修分流/授权放行）必须操作工/工程师在正式界面做。二是 LLM 只做综合不做检索，输入是 ACL 拉的真实 SOP 片段，降低幻觉--模型编不出不存在的 SOP。三是低置信度（`<0.6`）或无 SOP 片段时推"转人工"兜底卡片，不硬答。与 MES 防错理念一致：宁可拦下让人判，不可错放。错答案零容忍是 MES 领域的底线。

**Q：点检超期锁定这种跨上下文的拦截，你怎么给处置？**
A：过点事件里只有粗原因 `EquipmentUnavailable`，D 需回溯精因。`ReasonEnricher` 经 ACL 只读查台账/点检上下文：这台设备为什么不可用？是点检超期（`pm.inspection.overdue` -> 台账 `MarkAssetUnavailable`）、维修中、还是故障停用？查到精因后给准处置（联系设备工程师完成点检 / 等维修完成 / 报修）。回溯是只读降级查询，不进过点主事务，超时降级为推粗因卡片 + "请联系工程师查详细原因"。精因还可入缓存，下次同精因直接命中。

**Q：和文档型 RAG、追溯型 RAG 是什么关系？重复吗？**
A：不重复，是协同。文档型 RAG（B）给 D 提供 SOP 片段（D 卡片生成时调 B 的 `search_docs`，带 `route_version` 过滤）；追溯型 RAG（A）给 D 提供历史同类处置（按 `blocking_reason` 查历史拦截记录与处置结果）。D 是"事件驱动 + 预计算缓存"的推送形态，A/B 是"检索 + 综合"的问答形态。D 把 A/B 的能力压缩成工位短卡片主动推，A/B 留给工程师主动查。先 B 后 D--B 没建起来 D 的 SOP 片段只能离线导入，无法随工艺版本自动刷新（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。

**Q：和 L1 诊断型 Agent 什么关系？**
A：分层，不重复。D 是**操作工侧实时短卡片**（一句话原因 + 一个动作，秒级推工位）；L1 是**工程师侧深度诊断**（多步 ReAct 推理，数十秒）。操作工看不懂 LLM 长答案，工位要的是短卡片（[RAG服务引入路线.md](../RAG服务引入路线.md) §5 Q&A）。D 推卡片时可附"如需深入诊断，工程师可用 L1"的入口。简单拦截用 D 的卡片够了，复杂根因递进用 L1。

**Q：不同车间能看的数据不一样怎么管？**
A：WebSocket 会话绑定 `(station_id, tenant_scope)`，握手时校验 token 的 `tenant_scope` 与工位归属，防止跨车间订阅。卡片只推给拦截发生工位的会话。历史同类处置查询前置 `tenant_scope` 过滤，不是答完再裁剪。本 MES 的 14 个限界上下文边界本身就是天然的权限切分面（§1.2）。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是三条架构判断：① RAG 不进过点主事务，拦后异步推、主判定走规则引擎；② 拦截原因是有限集，靠预计算 + 缓存命中即推把稳态做到毫秒级，LLM 只兜底首次未命中；③ 版本一致性从领域模型兜上来，订阅 `ProcessRouteActivated` 等事件刷新缓存片段。落地需要先做文档型 RAG（路线 B）验证 SOP 检索可用性，D 作为高价值试点挑一个高频拦截场景（如 AOI 质量门禁拦截）切入（[RAG服务引入路线.md](../RAG服务引入路线.md) §3）。诚实 + 体现架构判断力，比硬吹"已上线防错 AI"得分高。

---

## 14. 一句话定位

"防错即时辅助 RAG 把 RAG 从旁边问答变成现场防错副驾--过点引擎判拦截（`CheckpointBlocked`）时，D 服务拦后异步把'一句话原因 + 一个动作'的短卡片推给工位屏幕。它不进过点主事务（过点 P99 ≤200ms 是硬约束），主判定走规则引擎、RAG 只给处置；靠拦截原因是有限集做预计算 + 缓存命中即推（稳态毫秒级），LLM 只兜底首次未命中；卡片引用的 SOP 片段带 `route_version`，订阅 `ProcessRouteActivated`/`QualityGateRuleActivated` 刷新缓存片段；低置信度或无 SOP 转人工兜底，绝不硬答--这是让 RAG 能进生产主线而不拖慢过点的关键边界。"
