# 基于 E5 + Zvec 的文档检索栈备选方案（与现行 bge-m3 + ChromaDB 对比）

> **状态：备选方案 / 设计提案，未纳入主线。** 本文只描述"如果要换，怎么换、值不值得换"，不修改任何现行口径文档，也不代表生产选型已变更。
>
> **适用范围**：仅 `rag-service` 的 **Embedding 层**与**向量存储层**。不触及 `agent-service`、制造追溯只读工具、MES 写路径、文档版本治理业务规则与对外 Tool 契约。
>
> **口径源**：[rag-service-整体设计.md](../RAG服务/rag-service-整体设计.md)、[rag-service-实现方案.md](../RAG服务/rag-service-实现方案.md)、[整体技术选型与模块划分.md](../整体技术选型与模块划分.md)、[架构决策记录 ADR](../架构设计v2/00-架构决策记录ADR.md)、[RAG与Agent评测-设计与实现方案.md](../RAG与Agent评测/RAG与Agent评测-设计与实现方案.md)、[评测指标落地实现方案与中间件选型.md](../RAG与Agent评测/评测指标落地实现方案与中间件选型.md)。
>
> **证据分级**（沿用本仓写作纪律）：【B】厂商官方文档/源码；【C】二手资料需核对；【推】推断；【设】未验证假设。凡标【设】者均须在实现期实测，不得当作既成结论。
>
> **结论先行**：**不建议整体替换现行生产栈**。本方案的价值集中在"边缘/车间网隔离单机形态"与"开发态检索"，而非在线检索质量或端到端延迟。建议走 `影子评测 -> 单场景灰度 -> ADR 裁定` 的路径，现行 `bge-m3 + ChromaDB` 保持生产主线。

---

## 0. 摘要与决策建议

| 问题 | 结论 |
|---|---|
| 是否整体替换生产栈 | **否**。现行栈已绑定 8 条不可协商契约与一整套 hard gate，替换收益不足以覆盖质量与安全回退风险（§2.2、§4.3） |
| 推荐动作 | **S0 离线影子评测 + 边缘/单机形态试点**；生产主线不动（§3.10、§5 A） |
| 备选方案的真实收益 | 进程内零部署、WAL 持久化、明确的多进程读/单进程写语义、强类型 schema 与标量倒排索引、跨平台与 CPU 端侧可行性（§4.2） |
| 最大代价 | E5 512 token 截断、中文制造术语质量不确定、过滤 DSL 的注入面与"严格前置"语义待验证、单写进程与多副本分发（§4.3） |
| `zg`（Zvec-Grep）是否进生产 | **否**。它是本地工作区检索 CLI/MCP，自带索引与模型，不满足 `context_id`/锚点/审计/评测契约，进生产会形成第二套事实源（§1.2、§3.3） |
| 触发全面替换的条件 | 仅当"边缘/隔离网单机部署成为硬需求"或"Chroma 多进程争用被压测证明不可解"，且 S0–S3 全部门禁全绿，才按 ADR 走正式裁定（§5 B） |

---

## 1. 背景、范围与术语

### 1.1 本文讨论的三个外部组件

| 组件 | 层级 | 是什么 | 在本方案中的角色 |
|---|---|---|---|
| **E5** | 编码器层 | `intfloat` 发布的文本嵌入模型家族；本方案主选 `multilingual-e5-large`（XLM-R-large，560M 参数，1024 维，512 token 上下文，约 100 种语言，MIT，检索时需 `query: ` / `passage: ` 前缀）【B/C】 | 替换 `bge-m3`，产出 chunk 与查询的稠密向量 |
| **Zvec** | 存储检索层 | 阿里开源（`alibaba/zvec`，Apache-2.0，C++）的**进程内向量数据库**，官方定位"向量库里的 SQLite"：稠密/稀疏向量、全文检索 FTS、混合检索、类 SQL 过滤、WAL、多进程读同 collection / 写单进程独占；Python 3.10–3.14；v0.8.0 起内置中文 Jieba 分词【B】 | 替换 `ChromaDB`，存 chunk 向量与元数据并做 ANN |
| **zg（Zvec-Grep）** | 开发态工具层 | `zvec-ai/zvec-grep`，本地优先 CLI + MCP，把 ripgrep 精确匹配、BM25、向量检索合成一条链路，给人或 AI Agent 在本地代码/文档里找证据；内置 11 种本地 embedding 模型，含 `local/multilingual-e5-small`【B】 | **仅开发/诊断态**使用（如对本仓库 Markdown 做语义自查），不进生产链路 |

三者在 RAG 链路里的分工：

```text
文本 / 问题 --E5 编码--> 稠密向量 --Zvec 存储 + 过滤 + ANN--> 候选 chunk
                                        │
                                        └-- bge-reranker-v2-m3 精排 --> DocEvidence --> search_docs

zg：开发者本地检索本仓库文档（旁路，不接 MySQL / 不接 context_id / 不产出生产证据）
```

### 1.2 明确不在本方案范围内

- 不改 `search_docs` / `fetch_doc_answer` 的入参出参、错误码、`DocEvidence.locator` 语义；
- 不改文档版本状态机、绑定方案 B、`PENDING_REBIND` 人工确认、Kafka 事件重索引与位点事务；
- 不启用 Zvec 的稀疏向量 / 全文检索 / 混合检索作为默认召回路径（现行口径是**单路 dense + 前置过滤 + cross-encoder 精排**，见整体设计 §6.1；改为混合召回会改变评测语义，须另立扩展项并独立评测）；
- 不启用 Zvec 内置 `Embedding` / `Reranker` / MCP Server / Skills，避免再引入第二套模型与事实源；
- 不引入图数据库、GraphRAG、新的中间件服务，不恢复 `AUTO` 多步编排，不让 rag-service 承接制造追溯或意图分诊；
- `zg` 不得作为 `search_docs` 后端、不得作为评测事实源、不得与生产索引混用。

---

## 2. 现行方案基线（替换必须原样满足）

### 2.1 现行栈一览

