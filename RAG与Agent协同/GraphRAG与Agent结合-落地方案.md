# GraphRAG 与 Agent 结合落地方案（图 + L1 诊断 + L2 草稿）

> 本文把 [RAG 路线 A 追溯型 RAG（GraphRAG）](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md) 与 [AGENT 路线 L1 诊断型 Agent](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)、[L2 草稿型 Agent](../AGENT服务/AGENT服务引入路线.md) 结合成一条 **"查图 -> 多步诊断 -> 草拟处置"** 的落地链路。
>
> **与现有文档的关系**：
> - [追溯型 RAG-实现方案.md](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md) / [详细设计.md](../RAG服务/追溯型%20RAG/追溯型%20RAG-详细设计.md) = **图怎么建、怎么检索**（基座，已设计）；
> - [L1诊断型Agent-实现方案.md](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) = **Agent 怎么多步推理**（上层，已设计）；
> - **本文 = 两者怎么协作**：分层路由规则、业务场景分流、模块协作契约、L2 草稿闭环。**不重复**建模与编排细节，只补"结合层"。
>
> **口径纪律**：本方案是**设计阶段的引入规划**，不是线上已落地能力。讲法遵循 [项目亮点与指标卡片.md](../面试指南/项目亮点与指标卡片.md) §0——说"规划方向 / 设计取舍"，不说"我们已上线 GraphRAG+Agent"。MES 领域对错误答案零容忍（错给一条已失效工艺 = 批量不良），对误写零容忍（错发一张返工单 = 批量报废），所以本文主轴是 **图作快路径把版本/权限结构性兜住 + L2 写动作过 confirmation gate**，而非堆模型。

---

## 1. 设计目标与边界

### 1.1 目标

把"单件焊接不良根因"这类问题，从"工程师跨 5 个界面手动串"升级成一条自动链：

```text
图一次性给齐 5M1E 全貌（秒级）
        │
        ▼
L1 Agent 对全貌做根因假设排序，必要时递进深挖某一维（数十秒）
        │
        ▼
L2 基于诊断结果 + 图证据，草拟返工单 / 8D（人确认后落库）
```

三层各司其职，**不是互相替代，是接力**：

| 层 | 职责 | 数据来源 | 时延 | 写权限 |
|----|------|---------|------|--------|
| **GraphRAG**（基座） | 结构化事实链 + 版本/权限**结构性**兜底 | 领域事件流预投影的属性图 | 秒级 | 无（只读投影） |
| **L1 诊断 Agent**（上层） | 多步推理、递进深挖、跨上下文跳转 | 图（快路径）+ 上下文只读 REST（降级/补齐） | 数十秒（≤60s） | 无（只读工具） |
| **L2 草稿 Agent**（写意图） | 草拟返工单 / 8D / SOP | L1 诊断结果 + 图证据（subgraph_ref）+ 文档型 RAG | 秒级 | 草稿不落库；落库走人确认 + 正常应用服务 |

### 1.2 硬边界（一开口就要讲）

继承自 RAG 路线 §4 与 AGENT 路线 §4 的全部约束，结合方案特有的三条加粗：

| 边界 | 说明 | 落地 |
|------|------|------|
| **只读投影** | 图是领域事件的只读投影，事实源是各上下文聚合根 | RAG 服务只订阅 Kafka 只读事件 + REST 只读降级；`ReadOnlyProjectionGate` 启动断言禁止 `DELETE`/`REMOVE`（[图方案 §9.7](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)） |
| **不进过点主事务** | 图索引、Agent 推理、L2 草拟均异步，与过点判定完全解耦 | 过点 P99 ≤200ms 不受影响；图允许秒级最终一致；Agent ≤60s（[领域总览.md](../领域模型/领域总览.md) §4.1） |
| **版本快照不可变** | 历史过点记录锁定的 `routeVersion` 不随工艺变更改变 | `CheckpointRecord` 带 `route_version` + `[:SNAPSHOT_OF_ROUTE]` 边指向当时版本；工艺变更只新增版本节点、旧版本 `DEPRECATED` 不删（INV-09） |
| **权限隔离** | 检索/工具调用前按车间/产线/角色过滤，不是答完再裁剪 | 图节点带 `tenant_scope`，Cypher `WHERE` 前置；L1 工具带 `required_tenant_scopes`，`ToolNode` 调用前拦截 |
| **可观测兜底** | 每个答案带证据链 + 置信度，低置信度转人工 | `TraceAnswer` / `DiagnosisReport` 带置信度阈值；与 MES 防错理念一致：宁可拦下让人判 |
| **写动作 confirmation gate** | L2 可生成写意图，但落库必须人确认；绝不旁路应用服务写 | L2 输出 `intent + draft`，`requires_confirmation=True`；落库走返工/工单上下文正常应用服务，过聚合根不变式 + 事务发件箱（[AGENT 路线 §4](../AGENT服务/AGENT服务引入路线.md)） |
| **工具注册对齐限界上下文** | Agent 能调的工具 = 14 个上下文暴露的 toolset，边界即工具边界 | 每个上下文一个只读 toolset；`query_traceability_graph` 是跨上下文的图检索工具，优先级最高 |
| **L2 不直查图** | L2 只消费 L1 已验证的诊断结果 + 图证据引用，不独立承担版本兜底责任 | L2 入参是 `DiagnosisReport + subgraph_ref`，不直接调 `query_traceability_graph` |