| 层 | 现行选型 | 出处 |
|---|---|---|
| 向量化 | `bge-m3`，**1024 维 cosine**，本地 TEI（`text-embeddings-inference:1.6`） | 实现方案 §1.1、§1.3；整体设计 §5.3 |
| 精排 | `bge-reranker-v2-m3` cross-encoder，`top_k=20 -> rerank_top_n=5` | 整体设计 §2.2、§6.1 |
| 向量库 | `ChromaDB >=0.5,<0.6` **自持 client**，嵌入式 `PersistentClient(path=/data/chroma)`，collection `document_chunks` | 实现方案 §1.2、§6.1；整体设计 §8.1 |
| 过滤 | `build_where()` 白名单构造 `where` dict，`state='PUBLISHED'` + 三段锚点 + `tenant_scope` + `doc_type`，**过滤在近邻之前** | 整体设计 §6.2；实现方案 §6.2 |
| 权威源 | MySQL（文档/版本/绑定/幂等/位点/审计），向量库是**可重建派生物** | 整体设计 §10.1–10.2 |
| 切分 | 按 `doc_type` 分策略：256–512 token、重叠 ~10%，参数表整体保留，打 `section_type` | 整体设计 §5.2；实现方案 §4.2 |
| 评测 | Recall@5 ≥ 0.80、MRR ≥ 0.65、引用可定位率 ≥ 0.95、版本过滤正确率 **1.00**（硬）、跨租户泄漏 **0.00**（硬）、检索 P95 ≤ 1.0s | 整体设计 §14；实现方案 §10.2 |
| 部署 | 独立 Python 3.12+ / FastAPI 微服务，API 与 Kafka 消费者可按进程角色拆分，不拆服务 | 整体设计 §8.3 |

### 2.2 八条不可协商约束

| # | 约束 | 依据 |
|---|---|---|
| C1 | **1024 维 / cosine / 单路 dense 近邻**；`IndexGate` 启动断言不过即失败。换维度须走变更评审 + 全量重建 + 全部基线重标定 | 实现方案 §6.1；整体设计 §5.3 |
| C2 | **前置过滤下推到向量库**，且只接受服务端白名单字段；禁止"先召回再裁剪"，禁止请求体透传 SQL / 集合名 / 顶层过滤表达式 | 整体设计 §6.2、§16.1 #7；实现方案 `extra=forbid`；ADR-09 |
| C3 | **chunk 不可变**（写入后无 UPDATE 路径，升版只追加）+ 幂等键 `{doc}:{version}:{seq}:{strategy_ver}` + `index_state=READY` 才对外可检索 + 命中后回查 MySQL 丢弃并打 `rag_doc_deprecated_leak_total`（期望 0） | 整体设计 §5.3、§10.2；实现方案 §4.3、§5.2 |
| C4 | **离线重建可原子切换**：写新 collection -> 校验 count/checksum -> 别名切换 -> 旧集合保留一个周期；重建只读 MySQL 与文件，不触发领域事件、不改 MES | 实现方案 §5.4 |
| C5 | **对外契约逐字不变**：`VersionAnchor` 三段、`context_id`（服务端签发，默认 30min）、`NO_MATCH` = 200 + `missing_evidence`、六个错误码、`locator={page,offset,heading_path}`、`quoted_text ≤1200`、`RETRIEVAL_ONLY` 默认、缺锚点拒答且代码中不存在"回退当前生效版"分支 | 整体设计 §2.2–2.3、§16.2；实现方案 §2.2 |
| C6 | **锚点单向流动**：追溯产出 -> agent 透传 -> rag 只消费；冲突返回 `VERSION_ANCHOR_MISMATCH` 由编排层停下；`evidence_set_id` 仍由 agent-service 单点签发 | 整体设计 §4.5；ADR-06、ADR-13 |
| C7 | **评测门禁原样可跑且全绿**：七项安全红线 `S` 类 `CIGate` hard gate 零容忍、质量 soft gate（基线 ×0.95）、`VersionAnchorChecker`、8 条必过用例 | 整体设计 §14；评测设计 §5.8、§6.5；实现方案 §10.2 |
| C8 | **适配层纪律**：向量库自持 client、封在 `infrastructure/vector` 之后，业务/domain 不 import 具体实现；**不得依赖 AgentScope `rag/` 模块接入**（已核实无 ChromaDB 适配器，且 2.0.0 曾被整体删除、2.0.5 才回归）；框架 pin 小版本 + CI API 漂移检查 | 整体设计 §8.2、§16.2；ADR-01；整体技术选型 §2.5 |

### 2.3 现行方案已自认的痛点（替换的真实动机来源）

1. **批量 upsert 无事务、元数据更新可能部分成功** —— 现行靠"chunk 不可变 + 版本号隔离 + MySQL 权威源 + 可重建别名切换"规避（整体设计 §16.1 #4、Q4）。
2. **嵌入式模式受单机资源限制；`where` 是字符串精确匹配，不适合模糊条件**（整体设计 Q4）。
3. **多进程争用**：评测侧已点名"ChromaDB 是嵌入式（Parquet 挂卷），生产多副本时也需评估迁移"、"评测环境用独立 Chroma 实例或禁用并行"（[评测指标落地实现方案与中间件选型.md §7 陷阱 1](../RAG与Agent评测/评测指标落地实现方案与中间件选型.md)）。
4. **框架适配器缺失**，故必须自持 client + 版本 pin + 降级预案（整体设计 §8.2）。

> 关于既有决策 "[Milvus / Qdrant 替换 Chroma：不引入](../RAG与Agent评测/评测指标落地实现方案与中间件选型.md)"（§1.3）：该结论的原文理由是"**评测只读，不替换生产向量库**"，属于评测侧边界声明，不是对生产选型的永久禁令。但它提出了一条对本方案同样有效的纪律：**影子评测不得改变既有评测基线与并行执行语义**，故 S0 阶段必须使用独立索引实例并禁用 `pytest-xdist` 并行。

---

## 3. 备选方案设计

### 3.1 目标与非目标

| 目标 | 非目标 |
|---|---|
| 在**不改任何对外契约与版本治理语义**的前提下，验证 E5 + Zvec 是否可承载 `search_docs` | 追求更高的中文检索质量（这是风险项，不是承诺） |
| 给出车间网隔离 / 单机 / CPU-only 边缘形态的可落地检索后端 | 把 rag-service 拆成多服务或引入新中间件 |
| 让替换可灰度、可观测、可秒级回退 | 借这次替换顺带启用混合检索、稀疏向量或多向量召回 |

### 3.2 架构与数据流

```text
┌─ rag-service（单一 Python 3.12+ / FastAPI 服务，进程角色可拆） ─────────────┐
│                                                                            │
│  [只读 API 角色]  POST /rag/docs/search                                     │
│     context_id -> 租户/scopes/预算/证据集                                    │
│     resolve_anchor() -> build_filter()（白名单，服务端构造）                 │
│     E5 编码 "query: " + query                                               │
│     ZvecStore.query(vector, filter, top_k=20)  ← 只读打开 collection 目录    │
│     bge-reranker-v2-m3 精排 top_n=5（保持不变）                             │
│     drop_deprecated_hits() 回查 MySQL                                        │
│     -> SearchDocsResponse（契约不变）                                        │
│                                                                            │
│  [单写角色 IndexWriter]  Kafka 只读消费 / publish / rebind                   │
│     MySQL 事务（状态 + index_idempotency + 位点）                            │
│     E5 编码 "passage: " + chunk                                             │
│     写 Zvec collection（单进程独占） -> optimize() -> count/checksum 校验     │
│     目录指针原子切换 -> 分发只读副本                                         │
└────────────────────────────────────────────────────────────────────────────┘
        ▲ 权威状态：MySQL    审计/指标：OTel + prometheus-client    缓存限流：Redis
```

关键分层不变：MySQL 仍是权威源，Zvec 仍是**可重建派生物**；E5 只负责编码；reranker 与版本治理不换，使一次变更只涉及两个变量，便于评测归因。

### 3.3 组件选型与版本口径

| 组件 | 选型 | 关键参数与纪律 | 证据 |
|---|---|---|---|
| Embedding（主选） | `intfloat/multilingual-e5-large` | **1024 维**，与现行 `bge-m3` 同维度 → C1 的 `IndexGate` 不变；512 token 上下文；约 100 语言；MIT；索引侧必须加 `passage: `、查询侧必须加 `query: ` | 【B/C】模型卡口径，实现期以实际权重与 tokenizer 复核 |
| Embedding（边缘备选） | `intfloat/multilingual-e5-small` | 384 维，CPU 可跑；**维度变更触发 C1 评审 + 全量重建 + 基线重标定**，仅作为边缘形态候选 | 【B/C】 |
| 不选 | `e5-large-v2` 等英文 E5 | MES 文档以中文 SOP/检验标准为主，英文模型不作默认 | 【推】 |
| 向量库 | `zvec`，**pin 到具体小版本**（如 `0.8.x` 的确定版） | Python 3.10–3.14 与仓库 `>=3.12` 兼容；Apache-2.0；`HNSW + COSINE`；标量字段建倒排索引；WAL；多进程读 / 单进程写；v0.8.0 起内置中文 FTS 分词 | 【B】官方 README、Quickstart、数据结构文档 |
| 精排 | `bge-reranker-v2-m3`（不动） | 保留 cross-encoder，避免同时改变召回与精排两个变量 | 现行口径 |
| 推理服务 | 本地 TEI 或 `sentence-transformers`（不动形态） | E5 前缀在 **rag-service 适配层**拼接，TEI 只做编码，避免模型语义散到多个服务 | 【推】 |
| `zg` | 仅开发机/诊断态 | `zg --index` 索引本仓库 Markdown 做语义自查；其索引、模型、缓存与生产完全隔离 | 【B】Zvec-Grep 官方定位 |

> 版本纪律沿用现行：**pin 到具体小版本，不用 `~=` 通配；CI 增 API 漂移检查**。Zvec 处于 0.x、GitHub README 显示 0.8.0 仍在扩充平台与索引能力，其 Python API 名（如 `Query` / `VectorQuery`）已有版本差异迹象，本文示例取官方 Quickstart 写法，落地时以 pin 版本的 API Reference 为准（§4.3 D7）。

### 3.4 索引设计：Chroma metadata 到 Zvec 强类型 schema 的映射

Zvec 是**强类型 schema**，标量只支持 `STRING/BOOL/INT*/FLOAT/DOUBLE` 及对应数组，**不支持嵌套字典**【B】。因此现行 `locator: dict` 必须拆字段，回包时再组装，保证 C5 对外契约不变。

| 现行 Chroma 字段 | Zvec 字段 | DataType / index_param | 用途 |
|---|---|---|---|
| `id = {doc_id}:{version_id}:{chunk_seq}` | `id` | STRING（主键，写入后不可改） | 幂等键、回溯 |
| `embedding`（1024 维 cosine） | `embedding` | `VECTOR_FP32`, dimension=1024, `HnswIndexParam(metric_type=COSINE)` | ANN |
| `text` | `text` | STRING | 回包 `quoted_text` 来源 |
| `state` | `state` | STRING + `InvertIndexParam()` | 前置过滤 `state = 'PUBLISHED'` |
| `version_kind` / `version` / `version_ref_id` | 同名 | STRING + 倒排索引 | 三段锚点前置过滤 |
| `tenant_scope` | `tenant_scope` | STRING + 倒排索引（配合 `in (...)`） | 租户隔离 hard gate |
| `doc_type` | `doc_type` | STRING + 倒排索引 | 文档类型过滤 |
| `doc_id` / `version_id` / `version_no` | 同名 | STRING + 倒排索引 | 证据组装、审计 |
| `section_type` | `section_type` | STRING + 倒排索引 | 章节过滤与展示 |
| `chunk_seq` | `chunk_seq` | INT32 | 幂等、排序 |
| `locator.page` / `locator.offset` / `locator.heading_path` | `locator_page`(INT32) / `locator_offset`(INT32) / `locator_heading_path`(STRING) | 无需索引 | 回包组装 `locator` dict |
| `file_content_hash` | `file_content_hash` | STRING | 摄入幂等 |
| `effective_from` / `effective_to` | 同名 | INT64（epoch 秒）或 STRING(ISO8601)，二选一后全仓统一 | 时间语义 |

> **口径对齐提醒**：ADR-06 要求证据带 `[valid_from, valid_to]` + supersession 链，而现行实现落的是 `effective_from/effective_to`；评测侧又用 `status=ACTIVATED`，实现侧用 `state=PUBLISHED`。换栈时须统一到一套字段名，并同步进过滤表达式与 hard gate 断言，否则会出现"门禁假通过/假失败"。本方案沿用实现侧 `state=PUBLISHED` 与 `effective_*`，字段名统一列为 S1 的交付项。

Schema 示例（字段为节选，完整清单见上表；API 名以 pin 版本为准）：