### 1.3 与现有文档的关系

- **与图方案**：图方案的 §6（5M1E 检索）、§9.8（`/rag/trace/query` 端点）是本文 L1 调图的底层。本文不重新定义 Cypher，只定义"Agent 何时调它、调完怎么用"。
- **与 L1 方案**：L1 方案的 §4（工具注册）、§5（ReAct 循环）是本文 Agent 层的基础。本文新增的是"图工具优先"的路由策略 + L1->L2 的衔接契约。
- **与 L2**：L2 尚无独立实现方案文件（仅在 [AGENT 路线 §2.3](../AGENT服务/AGENT服务引入路线.md) 有方向描述）。本文给出 L2 的结合层契约与代码骨架，**L2 完整实现方案待补**（§8 🔴）。
- **与文档型 RAG（路线 B）**：L2 的 8D / SOP 草拟需要路线 B 提供历史同类文档检索。本文假设路线 B 已提供 `search_docs(query, route_version_filter)`（[L1 方案 §5.5](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）。

---

## 2. 结合架构：分层路由（图作快路径）

### 2.1 总体架构

```text
┌──────────────────────────────────────────────────────────────────────┐
│  前端（工程师工作台 / 工位屏幕）                                        │
│    按场景路由：标准化追溯 -> /rag/trace；开放诊断 -> /agent/diagnose；     │
│                要处置 -> /agent/draft                                    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
  ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐
  │ rag-service    │   │ agent-service    │   │ agent-service    │
  │ /rag/trace/*   │   │ /agent/diagnose  │   │ /agent/draft     │
  │                │   │  (L1 诊断)        │   │  (L2 草稿)        │
  │ GraphRetriever │   │  LangGraph ReAct │   │  DraftService    │
  │  Cypher 5M1E   │   │                  │   │                  │
  │  LLM 综合      │   │  ToolRegistry:   │   │  消费 L1 report  │
  │                │   │  ①query_trace_   │   │  + subgraph_ref  │
  │                │◀──│    graph(优先)   │   │  -> intent+draft  │
  │                │   │  ②上下文只读REST  │   │                  │
  │                │   │   (降级/深挖)     │   │  confirmation    │
  └───────┬────────┘   └──────┬───────────┘   └────────┬─────────┘
          │                   │                        │
          │                   │ 降级/深挖调 REST         │ 草稿落库
          │                   ▼                        ▼
          │          ┌──────────────────┐    ┌────────────────────┐
          │          │ ACL -> 各上下文    │    │ 返工/工单上下文      │
          │          │ 只读 REST         │    │ 正常应用服务         │
          │          │ (过点/工艺/物料/   │    │ (聚合根不变式 +      │
          │          │  质量/设备...)     │    │  事务发件箱)         │
          │          └──────────────────┘    └────────────────────┘
          │
          ▼
  ┌──────────────────────────────────────────┐
  │ Neo4j（追溯图投影）                        │
  │  领域事件流 -> GraphProjector -> 节点/边     │
  │  版本/权限结构性兜底（SNAPSHOT_OF_ROUTE、  │
  │  tenant_scope WHERE 前置）                 │
  └──────────────────────────────────────────┘
```

### 2.2 分层路由规则

前端按问题形态路由，Agent 内部再按"图优先"二次路由：

| 规则 | 触发条件 | 走法 | 主力模块 |
|------|---------|------|---------|
| **R1：直查图** | 用户提供 SN/批次/工单 + 要 5M1E 全貌（标准化追溯） | 直接 `POST /rag/trace/query`，不绕 Agent | GraphRAG |
| **R2：Agent 诊断（图作快路径）** | 开放诊断（根因要递进、跨上下文跳转） | `POST /agent/diagnose`；Agent **第一步先调 `query_traceability_graph`** 取全貌，图覆盖不足或需深挖某一维再调上下文 REST | L1（调图 + 降级 REST） |
| **R3：草拟处置** | 诊断完成、工程师要处置（返工/8D/SOP） | `POST /agent/draft`；L2 消费 L1 的 `DiagnosisReport + subgraph_ref` 草拟，**不重查图** | L2（+ 图证据引用 + 文档型 RAG） |
| **R4：图降级** | 图投影滞后 / 节点缺失（如 `CONSUMED_BATCH` 边 gap） | L1 在诊断过程中经 ACL 降级查询上下文只读 REST 补齐；L2 不直查，只消费 L1 已验证证据 | L1 ACL 降级 |

> **为什么图作快路径**：MES 零容忍场景下，版本一致性是命门。图的 `SNAPSHOT_OF_ROUTE` 快照边把"查对版本"变成**图的结构属性**（物理指向当时版本节点），而纯 Agent+REST 把它变成 **LLM 每步自觉带 `route_version`** 的流程性约束——前者从结构上杜绝失效工艺，后者依赖 LLM 不漏带（[图方案 §15 Q&A](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)）。所以 Agent 诊断时**优先调图**，把版本/权限兜底交给图结构，REST 只在图覆盖不到时做降级补齐。

### 2.3 关键设计决策

- **图作快路径，不等于图能回答一切**：图覆盖 5M1E 的"结构化事实"（哪批料、哪台设备、哪版工艺），但"同批次不良率统计""设备参数基线对比"这类**聚合计算**图不擅长——这些由 L1 调上下文 REST 现场算。图给"是谁"，REST 补"多少"。
- **L1->L2 靠 `subgraph_ref` 传证据，不重查**：L1 诊断产出的 `DiagnosisReport` 带 `subgraph_ref`（指向落库的 `TraceSubgraph`），L2 草拟返工单时按 `subgraph_ref` 回查图节点填充 `affected_sn_list`、`source_work_order_id`——避免 L2 阶段重复检索 + 重复承担版本兜底。
- **L2 不直查图、不直写 MES**：L2 只读 L1 产物 + 文档型 RAG，写意图落库走人确认 + 正常应用服务。这条把 L2 的风险面降到最小——最坏情况是"草稿没用上"，不会产生写副作用。
- **三个服务独立部署、HTTP 解耦**：`rag-service` 与 `agent-service` 是独立微服务，跨服务调图/调诊断走 httpx，互不侵入。这与 [L1 方案 §1.3](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) 跨语言解耦一脉相承。

---

## 3. 业务场景与各模块使用

> 按业务场景组织：每个场景说明**触发条件 -> 走哪个模块 -> 数据流 -> 输出 -> 兜底**。场景 3.1–3.3 偏检索/诊断（图 + L1），3.4–3.6 偏处置草拟（L2）。

### 3.1 单件根因诊断（SN-001 焊接不良）—— 图为主

| 项 | 内容 |
|----|------|
| **触发** | 工艺/质量工程师输入"SN-001 焊接不良根因" |
| **路由** | R1 直查图（问题含明确 SN + 要全貌，标准化追溯） |
| **数据流** | `SeedResolver` 正则命中 SN-001 -> `GraphRetriever.expand_5m1e` Cypher 扩展 -> LLM 综合 -> `TraceAnswer` |
| **主力模块** | **GraphRAG**（全程）；L1 不介入 |
| **输出** | 5M1E 假设排序 + 证据链（`node_id` 引用）+ 置信度 |
| **兜底** | `confidence < 0.6` -> `needs_human_review` 转人工；`projection_lag_ms` 超阈值降权 + ACL 降级补齐 |

> 这是图**独立能闭环**的场景——5M1E 全貌一次取齐，不需要多步推理。用 Agent 反而是浪费（数十秒 vs 秒级）。

### 3.2 批次不良突增（B-77 锡膏进了哪些单件、不良率）—— 图反向 + L1 聚合

| 项 | 内容 |
|----|------|
| **触发** | 质量工程师"B-77 这批锡膏进了哪些单件，不良率多少" |
| **路由** | R2 Agent 诊断（需"找单件 + 算不良率"，图给清单、REST 算比率） |
| **数据流** | L1 第一步调 `query_traceability_graph(seed=InventoryBatch{B-77})` -> 图反向扩展 `InventoryBatch ← CONSUMED_BATCH ← WipUnit` 给 `sn_list` -> L1 再调 `query_defect_rate(sn_list)` 聚合不良率 -> 对比基线 -> 假设排序 |
| **主力模块** | **GraphRAG**（反向扩展给 sn_list）+ **L1**（调质量 REST 聚合不良率） |
| **输出** | 受影响单件清单 + 不良率 + 根因假设 |
| **兜底** | `CONSUMED_BATCH` 边若未投影（🔴 [图方案 §5.1](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)），L1 降级调 `GET /api/material/consumption?sn=&work_order_id=` 补齐 |

> 图给"是谁"（哪些单件用了 B-77），REST 补"多少"（不良率统计）——图不擅长聚合计算，这类由 L1 调 REST 现场算。

### 3.3 递进深挖（供应商问题还是回流曲线偏移？）—— L1 多步为主

| 项 | 内容 |
|----|------|
| **触发** | 3.1 诊断指向 Material 维度，工程师要深挖"是 B-77 供应商问题，还是焊接站回流曲线偏移" |
| **路由** | R2 Agent 诊断（开放、跨上下文递进） |
| **数据流** | L1 多步 ReAct：①调图取 B-77 全部单件 + 焊接站设备绑定（`USED_EQUIPMENT`）-> ②调 `query_device_params(asset_id, time_range)` 取回流曲线 -> ③调 `query_supplier_history(supplier_id)` -> ④对比基线 -> 假设排序 |
| **主力模块** | **L1**（多步调图 + REST），GraphRAG（每步可调取结构化关系） |
| **输出** | 递进假设 + 每步工具调用证据（`tool_call_trace`） |
| **兜底** | `recursion_limit` ≤20（≤10 次工具调用）；工具连续失败 3 次转人工 |

> 这是图**单独搞不定**的场景——要跨物料/设备/供应商多个上下文递进追问、对比基线，正是 L1 多步推理的价值。但每一步的"结构化关系查找"仍优先调图（快路径），深挖的"参数对比"才调 REST。

### 3.4 诊断 -> 草拟返工单（L1->L2 闭环）—— L2 草稿 + 图供证据

| 项 | 内容 |
|----|------|
| **触发** | 3.1/3.3 诊断确认某批次要返工，工程师点"草拟返工单" |
| **路由** | R3 草拟处置 |
| **数据流** | L2 消费 L1 的 `DiagnosisReport + subgraph_ref` -> 按 `subgraph_ref` 回查图节点填充 `source_work_order_id`、`affected_sn_list`、`reentry_point` -> 草拟返工单（含返工工艺路线引用 + `route_version`）-> 工程师在**返工上下文正式界面**审核确认 -> 走返工上下文应用服务落库 |
| **主力模块** | **L2**（草拟）+ GraphRAG（供 `affected_sn_list`/`source_work_order_id` 证据）+ 返工上下文（落库） |
| **输出** | 返工单草稿（`intent + draft`，`requires_confirmation=True`） |
| **兜底** | 草稿**不落库**；落库过返工聚合根不变式 + 事务发件箱；Agent 绝不旁路写 |

> L2 的关键字段（`source_work_order_id`、`affected_sn_list`、`reentry_point`）都来自图的追溯子图——图是 L2 草稿的"证据基础"。L2 不自己调图，靠 L1 传来的 `subgraph_ref` 回查，保证证据已被 L1 验证过版本/权限。

### 3.5 8D 报告草拟 —— L2 + 图证据 + 文档型 RAG

| 项 | 内容 |
|----|------|
| **触发** | 质量工程师要出 8D 报告 |
| **路由** | R3 草拟处置 |
| **数据流** | L2 按 `subgraph_ref` 拉图的 5M1E 证据链（人/料/法/测各维节点）+ 调 `search_docs`（路线 B）检索历史同类 8D -> 草拟 8D（问题描述/根因/ containment/纠正措施）-> 质量工程师改完发布 |
| **主力模块** | **L2** + GraphRAG（5M1E 证据）+ 文档型 RAG（历史 8D） |
| **输出** | 8D 草稿 |
| **兜底** | 草稿不落库；8D 模板待质量上下文定义（🔴 §8） |

> 图给"事实链"（5M1E 证据），文档型 RAG 给"方法论"（历史 8D 怎么写的）——两者协同，L2 综合成稿。

### 3.6 工艺变更后 SOP 草拟 —— L2 + 文档型 RAG（图作版本锚点）

| 项 | 内容 |
|----|------|
| **触发** | 订阅 `ProcessRouteActivated` 事件（工艺升版 v3->v4） |
| **路由** | 事件驱动 L2（非问答，主动触发） |
| **数据流** | L2 基于新 `route_version=4` + 调 `search_docs` 检索现有 SOP -> 对比新旧工艺步骤差异 -> 草拟新 SOP -> 工艺工程师审核发布 |
| **主力模块** | **L2** + 文档型 RAG（现有 SOP）；GraphRAG 提供 `RouteVersion{v4}` 节点作版本锚点（非主力） |
| **输出** | SOP 草稿 |
| **兜底** | 草稿不落库；版本过滤对齐 `ProcessRouteActivated`（[图方案 §5.4](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)） |

> 这个场景图不是主力（SOP 内容在文档里），但图的新 `RouteVersion` 节点提供**版本锚点**——L2 草拟时锁定 v4，保证 SOP 与生产执行侧工艺缓存版本一致。

### 3.7 场景与模块速查表

| 场景 | GraphRAG | L1 诊断 | L2 草稿 | 文档型 RAG | 典型用户 |
|------|:--------:|:-------:|:-------:|:----------:|---------|
| 3.1 单件根因 | ★主力 | — | — | — | 工艺/质量工程师 |
| 3.2 批次突增 | 提供 sn_list | ★聚合 | — | — | 质量工程师 |
| 3.3 递进深挖 | 快路径 | ★主力 | — | — | 工艺/质量工程师 |
| 3.4 草拟返工单 | 供证据 | 已诊断 | ★主力 | — | 工艺/质量工程师 |
| 3.5 8D 草拟 | 5M1E 证据 | 已诊断 | ★主力 | ★历史 8D | 质量工程师 |
| 3.6 SOP 草拟 | 版本锚点 | — | ★主力 | ★现有 SOP | 工艺工程师 |

---

## 4. 模块协作契约

### 4.1 GraphRAG -> L1：`query_traceability_graph` 工具契约

L1 把图的 `/rag/trace/query` 封装为工具，注册时**排首位**，system prompt 引导"先调图"：

```python
class QueryTraceabilityGraphArgs(BaseModel):
    seed_kind: Literal["WIP_UNIT", "INVENTORY_BATCH", "WORK_ORDER"]
    seed_value: str               # SN / batch_no / work_order_id
    as_of: datetime | None = None # 时间窗，None=最新
    # tenant_context 由 ToolNode 注入，不暴露给模型

class QueryTraceabilityGraphResult(BaseModel):
    subgraph_ref: str             # 指向落库的 TraceSubgraph（L2 回查用）
    summary: str                  # 5M1E 全貌摘要
    confidence: float
    needs_human_review: bool
```

- **版本/权限由图兜底**：工具入参**不含** `route_version`/`tenant_scope`——版本由图的 `SNAPSHOT_OF_ROUTE` 边按 SN 锁定，权限由图 `tenant_scope WHERE` 前置过滤。L1 拿到的结果已经是版本/权限合规的，**不需要 LLM 自觉带版本**。
- **图覆盖度信号**：`summary` 里标注哪些 5M1E 维度为空（如 "Material: CONSUMED_BATCH 未投影"），L1 据此决定是否降级调 REST 补齐（🔴 阈值待定，§8）。

### 4.2 L1 -> L2：`DiagnosisReport + subgraph_ref` 传递

L1 诊断产出 `DiagnosisReport`（[L1 方案 §5.3](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)），L2 消费它 + `subgraph_ref`：

```python
class DiagnosisReport(BaseModel):
    summary: str
    confidence: float
    hypotheses: list[Hypothesis]       # 5M1E 假设排序，evidence 引用 node_id
    subgraph_ref: str                  # 指向 TraceSubgraph（L2 回查图证据用）
    needs_human_review: bool

class DraftRequest(BaseModel):
    diagnosis_report: DiagnosisReport  # L1 产物
    draft_kind: Literal["REWORK_ORDER", "EIGHT_D", "SOP"]
    tenant: TenantContext
```

- **L2 不重查图**：L2 需要的 `affected_sn_list`/`source_work_order_id`/`reentry_point` 按 `subgraph_ref` 回查图节点（只读），不重新调 `query_traceability_graph`——保证证据已被 L1 验证，且避免重复检索。
- **版本透传**：`DiagnosisReport.hypotheses[].evidence` 已含 `route_version`，L2 草拟返工工艺路线时引用同一 `route_version`（🔴 返工用原版本还是新版本待定，§8）。

### 4.3 L2 -> MES 写路径：confirmation gate

L2 输出 `intent + draft`，**绝不直接落库**：

```python
class Draft(BaseModel):
    draft_kind: Literal["REWORK_ORDER", "EIGHT_D", "SOP"]
    intent: str                        # "对 WO-2026-0707-001 的 12 件 SN 执行焊接返工"
    payload: dict                      # 返工单/8D/SOP 的结构化草稿
    evidence_refs: list[str]           # ["subgraph_ref=...", "node_id=..."]
    requires_confirmation: bool = True # L2 恒为 True
    route_version: str                 # 草稿锁定的工艺版本
```

- **落库走正常应用服务**：工程师在返工上下文 / 工单管理上下文的**正式界面**审核草稿 -> 调该上下文的正常应用服务下达 -> 过聚合根不变式校验 + 事务发件箱（[AGENT 路线 §4](../AGENT服务/AGENT服务引入路线.md)）。
- **Agent 不旁路写**：L2 不持有任何写工具，`ToolRegistry` 不注册 `create_*` 类工具（`ReadOnlyToolGate` 启动断言，[L1 方案 §7.1](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）。写动作的闸门 100% 在人手里。

### 4.4 版本一致性三层传递

```text
图（SNAPSHOT_OF_ROUTE{route_version} 边）
   │  检索结果带 route_version
   ▼
L1（DiagnosisReport.hypotheses[].evidence 含 route_version）
   │  诊断证据透传版本
   ▼
L2（Draft.route_version 锁定；返工工艺路线引用同版本）
   │  草稿带版本，落库时 MES 侧再校验
   ▼
MES（返工/工单上下文应用服务校验版本状态 ACTIVE）
```

- 版本一致性**不是哪一层自己保证的，是从图结构兜上来的**——图用快照边物理锁定版本，L1/L2 只透传不另搞版本管理，MES 侧应用服务做最后一道校验（[图方案 §5.4](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)）。

---

## 5. 关键代码骨架

### 5.1 分层路由入口（FastAPI 三端点）

```python
# agent_service/app/api/router.py
router = APIRouter()

@router.post("/agent/diagnose", response_model=DiagnosisReport)
async def diagnose(req: DiagnosisRequest, tenant: TenantContext = Depends(...),
                   svc: DiagnosisService = Depends(...)) -> DiagnosisReport:
    """L1 多步诊断。Agent 内部先调图（快路径），不够再调 REST。"""
    return await svc.diagnose(req, tenant)

@router.post("/agent/draft", response_model=Draft)
async def draft(req: DraftRequest, tenant: TenantContext = Depends(...),
                svc: DraftService = Depends(...)) -> Draft:
    """L2 草拟处置。消费 L1 诊断 + subgraph_ref，不重查图，不落库。"""
    return await svc.draft(req, tenant)
```

> 图的 `/rag/trace/query` 端点已在 [图方案 §9.8](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md) 定义，前端按 R1 规则直调；`/agent/diagnose` 在 [L1 方案 §7.5](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md) 定义。本文新增 `/agent/draft`。

### 5.2 L1 工具注册：图优先

```python
# agent_service/app/application/tool_registry_setup.py
def build_tool_registry(deps: Deps) -> ToolRegistry:
    registry = ToolRegistry()
    # ① 图检索工具——注册顺序首位，system prompt 引导"先调图取全貌"
    registry.register(ToolDescriptor(
        name="query_traceability_graph",
        description="第一步必调。按 SN/批次/工单取 5M1E 全貌，版本/权限已兜底。"
                    "返回 subgraph_ref 供后续草稿引用。",
        bounded_context="RAG服务",
        read_only=True,
        args_schema=QueryTraceabilityGraphArgs,
        required_tenant_scopes=["WORKSHOP", "LINE"],
        handler=deps.rag_client.query_traceability_graph,
    ))
    # ② 各上下文只读 REST——图覆盖不到时降级/深挖
    for tool in deps.context_tools:        # query_pass_records / query_device_params / ...
        registry.register(tool)
    registry.validate_on_startup()         # ReadOnlyToolGate 启动断言
    return registry
```

- system prompt 明确约束："**第一步先调 `query_traceability_graph`** 取 5M1E 全貌；仅在图返回某维度为空、或需聚合计算（不良率/参数基线）时才调上下文 REST。查工艺不得自行指定版本——版本由图锁定。"（🔴 "图覆盖度判断"阈值待定，§8）

### 5.3 L1 诊断编排（调图 + 降级）

```python
# agent_service/app/application/diagnosis_service.py（结合层增强）
class DiagnosisService:
    def __init__(self, graph_builder: GraphBuilder, rag_client: RagClient,
                 session_manager: SessionManager, report_repo: ReportRepo):
        self._graph_builder = graph_builder
        self._rag_client = rag_client
        ...

    async def diagnose(self, request: DiagnosisRequest, tenant: TenantContext) -> DiagnosisReport:
        session = await self._session_manager.create(request, tenant)
        # 图作快路径：Agent 第一步必然调 query_traceability_graph，
        # LangGraph 的 model_node 在 system prompt 引导下产出该 tool_call
        graph = self._graph_builder.build_for(tenant)
        final_state = await asyncio.wait_for(
            graph.ainvoke(
                {"messages": [self._build_system_prompt(request)],
                 "tenant": tenant, "session_id": session.id},
                config={"recursion_limit": 20, "configurable": {"thread_id": session.id}},
            ),
            timeout=60.0,
        )
        report = ReportParser.parse(final_state["messages"][-1], session)
        report.subgraph_ref = self._extract_subgraph_ref(final_state)  # 透传给 L2
        await self._report_repo.save(report)
        return report
```

### 5.4 L2 草稿生成（消费 L1 + subgraph_ref）

```python
# agent_service/app/application/draft_service.py
class DraftService:
    def __init__(self, rag_client: RagClient, doc_rag_client: DocRagClient,
                 llm: BaseChatModel):
        self._rag_client = rag_client      # 按 subgraph_ref 回查图节点（只读）
        self._doc_rag = doc_rag_client     # 文档型 RAG（历史 8D / 现有 SOP）
        self._llm = llm

    async def draft(self, req: DraftRequest) -> Draft:
        report = req.diagnosis_report
        # 1. 按 subgraph_ref 回查图节点，提取草稿所需证据字段（不重查图）
        evidence_nodes = await self._rag_client.fetch_subgraph_nodes(
            report.subgraph_ref, tenant=req.tenant
        )
        # 2. 按草稿类型补文档知识（8D 历史 / 现有 SOP）
        if req.draft_kind == "EIGHT_D":
            history = await self._doc_rag.search_docs(
                query=report.summary, route_version_filter=self._extract_rv(evidence_nodes)
            )
        # 3. LLM 综合成草稿（intent + payload + evidence_refs）
        draft = await self._llm.with_structured_output(Draft).ainvoke(
            self._build_prompt(report, evidence_nodes, history)
        )
        draft.requires_confirmation = True   # L2 恒为 True
        return draft
```

### 5.5 confirmation gate（草稿落库前的人确认）

```text
L2 产出 Draft（requires_confirmation=True）
        │
        ▼
工程师 UI 展示草稿 + 证据链（subgraph_ref 可点开回溯）
        │
        ├─ 工程师驳回 -> 草稿归档，不落库
        │
        └─ 工程师确认 -> 调返工/工单上下文的【正常应用服务】
                                │
                                ▼
                        聚合根不变式校验 + 事务发件箱
                                │
                                ▼
                        返工单/工单落库，Agent 不旁路写
```

- L2 **不持有写工具**，确认动作走前端调 MES 正式 API，Agent 完全不参与写路径——这条把 L2 的写风险面降到零。

---

## 6. 实现步骤

> 前置依赖：图方案 MVP 4 上下文（过点/工艺/物料/质量）已投影跑通（[图方案 §13](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)），L1 骨架已搭（[L1 方案 §9](../AGENT服务/L1诊断型Agent/L1诊断型Agent-实现方案.md)）。

### 阶段一：L1 调图工具 + 分层路由（2 周）
1. 在 L1 `ToolRegistry` 注册 `query_traceability_graph`（封装 `/rag/trace/query`），排首位 + system prompt 引导先调图。
2. 前端按 R1/R2 规则路由：标准化追溯直调图，开放诊断走 `/agent/diagnose`。
3. 验证场景 3.1（图独立闭环）+ 3.3（L1 调图 + 降级 REST 深挖）。

### 阶段二：L2 草稿服务 + 证据传递（2 周）
4. 实现 `DraftService`：消费 `DiagnosisReport + subgraph_ref`，按 `subgraph_ref` 回查图节点提取证据字段。
5. 实现返工单草拟（场景 3.4）：`source_work_order_id`/`affected_sn_list`/`reentry_point` 从图证据填充。
6. 实现 confirmation gate 流程：草稿不落库，确认走返工上下文正常应用服务。

### 阶段三：文档型 RAG 协同 + 8D/SOP 草拟（2 周）
7. 对接路线 B `search_docs(query, route_version_filter)`。
8. 实现 8D 草拟（场景 3.5）：图 5M1E 证据 + 历史 8D。
9. 实现 SOP 草拟（场景 3.6）：订阅 `ProcessRouteActivated` + 现有 SOP。

### 阶段四：版本一致性透传 + 评测（2 周）
10. 验证版本三层传递：图 `route_version` -> L1 evidence -> L2 草稿 -> MES 校验。
11. 沉淀评测集：每个场景含种子 + 预期模块路由 + 预期输出。
12. 灰度一条产线，收集工程师反馈。

---

## 7. 约束落地检查清单

- [ ] L1 `ToolRegistry` 中 `query_traceability_graph` 注册首位，system prompt 引导"先调图"；所有工具 `read_only=True`，`ReadOnlyToolGate` 启动断言生效。
- [ ] L2 **不持有任何写工具**，`ToolRegistry` 无 `create_*` 类工具；落库走返工/工单上下文正常应用服务。
- [ ] L2 草稿 `requires_confirmation=True` 恒成立；草稿不落库，确认后走聚合根不变式 + 事务发件箱。
- [ ] L2 不直查图，只按 L1 传来的 `subgraph_ref` 回查图节点（只读）。
- [ ] 版本三层传递：图 `SNAPSHOT_OF_ROUTE` -> L1 `evidence.route_version` -> L2 `Draft.route_version` -> MES 应用服务校验 `ACTIVE`。
- [ ] 图作快路径：L1 诊断第一步必调 `query_traceability_graph`；版本/权限由图结构性兜底，L1 不自行指定版本。
- [ ] 图投影滞后 / `CONSUMED_BATCH` 边缺失时，L1 经 ACL 降级查询上下文只读 REST 补齐（[图方案 §7.3](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)）。
- [ ] 前端按 R1/R2/R3 路由：标准化追溯直调图、开放诊断走 Agent、处置草拟走 L2。
- [ ] 全程不进过点主事务：图投影异步、L1 ≤60s、L2 草拟秒级，过点 P99 ≤200ms 不受影响。
- [ ] `confidence < 0.6`（图）/ `< 0.5`（L1）-> `needs_human_review`，不展示给操作工。
- [ ] 每步工具调用落 `tool_call_trace`，`subgraph_ref` + `node_id` 让证据链可点开回溯。
- [ ] L2 主动触发（SOP 草拟）只订阅 `ProcessRouteActivated` 只读事件，不消费写命令。
- [ ] 所有输出带 disclaimer：辅助假设/草稿，最终处置需工程师确认。

---

## 8. 待判断事项（🔴 交用户 / SME）

> 以下是无法替你拍板、需要你或 SME 判断的点。影响实现细节，不影响整体架构。

1. **✅ L2 实现方案已补**：见 [L2草稿型Agent-实现方案.md](../AGENT服务/L2草稿型Agent/L2草稿型Agent-实现方案.md)。L2 内部实现（策略模式草拟、只读 ACL、`NoWriteClientGate` 启动断言、confirmation gate）已落地；下述与 L2 相关的待判断项（#3 L1->L2 触发 / #4 返工路线 / #5 8D 模板 / #6 审批矩阵 / #9 subgraph_ref 生命周期）移交 L2 文档 §11 统一跟踪。
2. **🔴 "图覆盖度判断"阈值**：L1 调图后，`summary` 标注某维度为空——什么条件下 L1 判定"图不够"、转降级 REST？是"某维度完全为空"还是"节点数 < N"？这影响 L1 的步数与延迟。
3. **🔴 L1->L2 触发方式**：诊断完成后，L2 草拟是**自动续接**（L1 诊断完直接触发 L2）还是**人工发起**（工程师点"草拟返工单"）？自动续接省一步但可能草拟出不需要的处置；人工发起稳妥但多一次交互。
4. **🔴 返工工艺路线 `ReworkRoute` 版本规则**：返工走**独立返工工艺路线**（非正常 `RouteVersion`，见 [返工上下文.md](../领域模型/生产执行服务/事件风暴/返工上下文.md)）。`ReworkRoute` 是否有版本生命周期、L2 草拟时如何选择 `rework_route_ref`，需工艺管理上下文明确。详见 [L2 §11](../AGENT服务/L2草稿型Agent/L2草稿型Agent-实现方案.md)。
5. **🔴 8D 模板归属**：8D 报告草稿的模板（问题描述/根因/containment/纠正措施字段）是否由质量上下文定义标准模板？还是 L2 自由生成？
6. **🔴 confirmation gate 审批人角色矩阵**：返工单草稿由谁确认（线长/工艺工程师/质量工程师）？不同 `draft_kind` 审批人是否不同？需与权限模型对齐。
7. **🔴 `CONSUMED_BATCH` 边 gap**：批次反向扩展（场景 3.2）依赖 `CONSUMED_BATCH` 边，但该边的事件契约尚不明确（[图方案 §5.1 🔴](../RAG服务/追溯型%20RAG/追溯型%20RAG-实现方案.md)）。MVP 用降级 REST 兜底，待物料上下文明确消耗明细事件后改投影——这条继承自图方案，需持续跟进。
8. **🔴 分层路由的前端实现**：R1/R2/R3 路由是前端硬编码按问题形态分流，还是做一个 L0 收口型 Agent 做意图路由（[AGENT 路线 §2.1](../AGENT服务/AGENT服务引入路线.md)）？L0 更优雅但依赖 L1/L2 成型，MVP 建议前端硬编码。
9. **🔴 跨服务事务一致性**：L1 诊断产出的 `subgraph_ref` 落在 rag-service 的库，L2 在 agent-service 回查——跨服务引用。`subgraph_ref` 的生命周期（保留多久、是否随工单归档）需定义。

---

## 9. 面试防守 Q&A

**Q：为什么不直接让 Agent 调一堆 REST，要图作快路径？**
A：MES 零容忍场景下，版本一致性是命门。纯 Agent+REST 把"查对版本"变成 LLM 每步自觉带 `route_version` 的流程性约束——漏带就报错重试、多轮往返，还可能基于失效工艺给根因。图用 `SNAPSHOT_OF_ROUTE` 快照边把版本变成**结构属性**（物理指向当时版本节点），Agent 调图拿到的结果已经版本/权限合规，不依赖 LLM 自觉。所以 Agent 诊断优先调图（秒级、结构性兜底），REST 只在图覆盖不到时降级补齐——这是 MES 场景下"图作基座、Agent 作上层"的根因。

**Q：L1 和 L2 怎么保证不越界？**
A：L1 全程只读——`ToolRegistry` 只注册 `query_*` 工具，`ReadOnlyToolGate` 启动断言锁死。L2 不持有任何写工具——草稿 `requires_confirmation=True` 恒成立，落库走返工/工单上下文的正常应用服务，过聚合根不变式 + 事务发件箱，Agent 绝不旁路写。写动作的闸门 100% 在人手里，这是 MES 写红线的安全落地形态（[AGENT 路线 §4](../AGENT服务/AGENT服务引入路线.md)）。

**Q：图没建起来 Agent 怎么办？**
A：图没建起来，L1 退化为纯工具循环（调上下文 REST），体验差且版本兜底弱——这就是"先有图、后有 Agent"。落地顺序是 RAG 路线 A/B 先成型，再做 L1+L2（[AGENT 路线 §5](../AGENT服务/AGENT服务引入路线.md)）。本文的结合方案假设图 MVP 4 上下文已跑通。

**Q：L2 草稿会不会直接落库？**
A：不会。L2 输出 `intent + draft`，`requires_confirmation=True`，草稿不落库。工程师在正式界面审核确认后，调返工/工单上下文的正常应用服务下达——过聚合根不变式校验 + 事务发件箱。Agent 不参与写路径，最坏情况是"草稿没用上"，不会产生写副作用。

**Q：L1 诊断和直接用图检索（3.1）有什么区别？不重复吗？**
A：不重复，是分层。3.1 单件根因是图独立闭环的场景——5M1E 全貌一次取齐，用 Agent 是浪费。L1 诊断（3.3）服务于"图搞不定"的场景——递进追问、跨上下文跳转、聚合计算（不良率/参数基线对比）。图给"是谁"，L1 补"为什么 + 多少"。前端按 R1/R2 路由分流，简单场景直查图，复杂场景才走 Agent。

**Q：版本一致性在三层怎么传递？**
A：从图结构兜上来。图用 `SNAPSHOT_OF_ROUTE{route_version}` 边物理锁定版本 -> L1 诊断证据 `evidence` 带 `route_version` -> L2 草稿 `Draft.route_version` 锁定 -> MES 应用服务最后一道校验 `ACTIVE`。L1/L2 只透传版本，不另搞版本管理——版本一致性不是哪层自己保证的，是从领域模型 + 图结构兜上来的。

**Q：上线了吗？**
A：这是设计阶段的引入规划，不是已落地。重点是三个架构判断：①图作快路径把版本/权限从 LLM 责任变成图结构属性；②L1->L2 靠 `subgraph_ref` 传证据不重查，L2 不直查图不直写 MES；③写动作过 confirmation gate 不旁路应用服务。落地需要先图 MVP 跑通，再 L1 调图工具，再 L2 草稿——按"先基座后上层"推进。诚实 + 体现架构判断力，比硬吹"已上线 AI"得分高。

---

## 10. 一句话定位

"GraphRAG 与 Agent 结合的关键，是**让图作快路径把版本/权限从 LLM 的推理责任降级为图的结构属性**——L1 诊断优先调图取 5M1E 全貌、不够再降级调 REST，L2 草稿消费 L1 诊断 + `subgraph_ref` 证据不重查图、写意图过 confirmation gate 不旁路应用服务。三层接力：图给结构化事实链（秒级）、L1 做多步递进诊断（数十秒）、L2 草拟返工单/8D/SOP（人确认落库），全程不进过点主事务、版本三层透传、写闸门在人手里——这是建立在追溯护城河上的'问得到 -> 做得完'闭环，别人没有这套领域模型抄不走。"