```python
schema = zvec.CollectionSchema(
    name="document_chunks",
    fields=[
        zvec.FieldSchema(name="state", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="version_kind", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="version", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="version_ref_id", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="tenant_scope", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="doc_type", data_type=zvec.DataType.STRING,
                         index_param=zvec.InvertIndexParam()),
        zvec.FieldSchema(name="locator_page", data_type=zvec.DataType.INT32),
        zvec.FieldSchema(name="locator_offset", data_type=zvec.DataType.INT32),
        zvec.FieldSchema(name="locator_heading_path", data_type=zvec.DataType.STRING),
        zvec.FieldSchema(name="text", data_type=zvec.DataType.STRING),
    ],
    vectors=[
        zvec.VectorSchema(name="embedding", data_type=zvec.DataType.VECTOR_FP32,
                          dimension=1024,
                          index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE)),
    ],
)
collection = zvec.create_and_open(path="/data/zvec/document_chunks_<ts>", schema=schema)
```

> **限制**：Zvec **不支持跨 collection 查询**（无 Join / Union / 多 collection 检索）【B】。因此影子双跑必须是适配层发起的**两条独立 query**，不能指望库内 union；现行"单 collection + 别名切换"的形态与此一致，不构成退化。

### 3.5 过滤下推：从 `where` dict 到 Zvec filter DSL

Zvec 支持 `=`、`!=`、`in (...)`、`and/or`、`like`、`contain_any` 等类 SQL 表达式，已索引标量字段可高效过滤，未索引字段可查但显著变慢【B】。官方 Quickstart 对"向量 + 过滤"的表述是 *only matching documents are considered during search*【B】；但**"绝不让不满足条件的文档进入候选/结果"这一安全性是否严格等价于 Chroma 的 where pre-filter，尚未在本仓库验证**【设】，列为 §6 的 H2 与 S1 硬门禁用例。

安全口径：`filter` 表达式**只能由服务端 `build_filter()` 用白名单字段构造**，字段名硬编码、值来自 `context_id` 解析出的租户上下文与已校验的 `VersionAnchor`；值先做**字符白名单校验，含引号或非法字符直接 fail closed 拒绝**，不依赖尚未核实的 DSL 字面量转义语法（H2）。请求体出现 `tenant_id` / `sql` / `collection` / `where_raw` / `filter` 一律 422（`extra=forbid` 现状不变）。

```python
import re

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/\-]+$")

def _q(value: str) -> str:
    if not _SAFE_VALUE.fullmatch(value):
        raise ContractError("FILTER_VALUE_REJECTED")
    return "'" + value + "'"

def build_filter(req: SearchDocsRequest, tenant: TenantContext) -> str:
    parts = ["state = 'PUBLISHED'"]
    if req.doc_category == "PROCESS_BOUND":
        anchor = req.version_anchor or anchor_from_evidence_set(req.evidence_set_id)
        parts += [f"version_kind = {_q(anchor.version_kind)}",
                  f"version = {_q(anchor.version)}",
                  f"version_ref_id = {_q(anchor.version_ref_id)}"]
    elif req.doc_category == "ASSET_BOUND":
        if not req.asset_id:
            raise ContractError("ASSET_ID_REQUIRED")
        parts += ["version_kind = 'asset'", f"version_ref_id = {_q(req.asset_id)}"]
    parts.append("tenant_scope in (" + ", ".join(_q(s) for s in tenant.scopes) + ")")
    if req.doc_types:
        parts.append("doc_type in (" + ", ".join(_q(d) for d in req.doc_types) + ")")
    return " and ".join(parts)

class ZvecStore:
    async def assert_ready(self) -> None:
        col = self._collection
        assert_dim_and_count(col, expected_dim=1024, expected_metric=COSINE)

    async def query(self, vector: list[float], filter_expr: str, top_k: int) -> list[dict]:
        rows = self._collection.query(
            zvec.Query(field_name="embedding", vector=vector),
            topk=min(top_k, self._max_top_k),
            filter=filter_expr,
        )
        return _to_rows(rows)
```

`VectorStorePort` 保持现行签名（`query(vector, where, top_k)`），`ChromaStore` / `ZvecStore` 两个实现通过配置 `RAG_VECTOR_BACKEND=chroma|zvec` 切换，`build_where()` 与 `build_filter()` 都只从同一份 `TenantContext` 与 `VersionAnchor` 取值，避免两套过滤逻辑分叉。

### 3.6 摄入、幂等、不可变与重建

- **不变**：状态机、绑定方案 B、`PENDING_REBIND` 人工确认、幂等键 `{doc}:{version}:{seq}:{strategy_ver}`、位点与业务变更同事务落 MySQL 后再 commit offset、`index_state=READY` 才对外可检索、命中后 MySQL 兜底丢弃并打 `rag_doc_deprecated_leak_total`。
- **E5 前缀纪律**：索引侧 `"passage: " + chunk_text`，查询侧 `"query: " + req.query`；**两侧前缀一致性进 CI 静态检查**，漏加会静默降质而非报错（§4.3 D3）。
- **512 token 截断治理**：E5 上下文 512 token 且前缀也占额度【B/C】，而现行 `StepwiseChunker(512)` 与"检验标准参数表整体保留"的 chunk 可能超限。新增指标 `rag_embedding_truncated_total`，S0 先统计**截断率与受影响 `section_type` 分布**；允许的处理只有"按 E5 tokenizer 重新切分并落 `chunk_strategy_version` 新版本号"，**不允许静默截断**（C3 幂等键含 `strategy_ver`，天然支持重切分后全量重建）。
- **不可变 chunk**：Zvec 虽支持 `upsert()` / `update()`【B】，本项目仍按 C3 禁用批量 UPDATE；升版只追加，单条软删例外。
- **重建与原子切换**：Zvec collection **完全自包含于磁盘目录、目录可自由搬移**【B】。据此设计为"写 `document_chunks_<ts>` 新目录 -> `optimize()` -> count/checksum 校验 -> 应用层路径指针（软链或配置）原子切换 -> 旧目录保留一个周期"。【设】官方是否提供原生 collection alias API 未核实，本方案不依赖它；切换必须保证读进程在切换瞬间仍持有旧 collection 的可用句柄，实现期验证。
- **WAL 的边界**：WAL 保证进程崩溃/断电不丢数据【B】，但**不等于跨多条 Doc 的业务事务**【设】；MySQL 仍是唯一权威源，`index_state` 仍由 MySQL 决定，不得因 WAL 而简化幂等与位点纪律。

### 3.7 部署形态：单写进程 + 只读副本

Zvec 明确"多个进程可同时读同一 collection，**写入为单进程独占**"【B】，这与现行 Kafka 消费者按分区并行 upsert 的假设冲突。设计如下：

| 角色 | 数量 | 职责 | 与现行口径的关系 |
|---|---|---|---|
| `IndexWriter`（单进程） | **严格 1** | 消费 Kafka 事件 / publish / rebind，串行写 Zvec，`optimize()`，校验后切换目录指针并分发 | 现行"API 与 Kafka 消费者可按进程角色拆分"的显式化；位点事务纪律不变 |
| `rag-api`（只读） | 可多副本 | 只 `zvec.open()` 只读打开本地 collection 目录，处理 `search_docs` | 对外契约不变 |

- 每个只读副本持**一份 collection 目录拷贝**，由 writer 在校验通过后分发（共享卷/对象存储拉取）；副本侧原子切换路径指针。
- 【设】目录分发的时效、失败重试与 `freshness_lag_seconds` 口径需实现期验证；现行文档对制造追溯读模型要求 ≤30s 并显式标 stale，文档索引侧目前只写"容忍秒级延迟"与 `rag_ingest_lag_seconds`，本方案须给出明确阈值后再上线。
- 【设】多副本下"某些副本仍指向旧目录"的窗口必须能被 `INDEX_UNAVAILABLE` / `degraded` 语义覆盖，不允许返回空列表冒充"无相关文档"。

> 这一点是**改进而不是彻底解决**：Zvec 把 Chroma "多进程争用"这种未定义行为，变成了"多读单写"的明确约束；但它仍是进程内库，**不是分布式共享索引**，每副本一份拷贝的运维成本依然存在。

### 3.8 契约不变清单与降级矩阵

| 项 | 保持不变 |
|---|---|
| 接口 | `POST /rag/docs/search`、`GET /rag/answers/{id}`；`SearchDocsRequest/Response` 来自共享契约包，不在服务内复制 DTO |
| 参数 | `context_id`（服务端签发 30min）、`top_k=20`、`rerank_top_n=5`、`doc_category`、`extra=forbid` |
| 证据 | `DocEvidence.locator={page,offset,heading_path}`、`quoted_text ≤1200`、`version_anchor`、`score` |
| 语义 | 缺锚点 422、锚点冲突 409 停下、`NO_MATCH` = 200 + `missing_evidence`、`RETRIEVAL_ONLY` 默认、无"回退当前生效版"分支 |
| 链路顺序 | 锚点校验 -> 编码 -> **前置过滤召回** -> reranker 精排 -> 组装证据 -> MySQL 兜底丢弃 |
| 性能 | `RAG_SEARCH_TIMEOUT_MS=2000`、检索 P95 ≤1.0s / P99 ≤2.0s、20 QPS/租户限流、超时/重试策略不变 |

| 故障 | 期望行为（与现行一致） |
|---|---|
| Zvec collection 打不开 / 为空 | 503 `INDEX_UNAVAILABLE` + `degraded=true` + `missing_evidence`，**不返回空列表** |
| Reranker 不可用 | 用向量分数排序，不改 `top_n` 语义，标 `degraded` |
| MySQL 不可用 | fail closed，不以向量库状态替代权威状态 |
| 影子索引异常 | 只影响旁路评测，生产链路零影响（S0–S2 强制） |
| E5 编码服务异常 | 与现行 embedding 异常同一处置，不引入新的降级路径 |

### 3.9 观测与评测接线

- 沿用 `TracingMiddleware` + prometheus-client + structlog，`trace_id` 贯穿；审计仍落 `context_id`、锚点、**过滤条件摘要**、命中 chunk id、耗时。
- 新增指标：`rag_vector_backend`（标签）、`rag_zvec_query_seconds`、`rag_zvec_filter_reject_total`（DSL 构造失败/字段越界）、`rag_embedding_truncated_total`、`rag_index_dir_switch_total`、`rag_index_replica_lag_seconds`。
- 评测：同一金标准集、同一 `VersionAnchorChecker`、同一 8 条必过硬门禁用例；影子结果**只入评测库不入生产答案**；质量 soft gate 以现行基线 ×0.95 为界，安全七项 hard gate 零容忍。
- 现有纪律"prompt / 模型 / 检索 / 工具变更必过评测集回归"与"`EvalGate.passed` 未过拒绝启动"对本替换同样生效。

### 3.10 迁移阶段、出口条件与回退

| 阶段 | 动作 | 出口条件 |
|---|---|---|
| **S0 离线影子** | 从 MySQL 暂存 chunk 重建 E5 影子向量 + Zvec 影子索引；生产完全不动；独立评测实例、禁用 `pytest-xdist` 并行 | 同一金标准集下 Recall@5 ≥0.80 且不低于现行基线 ×0.95、MRR ≥0.65、引用可定位率 ≥0.95；版本过滤正确率 1.00、跨租户泄漏 0、失效文档泄漏 0；产出截断率与受影响 `section_type` 报告 |
| **S1 契约适配** | `VectorStorePort` + `ZvecStore` + `build_filter()` 白名单 + 字段映射 + 目录切换；CI 加前缀一致性静态检查、DSL 注入用例、API 漂移检查 | 8 条必过用例全绿；`extra=forbid` 与 422 用例全绿；过滤"严格前置"验证通过（H2） |
| **S2 单场景灰度** | 仅 `GENERAL`（无锚点、通用知识型）单租户小流量，生产主索引仍为 Chroma；`PROCESS_BOUND` / `ASSET_BOUND` 不参与 | 降级矩阵演练 + 回退演练通过；P95 ≤1.0s；安全 hard gate 连续两轮全绿 |
| **S3 ADR 裁定** | 汇总质量、延迟、资源、运维成本与风险，走 ADR 正式记录 | 明确"扩展 / 维持试点 / 终止"三选一；未裁定不得扩大范围 |
| **S4 全面替换（条件触发）** | 见 §5 B | 仅在触发条件成立且 S0–S3 全绿后执行 |

**回退**：配置开关 `RAG_VECTOR_BACKEND` 秒级切回 `ChromaStore`；影子期与主索引期两套索引并存，切换不依赖数据迁移；双写只存在于限期灰度窗口，且必须有 `index_state=READY` 与位点一致性校验，禁止长期双写形成第三套事实源。

---

## 4. 与现行方案的优劣势对比（展开）

### 4.1 总览对照

| # | 维度 | 现行 `bge-m3 + ChromaDB` | 备选 `E5 + Zvec` | 判定 |
|---|---|---|---|---|
| 1 | 组件形态 | Python 嵌入式 PersistentClient | C++ 进程内库，无服务无端口 | 备选略优（两者都免运维服务） |
| 2 | 向量维度 | 1024 维 cosine | E5-large 1024 维，可保持 `IndexGate` 不变 | **持平**；用 small/base 则变更成本陡增 |
| 3 | 上下文长度 | bge-m3 官方模型卡支持 8192 token；现行 chunk 只用到 384–512，但"参数表整体保留"不截断 | E5 512 token 且前缀占额度 | **备选明显劣**（§4.3 D1） |
| 4 | 中文 / 制造术语 | bge-m3 中文优化，现行基线已达标 | 多语言强，中文制造术语无本仓证据 | **备选劣/待验证**（§4.3 D2） |
| 5 | 精确代号、参数表 | 现行同为单路 dense + cross-encoder 精排 | 同为单路 dense + 精排；E5 无稀疏能力 | 机制持平；未来扩展性 bge-m3 优 |
| 6 | 稀疏 / FTS / 混合检索 | 未启用（口径明确单路 dense） | 库侧原生支持，但本方案**默认不启用** | 备选有"未来选项"，当期无收益 |
| 7 | 前置过滤表达 | dict `where`，字符串精确匹配 | 类 SQL `filter` 表达式，已索引字段高效 | 备选表达力更强，但引入注入面（D4） |
| 8 | 过滤安全性 | dict 结构，无字符串拼接 | 字符串 DSL，必须白名单 + 转义 + 严格前置验证 | **备选劣** |
| 9 | 持久化 / 事务 | 批量 upsert 无事务，靠 chunk 不可变规避 | WAL 保证崩溃不丢；跨 Doc 原子性未核实【设】 | 备选略优，但**不替代 MySQL 权威源** |
| 10 | 多进程 / 多副本 | 嵌入式多进程争用，评测文档已点名风险 | 多进程读 + 单进程写，语义明确；仍需每副本一份索引 | **备选优**（可判定性） |
| 11 | 在线延迟 | ANN 数十毫秒量级，P95 预算 1.0s | ANN 亚毫秒级 | 备选优，但端到端收益被稀释（§4.5） |
| 12 | 车间网隔离 / 边缘 | 已本地化可行 | 零服务 + 跨平台 + CPU 端侧更轻 | **备选优**（本方案主要价值） |
| 13 | 生态与成熟度 | `chromadb>=0.5,<0.6` 已 pin，基线/评测/运维齐备 | 0.x（v0.8.0），API 漂移与 wheel 平台风险 | **备选明显劣**（D7） |
| 14 | 框架适配 | AgentScope `rag/` 无适配器，故自持 client | 同样无适配器；自持 client 纪律本就成立 | **持平**，不新增约束 |
| 15 | 契约改造量 | 无需改造 | `locator` 需拆字段、过滤需重写、写入角色需串行化 | **备选劣** |
| 16 | 评测与迁移成本 | 已过门禁 | 全量基线重标定 + 影子期 + 双份索引 | **备选劣** |
| 17 | 回退能力 | 现状 | `RAG_VECTOR_BACKEND` 配置秒级回退 | 持平（设计可保证） |

### 4.2 备选方案相对优势（逐条展开）

**A1. 并发语义从"未定义"变成"可判定"。** Chroma 嵌入式的多进程争用被本仓评测文档明确列为陷阱，规避方式是"独立实例 / 禁用并行 / 生产多副本需评估迁移"。Zvec 官方明确"多进程读同一 collection，写入单进程独占"【B】，使 §3.7 的单 writer + 多只读副本成为有文档依据的架构，而不是经验性约定。**边界**：这不是分布式共享索引，仍需目录分发与副本滞后治理（D6）。

**A2. 强类型 schema + 标量倒排索引，让"前置过滤"有性能抓手。** 现行 `where` 是字符串精确匹配且未建立独立索引概念；Zvec 可对 `state`、三段锚点、`tenant_scope`、`doc_type` 显式建 `InvertIndexParam`，并明确"未索引字段可查但显著变慢"【B】。这对 C2 的"过滤必须在近邻之前完成"是正向的。**边界**：过滤表达式变成字符串 DSL，安全面扩大（D4）。

**A3. 库侧能力留给未来，而不改变当期语义。** Zvec 原生支持稀疏向量、FTS（v0.8.0 起内置中文 Jieba 词典与 HMM）、混合检索与分组搜索【B】。对"故障代码 E027 怎么修""某检验项标准值"这类精确词查询，未来可另立扩展项。**边界**：现行评测基线建立在单路 dense 上，启用混合检索会改变 Recall@5 / MRR 的定义与 hard gate 判定，必须独立评测，本方案明确不启用。

**A4. 边缘 / 隔离网 / CPU-only 部署门槛更低。** 无服务、无端口、Apache-2.0、跨 Linux/macOS/Windows/Android/iOS、Python 3.10–3.14 与仓库 `>=3.12` 兼容【B】；配合 `multilingual-e5-small`（384 维）可把 embedding 也压到 CPU。**边界**：一旦用 small，维度改变触发 C1 全量重建与基线重标定，且必须重新证明 Recall@5 ≥0.80。

**A5. 崩溃持久化与可搬移索引。** WAL 保证进程崩溃/断电不丢数据；collection 自包含于目录、可自由搬移【B】，比"嵌入式文件挂卷"更容易做离线重建、备份与分发。**边界**：WAL 不等于业务事务，MySQL 权威源与位点事务一条都不能省（§3.6）。

**A6. `zg` 改善开发态体验（旁路收益）。** 本仓库是 90+ 篇设计文档，跨文档找口径时 `zg` 的"精确 + BM25 + 语义"三路检索比纯 grep 更有效；它内置的 `local/multilingual-e5-small` 正好可用来做 E5 的离线手感验证。**边界**：仅开发机，绝不接生产契约（§1.2）。

### 4.3 备选方案相对劣势与风险（逐条展开）

| ID | 风险 | 等级 | 机制 | 缓解 / 验证 |
|---|---|---|---|---|
| D1 | **512 token 截断** | 高 | E5 上下文 512 token 且前缀占额度；现行 chunk 384–512 token，"参数表整体保留"可能超限，截断会静默丢证据 | S0 统计截断率与 `section_type` 分布；按 E5 tokenizer 重切分并升 `chunk_strategy_version`；`rag_embedding_truncated_total` 期望 0，禁止静默截断 |
| D2 | **中文制造语料质量回退** | 高 | E5 是通用多语言对比学习模型；MES 语料是中文 SOP、检验标准、故障代码、物料/工艺版本号，长尾术语与代号密集；本仓**无 E5 评测证据**【推】 | 只能用自建金标准集说话：S0 影子对比 Recall@5 / MRR，未达基线 ×0.95 即终止；不引用外部榜单替代本仓结论 |
| D3 | **前缀一致性静默降质** | 中高 | E5 要求索引 `passage: ` / 查询 `query: `；漏加不报错，只是相似度系统性变差 | 前缀拼接集中在适配层单一函数，CI 静态检查 + 影子 A/B 对比 |
| D4 | **过滤 DSL 注入与"严格前置"语义** | 高（安全红线） | 现行 `where` 是 dict；Zvec `filter` 是字符串。若拼接了请求体字段，等价于把 `where_raw` 开了口子；且官方表述为"only matching documents are considered during search"，是否保证**不达标片段绝不进结果**未在本仓验证【设】 | `build_filter()` 白名单 + 字面量转义 + `extra=forbid`；S1 把"跨租户 403、v3 不得命中 v4"等用例在新后端重跑一遍，验证前才允许灰度 |
| D5 | **单写进程与 Kafka 并行消费冲突** | 中高 | Zvec 写单进程独占；现行消费者按分区并行 upsert | 显式 `IndexWriter` 单进程 + 写串行化；位点与业务变更同事务落 MySQL 的纪律不变；压测验证吞吐是否成为重索引瓶颈 |
| D6 | **多副本索引分发与 freshness** | 中高 | 每副本一份目录拷贝，切换窗口内副本可能指向不同版本 | 目录分发 + 校验 + 原子指针切换；`rag_index_replica_lag_seconds` 与显式 stale 提示；阈值需定【设】 |
| D7 | **0.x 版本与平台风险** | 中 | v0.8.0 仍在扩平台与索引能力；Python API 名存在版本差异迹象；C++ wheel 对 CPU 指令集/Windows 版本有要求；无 AgentScope 适配器 | pin 小版本 + CI API 漂移检查 + 目标平台 wheel 冒烟；自持 client 封在适配层后（C8 本就要求，非新增约束） |
| D8 | **契约改造与基线重标定成本** | 中 | `locator` dict 需拆 3 字段；过滤重写；重建/别名机制重写；模型、检索、工具描述任一变更都要过评测集回归 | 一次只换两层，reranker 与 MySQL 治理不动；影子期不动生产；所有基线重跑并留 ADR 记录 |
| D9 | **收益被端到端延迟结构稀释** | 中 | 见 §4.5 | 以压测数据作为决策输入，不以"向量库更快"作为替换理由 |

### 4.4 现行方案的相对优势与固有痛点（对称视角）

**现行方案优势（本方案难以短期追平）**

1. **基线已达标且被门禁固化**：Recall@5 ≥0.80、MRR ≥0.65、版本过滤 1.00、跨租户泄漏 0.00 已有明确定义、金标准集与 8 条必过用例；换栈不是"加个组件"，是把这些结论全部重新证明一遍。
2. **中文长文本与表格友好**：bge-m3 的长上下文让"检验标准参数表整体保留"这类 chunk 策略无需妥协；E5 需为此重切分（D1）。
3. **过滤无字符串拼接面**：`where` dict 由 `build_where()` 构造，越权注入面天然小于 DSL；现行 `extra=forbid` 明确拒绝 `where_raw`。
4. **1024 维与 `IndexGate`、别名切换、幂等键、位点事务、观测指标、降级矩阵全部已落地**，运维与排障资产完整。
5. **自持 client 纪律已把框架风险挡在适配层外**，换 Zvec 并不能消除"框架 RAG 模块无适配器"这一约束，只是换了个自持对象。

**现行方案固有痛点（本方案确有改进空间）**

1. 批量 upsert 无事务，靠 chunk 不可变 + MySQL 权威 + 别名切换"绕开"而非"解决"；
2. 多进程争用被评测文档点名，生产多副本需评估迁移；
3. `where` 只支持字符串精确匹配，不适合模糊条件；
4. 嵌入式模式受单机资源限制，边缘/CPU-only 形态不如 Zvec 轻。

> 关键判断：**痛点 1–3 都已有成文规避手段且未构成当前阻塞**，痛点 4 只在"边缘部署成为硬需求"时才变成阻塞。这正是 §5 建议"不整体替换"的核心理由。

### 4.5 收益是否被稀释：延迟瓶颈分析【推】

现行检索 P95 预算 1.0s，链路为：`context_id` 解析 -> TEI embedding -> Chroma ANN(top_k=20) -> `bge-reranker-v2-m3` cross-encoder 精排 20 条 -> MySQL 回查 -> 组装证据。按典型量级估计，ANN 只占其中很小一部分，大头在 embedding 与 **cross-encoder 精排**（20 条逐条打分）以及网络/回查。因此把 ANN 从"数十毫秒"降到"亚毫秒"，端到端 P95 的改善通常是个位数百分比，**不足以作为替换理由**【推，须以压测为准】。

结论：备选方案的价值排序应为 **部署形态 > 并发语义可判定性 > 边缘 CPU 可行性 > 库侧未来扩展能力 > 在线延迟**，而不是反过来。决策输入应是"是否真的要上边缘/单机形态 + 影子评测质量是否达标"，而非 benchmark 上的 ANN 数字。

### 4.6 场景化判定矩阵

| 场景 | 推荐栈 | 理由 |
|---|---|---|
| 生产多租户 `PROCESS_BOUND` / `ASSET_BOUND` 检索 | **现行 `bge-m3 + ChromaDB`** | 版本锚点 hard gate、基线已达标、无替换收益 |
| 车间网隔离的单机工位 / 边缘盒子（CPU-only） | **可试点 `E5-small + Zvec`** | A4 收益成立；但须独立评测、维度变更评审、不复用生产基线结论 |
| 评测影子 / 模型 A/B | **两套并存（限期）** | 独立实例、禁用并行、影子结果不入生产答案 |
| 开发态跨文档口径自查 | **`zg`（仅开发机）** | A6；与生产索引完全隔离 |
| 未来精确代号 / 长表格检索增强 | **先立扩展项，不在本次替换内** | 混合检索会改变评测语义，须独立门禁 |

---

## 5. 决策建议

**A（推荐）：保留现行栈为生产主线，本方案定位为"影子评测 + 边缘形态备选"。**
理由：① 替换收益集中在部署形态而非检索质量/在线延迟（§4.5）；② D1/D2/D4 三项高风险都直接压在 C1–C7 的硬约束与 hard gate 上；③ 现行痛点均已有成文规避手段且未被证明为当前阻塞。落地动作：执行 S0–S1，产出可复现的影子评测报告与 ADR 记录，**不扩大到 `PROCESS_BOUND`**。

**B（条件触发，需 ADR 裁定）：仅在以下任一条件成立且 S0–S3 全绿时，才讨论全面替换。**
1. 车间网隔离 / 单机 / CPU-only 部署成为产品硬需求，现行 TEI + Chroma 组合在该环境不可接受；
2. Chroma 嵌入式多进程争用被生产压测证明为不可解瓶颈，且 Zvec 的单写多读方案在同等压力下通过；
3. 影子评测显示 E5 + Zvec 在**本仓金标准集**上明显优于现行基线，且所有安全 hard gate 零失败。

**C（明确不做）：**
- 不以"Zvec benchmark 更快"为唯一理由全面替换；
- 不在同一次变更里同时换模型、换向量库、启用 FTS/混合检索、改 chunk 策略；
- 不把 `zg` 作为 `search_docs` 后端或评测事实源；
- 不因 WAL 而简化 MySQL 权威源、幂等键、位点事务与 `drop_deprecated_hits` 兜底。

---

## 6. 待验证假设与开放问题（【设】清单）

| ID | 假设 / 问题 | 验证方式 | 责任阶段 |
|---|---|---|---|
| H1 | `multilingual-e5-large` 实际输出维度、tokenizer 上限、TEI 兼容性 | 加载权重实测 `dim` 与 512 token 边界，固化模型卡快照 | S0 |
| H2 | Zvec `filter` 是否保证"不满足条件的 Doc 绝不进候选/结果"（等价 where pre-filter）；字符串字面量的引号/转义语法 | 查 pin 版本 API Reference；构造跨租户 / v3-v4 混淆与含引号值用例，检查结果集、审计与 explain | S1（阻断项） |
| H3 | Zvec 是否提供原生 collection alias / 热切换 API | 查 pin 版本 API Reference；若无，验证"目录 + 软链原子切换"在读进程不停机的可行性 | S1 |
| H4 | 跨多条 Doc 的批量写入原子性 | 故障注入（进程 kill / 断电模拟）+ 与 MySQL `index_state` 对账 | S1 |
| H5 | 目标平台（生产 Linux + 车间网 Windows）wheel 与 CPU 指令集兼容 | CI 矩阵冒烟 + 部署镜像试装 | S1 |
| H6 | 单 `IndexWriter` 的重索引吞吐是否满足 Kafka 积压与 freshness | 全量重建压测 + 位点滞后监测，定出 `freshness_lag_seconds` 阈值 | S2 |
| H7 | E5 在本仓 SOP / 检验标准 / 故障代码语料上的截断率与质量 | S0 影子评测报告（含按 `section_type` 分组） | S0（阻断项） |
| H8 | 目录分发下多副本切换窗口的 `degraded` / `INDEX_UNAVAILABLE` 行为 | 副本错版本演练 + 降级矩阵 | S2 |
| H9 | 字段名统一（`state=PUBLISHED` vs `status=ACTIVATED`；`effective_*` vs `[valid_from, valid_to]`） | 与 ADR-06、评测断言逐条对齐后固化 | S1 |

---

## 7. 参考与出处

**仓库内**
- [rag-service-整体设计.md](../RAG服务/rag-service-整体设计.md) — §2 契约、§4 版本治理、§5 摄入切分、§6 检索、§8 组件与框架风险、§10 一致性、§14 评测、§16 口径红线
- [rag-service-实现方案.md](../RAG服务/rag-service-实现方案.md) — §1 依赖、§4 切分与幂等、§5 重建、§6 `VectorRetriever` / `build_where()`、§10 硬门禁用例
- [整体技术选型与模块划分.md](../整体技术选型与模块划分.md) — §2.5 ChromaDB 风险注、§3.2 模块结构、§3.4 版本锚点传递链
- [00-架构决策记录ADR.md](../架构设计v2/00-架构决策记录ADR.md) — ADR-01（框架 API 核实）、ADR-06（版本锚定）、ADR-09（不开放任意查询）、ADR-13（编排权归属）
- [RAG与Agent评测-设计与实现方案.md](../RAG与Agent评测/RAG与Agent评测-设计与实现方案.md) — §5 指标、§6.5 CIGate
- [评测指标落地实现方案与中间件选型.md](../RAG与Agent评测/评测指标落地实现方案与中间件选型.md) — §1.3 明确不引入、§7 陷阱 1
- [制造追溯-需求与实现效果.md](../制造追溯-需求与实现效果.md) — freshness ≤30s 与锚点单向流动

**外部（【B】厂商官方）**
- Zvec GitHub README：https://github.com/alibaba/zvec
- Zvec Quickstart / 数据结构 / 条件过滤：https://zvec.org/en/docs/db/quickstart 、https://zvec.org/zh/docs/db/concepts/data-modeling 、https://zvec.org/zh/docs/db/data-operations/query/filter
- Zvec-Grep 产品定位与内置模型：https://zvec.org/en/docs/zvec-grep/ 、https://zvec.org/en/docs/zvec-grep/embedding-models
- E5 模型卡：https://huggingface.co/intfloat/multilingual-e5-large
- bge-m3 / bge-reranker-v2-m3：https://huggingface.co/BAAI/bge-m3 、https://huggingface.co/BAAI/bge-reranker-v2-m3
