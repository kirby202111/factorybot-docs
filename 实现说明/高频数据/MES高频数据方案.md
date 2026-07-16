# MES 高频数据方案

> **定位**：本文是本 MES 项目**高频数据**的权威方案。§1 给出 **MES 全高频数据全景**（哪些算高频、已覆盖还是 gap、各自如何应对）；§2 起是**设备数采**（全景第①类，最复杂、最具代表性）的完整实现设计--为什么不走 Outbox、边缘网关层、Kafka 直连管道、平台去重与乱序矫正、存储热温冷分层、限流与背压、可靠性保证、可观测。全景中②③④⑦类与设备数采**同构**、平移本文设计；⑤类（视频流）需独立通道；⑥类（衍生流）需流式计算管道，均在 §1.2 标注。
>
> **与既有文档的关系**：
> - 领域契约（聚合根 / 事件 / 不变式编号）的权威源是 [设备数据接入上下文 - 事件风暴](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md) 与 [设备数据接入上下文 - 领域建模](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)，本文**不重复定义**，只引用其 §6 不变式编号（INV-02/03/04/05/07/11/15、INV-CX-01/03/04/07/08/09、BIZ-01/02/03/04）。
> - 本文落地事件风暴 §暂缓模块中推迟到"实现说明文档"的三项：**边缘网关高可用部署形态**、**协议适配器实现细节**、**数据存储分层（热/温/冷）与压缩**。
> - 与 [Outbox设计方案](../业务事件/Outbox设计方案.md) 是**分工关系**：Outbox 管低频业务契约事件，本文管高频数据，两者复用同一 Kafka 集群、不同 topic 命名空间。
> - 对象存储细节见 [MinIO配置说明](../基础设施/MinIO配置说明.md)，Kafka 基础配置见 [Kafka配置说明](../基础设施/Kafka配置说明.md)，本文只给采集侧的差异化配置。
>
> **适用边界**：§1 覆盖 **MES 全高频数据全景**（设备数采 / 人机交互 / 环境能耗 / AGV 物流 / 视频流 / 衍生计算 / 第三方对接），标注每类的覆盖状态与应对方式。§2 起的详细实现设计聚焦**设备数采**（全景第①类）；②③④⑦类与设备数采同构、平移本文设计，⑤⑥类按 §1.2 标注的独立模式处理。低频业务契约事件（过点决策 / 工单 / 维修 / 物料账目 / 质量 / 工艺版本生效）走 Outbox，**不在本文范围**。
>
> **可靠性语义**：采集/搬运类高频数据（①②③④⑦）采用"不丢不重"（边缘缓冲 + 断点续传 + 平台 `msg_id` 去重 + 幂等消费），**不追求与业务状态强一致**--这是相对 Outbox 的刻意降级取舍（见 §2）。视频流（⑤）与衍生流（⑥）的可靠性模型见 §1.2。

---

## 0. 口径纪律

本文出现的频率、吞吐、体积、保留期数值均为**设计容量目标 + 假设**（基于典型 SMT/PCBA + Box Build 单线量级估算），不是线上实绩。所有需要按真实集群容量与车间实测量拍板的阈值用 🔴 标注，交还运维/架构决策，**不自行编造 SLA**。

---

## 1. 高频数据的来源与分类

### 1.1 什么是"高频"--与低频业务事件的边界

MES 里"高频"不是一个绝对数字，而是相对**业务契约事件**的对照。两者用不同的可靠性模型与管道处理：

| 维度 | 低频业务契约事件（走 Outbox） | 高频采集数据（走本文方案） |
|---|---|---|
| 典型来源 | 过点决策、工单状态流转、维修派工、物料账目、质量门判定、工艺版本生效 | 设备遥测、工艺参数采样、单件采集事件、老化监控、波形曲线 |
| 节奏 | 状态变化触发，单线每秒个位数~数十 | 秒级持续流 / 每板 / 每枪 / 每测试项 |
| 一致性要求 | 与业务状态原子（强一致） | 不丢不重即可（最终一致） |
| 单条价值 | 高（缺失=业务流程断裂） | 单条低（缺失一条温度采样不影响判定，靠聚合） |
| 管道 | DB 事务 + outbox_event + Publisher | 边缘缓冲 + Kafka 直连 |

**判定准则**：一条数据如果"必须和某个业务状态变更原子提交"——它是低频业务事件，走 Outbox；如果"只是观测事实的搬运、丢一条可容忍、靠聚合/重传兜底"——它是高频采集数据，走本文。设备数据接入上下文坚持的"采集只搬运不解释"（INV-CX-01）正是这条边界的体现。视频流（⑤）与衍生计算流（⑥）不属“搬运”类，其判定与应对见 §1.2 全景表。

### 1.2 MES 高频数据全景（已覆盖 vs gap）

MES 中的高频数据不止“设备数采”一类。下表给出全景，标注每类的应对方式与覆盖状态。本文 §2 起的详细实现设计聚焦第①类（设备数采，最复杂、最具代表性）；其余各类或与①同构平移、或需独立通道、或为衍生计算，均在此标注。

| # | 类别 | 典型数据 | 节奏 | 来源 | 应对方式 | 覆盖状态 |
|---|---|---|---|---|---|---|
| ① | **设备数采数据** | 设备遥测流、工艺参数采样、单件采集事件、老化监控、波形曲线 | 秒级 / 每板 / 每枪 | 设备协议（SECS/GEM / OPC-UA / Modbus / MQTT） | 边缘网关 + Kafka 直连（本文 §3–§10 详述） | ✅ 已覆盖（本文权威设计） |
| ② | **人机交互事件流** | 工位扫码、人员刷卡登录/登出、安灯（Andon）呼叫、工位操作日志 | 秒级 / 事件触发 | 工位终端 / 扫码枪 / 读卡器 | 工位 Agent + Kafka 直连（同构①） | 🟡 部分覆盖：扫码已在数采（`dc.material.event.raw`）；人员登录/工位操作日志未建模 |
| ③ | **环境与能耗监控** | 车间温湿度、洁净度（粒子计数）、ESD、压差、水电气能耗 | 秒级 / 分钟级 | 传感器 / 智能表 | 复用边缘网关 + Kafka 直连（同构①） | 🔴 gap：无 EHS / 能源管理上下文 |
| ④ | **AGV/AMR 与物流调度** | AGV 位置/电量/任务状态、输送线节拍、立体库库位变动 | 秒级 | AGV 调度系统 / PLC | AGV API / MQTT + Kafka 直连（同构①） | 🔴 gap：无物流调度上下文（物料上下文是主数据/BOM，非物流） |
| ⑤ | **视频与机器视觉流** | 车间监控视频、在线视觉检测实时图像流（区别于 AOI 单板图像） | 持续流，超大流量 | 摄像头 / 视觉主机 | **独立流媒体服务器**（RTSP/WebRTC），MES 只存片段引用 | 🔴 gap：需独立通道，不进 Kafka 主流 |
| ⑥ | **衍生计算流** | 实时 OEE/节拍/稼动率、实时 SPC 控制图点、在制品位置投影 | 秒级 / 事件触发 | 平台内基于①+过点事件二次计算 | 消费 `dc.*` 流式聚合，产出再发 Kafka | 🟡 数据源已有；实时计算管道未建模（累积归台账、分析归数据应用上下文） |
| ⑦ | **第三方系统实时对接** | SCADA 实时数据、WMS 库存变动推送、ERP 工单/库存同步 | 秒级~分钟级 | 外部系统 API / MQ | SCADA 归①；WMS/ERP 低频走 Outbox | ✅ 大部分已覆盖（实时 WMS 库位变动若高频需评估） |

**三种应对模式**：

- **同构平移（②③④⑦）**：与设备数采①同构--“边缘缓冲 + Kafka 直连 + 平台去重 + 幂等消费”，复用本文 §3–§10 的设计与 Kafka/MinIO 中间件，仅 topic 命名空间不同（如 `env.*` / `logistics.*`）。**不需要新中间件**。
- **独立通道（⑤）**：视频码流（Mbps~Gbps 级）进 Kafka 会瞬间打爆 broker，须走独立流媒体服务器（MediaMTX / 厂商 NVR 等），MES 主流只存“片段引用”（摄像头 ID + 时间窗 + 截图 URI），与 §5.4 大载荷卸载 MinIO 同一思想、不同载体。🔴 流媒体选型是独立决策。
- **衍生计算（⑥）**：不需新采集通道，缺的是**流式计算管道**（Flink / Spark Streaming / Kafka Streams 三选一 🔴）。当前可不引入--用定时聚合班次级 OEE 兜底，精度够用；待实时 SPC/OEE 看板成为刚需再上。

> **🔴 的两种含义**：本表 🔴 表示“该类高频数据当前未建模限界上下文（scope gap）”，区别于 §0 / 附录 A 中表示“阈值待按集群容量拍板”的 🔴。前者是后续是否新建上下文的范围决策，后者是当前设计的参数决策。

### 1.3 设备数采的形态细分（全景第①类的展开）

设备数采（全景第①类）按**数据形态与节奏**再分五类，决定了它们在管道里的去向（主流 Kafka vs 对象存储 MinIO）与存储分层：

| 类别 | 典型数据 | 节奏 | 单条体积 | 主流去向 | 对应领域事件（§5.6） |
|---|---|---|---|---|---|
| **A. 设备遥测流** | 波峰焊温区温度、回流焊温区、链速、设备状态 running/idle/fault | 秒级 | 小（结构化 KV） | Kafka `dc.process.sample.raw` / `dc.station.event.raw` | `Wave*Sampled` / `PrinterStatusReported` |
| **B. 工艺参数采样** | 印刷 speed/pressure/angle/demold、测试项测量值 | 每板 / 每测试项 | 小（结构化 KV） | Kafka `dc.process.sample.raw` / `dc.station.event.raw` | `PrinterProcessParamSampled` / `TestItemMeasured` |
| **C. 单件级离散事件** | 过板、拧紧每枪、烧录会话、AOI 检测结果、测试会话完成 | 每板 / 每枪 / 每件 | 中（结构化 + 可能带 URI） | Kafka `dc.station.event.raw` / `dc.identity.sn.minted` | `PrinterCycleObserved` / `FastenerTightened` / `SerialNumberMarked` |
| **D. 周期性监控** | 老化房温度/湿度、老化单件电气量 | 分钟级 | 小 | Kafka `dc.process.sample.raw` / `dc.station.event.raw` | `AgingEnvironmentSampled` / `AgingDeviceMeasured` |
| **E. 高密度波形/大文件** | 扭矩曲线（数百点/枪）、AOI 图像、烧录日志、振动频谱 | 每件 | 大（KB~MB） | **MinIO 对象存储**，主流只传 URI + `sha256`（INV-15） | `curve_uri` / `image_uri` / `log_uri` 字段 |

> **A/B/D 是"窄而快"的结构化流**，走 Kafka 主流；**E 是"大而慢"的附件**，走 MinIO，Kafka 只承载引用。**C 介于之间**：结构化部分进主流，若附带曲线/图像/日志则引用走 MinIO。这个分流是吞吐与成本的核心折衷，由领域模型 `PayloadKind`（STRUCTURED / OBJECT_STORAGE）固化（领域建模 §2.12）。

### 1.4 按接入形态的设备分类（承接事件风暴 §1）

上述五类数据来自四类接入形态的设备，差异在**事件载荷**而非**骨干链路**（所有设备走同一条 设备->网关->Kafka->平台 骨干，见 §3）：

| 接入形态 | 典型设备 | 主要产出数据类别 | 典型协议 |
|---|---|---|---|
| A. 工业设备（标准协议） | 锡膏印刷机、AOI、波峰焊 | A 遥测流 + B 工艺采样 + C 过板事件 | SECS/GEM、OPC-UA、Modbus TCP |
| B. 标识/打码设备 | 镭雕机 | C 单件事件（SN 落版） | TCP Socket、串口、共享目录 |
| C. PC 型工站 | 烧录电脑、测试工装电脑 | C 单件事件 + E 日志/曲线 | 本地 Agent + HTTP/REST、共享盘 CSV/XML |
| D. IoT/控制器型 | 智能电批、线边仓、老化房 | C 每枪事件 + D 周期监控 + E 扭矩曲线 | MQTT、WebSocket、PLC over OPC-UA |

### 1.5 量级假设（🔴 待实测）

单条线的高频数据量级估算（设计假设，非实绩）：

| 数据类别 | 单线估算速率 | 说明 |
|---|---|---|
| 设备遥测流（A 类） | 数十~百 条/s | 波峰焊 ~8 温区 × 1Hz + 状态流；回流焊类似 |
| 工艺参数采样（B 类） | 数 条/s | 每板一次，板节拍秒级 |
| 单件离散事件（C 类） | 数 条/s | 取决于线体节拍与紧固点数 |
| 周期监控（D 类，老化房） | 数 条/min | 但持续数小时~数天，总量大 |
| 波形/大文件（E 类） | 数 MB/件 | 不进 Kafka，进 MinIO |

🔴 真实速率需按车间实测设备数 × 协议轮询周期核定，是 §5 分区数、§8 限流参数、§7 存储容量的输入。

---

## 2. 设计总纲：为什么高频数据不走 Outbox

### 2.1 架构级排除

[Outbox设计方案 §9.1](../业务事件/Outbox设计方案.md) 已给出根本理由：Outbox 每条事件 = 一次 `outbox_event` INSERT + Publisher 至少一次 UPDATE，靠 MySQL 本地事务买强一致。若高频采集也走 Outbox，秒级持续流会把 `outbox_event` 写爆（写放大 + 行锁竞争 + 复制延迟），Outbox 限流的四类压力全部恶化。

**结论：高频采集走"边缘缓冲 + Kafka 直连"，可靠性模型从"同事务原子"降级为"不丢不重"。** 这条架构级排除是 Outbox 不被写爆的根本前提，也是本文存在的理由。

### 2.2 可靠性取舍

| 可靠性维度 | Outbox（业务事件） | 本方案（采集数据） | 实现手段 |
|---|---|---|---|
| 不丢 | DB 事务原子 | 边缘缓冲 + 断点续传 | `EdgeBuffered` -> `BackfillEnqueued` -> `BackfillCompleted`（INV-05 先补后新） |
| 不重 | `event_id` 幂等 | 平台 `msg_id` 去重 + 消费端幂等 | `PlatformIngestionService` 去重表（BIZ-02）+ 消费端 `msg_id` 幂等 |
| 顺序 | 分区内有序 | 分区内有序 + 乱序矫正窗口 | `partition_key=equipment_id` + `LateArrivalReordered`（打 LATE 质量标） |
| 与业务状态原子 | 是 | **否**（刻意降级） | 采集只搬运不解释（INV-CX-01），不与过点判定同事务 |

> **关键认知**：采集数据"丢一条可容忍"不是放任丢失，而是**单条价值低 + 可重传 + 靠聚合兜底**。波峰焊丢一个 1Hz 温度采样，不影响过点判定（过点查的是实时快照，§7.1），SPC 靠的是统计聚合不是单点。这与业务事件"丢一条=流程断裂"截然不同，是降级取舍成立的业务前提。

### 2.3 三大可靠性支柱

```text
① 边缘缓冲 + 断点续传  ──  连接异常时本地缓存，恢复后按游标补传（INV-05）
② 平台 msg_id 去重      ──  补传/重发产生的重复在平台侧丢弃（BIZ-02）
③ 消费端幂等            ──  漏网的重复在消费方按 msg_id 幂等吸收
```

三者叠加等效"不丢不重"。任何一环崩溃，下一环兜底。

---

## 3. 整体架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│  车间现场设备（A/B/C/D 类）                                            │
│   锡膏印刷机 / AOI / 波峰焊 / 镭雕机 / 烧录电脑 / 电批 / 老化房 ...    │
└──────────────┬──────────────────────────────────────────────────────┘
               │ 原始信号（SECS/GEM / OPC-UA / Modbus / MQTT / 文件 / 串口）
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  边缘网关 EdgeGateway（部署在产线侧，靠近设备）                         │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
│   │ProtocolAdapter│ │  DataPacket  │  │BackfillCursor│             │
│   │ 协议解码      │->│ 打标/封装     │->│ 边缘缓冲/补传 │             │
│   │(DecodingStrategy)│ │(msg_id,ts三元组)│ │(INV-05 先补后新)│            │
│   └──────────────┘  └──────┬───────┘  └──────┬───────┘             │
│                            │ 大载荷(E类)       │                      │
│                            ▼                  │                      │
│                   ┌────────────────┐         │                      │
│                   │  MinIO 直传     │         │                      │
│                   │ (预签名PUT,曲线/│         │                      │
│                   │  图像/日志,INV-15)│        │                      │
│                   └────────┬───────┘         │                      │
│                            │ object_uri       │                      │
│                            └────────┐         │                      │
│                                     ▼         ▼                      │
│                            ┌──────────────────────┐                  │
│                            │  上行投递(连接正常)    │                  │
│                            │  断连缓冲(连接异常)    │                  │
│                            └──────────┬───────────┘                  │
└───────────────────────────────────────┼──────────────────────────────┘
                                        │ Kafka 直连（不经 outbox 表、不开 DB 事务）
                                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Kafka 集群（dc.* 命名空间，与 mes.*/eam.* 业务事件隔离）              │
│   dc.process.sample.raw | dc.station.event.raw | dc.identity.sn.minted│
│   dc.equipment.lifecycle | dc.equipment.runtime | dc.equipment.alarm.raw│
│   dc.material.event.raw | dc.gateway.health                         │
└──────────────────────────────────────┬──────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  平台接入 PlatformIngestionService                                    │
│   PacketReceived -> Deduplicate(msg_id) -> BufferReorder -> PersistAndPublish│
│                    (BIZ-02去重)        (乱序矫正,LATE标)  (落库+分发)   │
└──────────────────────────────────────┬──────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  存储分层（§7）                                                       │
│   热层：实时快照(Redis/内存) ← 过点实时查询 ≤200ms (INV-CX-04)        │
│   温层：近期历史/SPC（当前 MySQL 结构化；TSDB 🔴 演进）                │
│   冷层：长期归档（对象存储/数据湖）                                     │
└──────────────────────────────────────┬──────────────────────────────┘
                                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  下游消费方（上下文外，只暴露主题契约）                                 │
│   过点执行上下文 / 质量上下文 / 物料上下文 / 台账上下文 / 数据应用      │
│   消费端按 msg_id 幂等                                                │
└─────────────────────────────────────────────────────────────────────┘
```

骨干链路与领域事件对应 [事件风暴 §2.3](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md)，本文不重复事件清单，只展开实现层。

---

## 4. 边缘网关层

> 本节落地事件风暴 §暂缓模块的"边缘网关高可用部署形态"与"协议适配器实现细节"。网关的领域契约（`EdgeGateway` / `ProtocolAdapter` / `CollectionChannel` / `BackfillCursor` 聚合、事件、不变式）见 [领域建模 §1.1-1.5](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)，本文只给实现选型与部署。

### 4.1 网关职责（实现视角）

边缘网关是部署在产线侧、靠近设备的进程，承担五件事：

1. **协议接入**：按设备协议（SECS/GEM / OPC-UA / Modbus / MQTT / TCP / 串口 / 文件扫描）与设备握手、维持长连接、心跳。
2. **协议解码**：经 `ProtocolAdapter` + `DecodingStrategy` 把原始帧结构化为 `DataPoint` / 设备特定事件（**只搬运不解释**，INV-CX-01）。
3. **封装与打标**：`DataPacket.seal` 固化 `msg_id` + 时间戳三元组 + 质量标（INV-04）。
4. **大载荷卸载**：E 类数据（曲线/图像/日志）经预签名 PUT 直传 MinIO，主流只留 `object_uri` + `sha256`（INV-15，见 §5.4）。
5. **边缘缓冲与断点续传**：连接异常时写本地缓冲，恢复后按 `BackfillCursor` 补传（INV-05）。

> 网关**不做业务判定**（PASS/FAIL 归质量上下文）、**不做资产停用**（归台账上下文）、**不推进过站**（归过点执行上下文）。越界是腐败源（事件风暴 §0）。

### 4.2 协议适配实现（DecodingStrategy 策略模式）

领域模型已把解码设计为策略模式（领域建模 §2.7 `DecodingStrategyRef`）：`ProtocolAdapter` 持有 `ProtocolProfile`（节点映射、地址表），按 `protocol_type + device_classification` 委托给对应 `DecodingStrategy`（ISP：适配器不实现各设备解码细节）。实现层落地：

```text
ProtocolAdapter（聚合根，持有 ProtocolProfile）
   │
   ├── decodeFrame(raw_frame)  ── 纯方法，委托 DecodingStrategy
   │
   └── DecodingStrategy（策略接口，按设备类型实现）
          ├── PrinterSecsGemStrategy      锡膏印刷机（SECS/GEM）
          ├── AoiSecsGemStrategy          AOI（SECS/GEM）
          ├── WaveOpcUaStrategy           波峰焊（OPC-UA 节点订阅）
          ├── MarkerTcpStrategy           镭雕机（TCP Socket 应答）
          ├── ProgrammerAgentStrategy     烧录电脑（Agent + HTTP 回调 + 文件扫描）
          ├── TorqueMqttStrategy          智能电批（MQTT）
          ├── WarehouseApiStrategy        线边仓（HTTP/REST）
          └── AgingChamberOpcUaStrategy   老化房（OPC-UA 周期采样）
```

**实现要点**：

- `DecodingStrategy` 是**纯计算**（输入原始帧 + ProtocolProfile，输出 `DataPointDecoded` + 设备特定事件），无副作用、可单测。这与点检上下文 `TriggerCondition.assess` 下沉同构（领域建模 §1.3）。
- `ProtocolProfile` 可热重载（`reloadProtocolProfile`），仅协议相关字段变更才触发，不中断通道（INV 承接事件风暴 §2.5）。
- 🔴 **协议栈选型**：SECS/GEM（jSECSON / Cimetron CIMConnect 类库）、OPC-UA（Eclipse Milo）、Modbus（j2mod / modbus4j）、MQTT（Paho）等客户端库选型归实现决策，按设备实际协议栈与许可成本定。也可选现成工业网关产品（见 §4.6）省去自研协议栈。

### 4.3 边缘本地缓冲

连接异常（设备 `OFFLINE` / 通道 `DEGRADED`，`entered_buffer_mode=true`）时，报文写本地缓冲，待恢复后补传。

| 维度 | 设计 | 说明 |
|---|---|---|
| 缓冲载体 | 🔴 本地持久化存储（RocksDB / SQLite / 磁盘顺序文件 三选一） | 需兼顾写吞吐与游标查询；RocksDB 适合高写吞吐，SQLite 适合简单部署，磁盘文件最轻量 |
| 游标机制 | `BackfillCursor`（`buffered_cursor` / `backfilled_cursor` 单调递增，INV-05） | 恢复后 `beginResume` -> 按位点顺序 `advance` -> `complete`，先补齐再放新 |
| 缓冲容量 | 🔴 按最长预估断连时长 × 峰值速率估算 | 断网超容量后的丢弃策略需定（打 BAD 质量标 + 告警，还是仅告警） |
| 缓冲清理 | 补传完成后可清 | `BackfillCompleted` 后释放对应位点 |

> **"先补后新"（INV-05）的实现**：恢复后 `resuming=true` 期间，新报文必须排在补传队列之后。由 `buffered_cursor` / `backfilled_cursor` 单调性与 `BackfillService` 顺序保证（领域建模 §1.5）。补传数据时间戳已在原始区间内，运行时长聚合去重（BIZ-03）。

### 4.4 断点续传时序

```text
连接正常:  DataPacket.seal -> dispatch() -> PacketDispatched -> Kafka 直连
                                      └ (大载荷) MinIO 直传 -> object_uri 入主流

连接异常:  DataPacket.seal -> enqueueBackfill(cursor) -> BackfillEnqueued -> 本地缓冲
                                                                     (buffered_cursor++)

恢复重连:  ReconnectService.onReconnectSucceeded
             -> BackfillService.onChannelRestored
                 -> BackfillCursor.beginResume -> BackfillResuming
                 -> 按 cursor 顺序逐条 advance -> (重发 Kafka)
                 -> backfilled_cursor == buffered_cursor -> complete -> BackfillCompleted
             -> Equipment.recoverEquipment / clearSuspect (解除降级)
```

> 若 Kafka 已发送成功但网关宕机前未推进 `backfilled_cursor`，恢复后会**重复发送同一 `msg_id`**，平台去重吸收（§6，BIZ-02）。这是"不丢"换"可能重复"的取舍，由去重兜底。

### 4.5 网关高可用部署形态 🔴

网关是采集链路的单点--它宕机则所属设备全部断采（虽有本地缓冲兜底，但缓冲也随网关进程消失）。部署形态需定：

| 形态 | 说明 | 适用 |
|---|---|---|
| 单网关 + 本地缓冲 | 一台网关管一条线若干设备；宕机靠本地缓冲（若缓冲在独立磁盘可保留） | 低成本，单线 |
| 主备（active-standby） | 主网关采，备机热备；主挂则备接管，共享/迁移缓冲游标 | 关键线体，🔴 切换 RTO 待定 |
| 网关集群 | 多网关分摊设备，某网关挂则其设备由其它网关接管（需设备协议支持重连） | 大规模车间 |

🔴 **默认形态与切换策略待定**，按车间规模与可用性预算决策。注意：网关 HA 不能破坏 BIZ-01（同一 `equipment_id` 同时只能有一个活跃 `CollectionChannel`）--接管时必须先确认原通道已 CLOSED/DEGRADED，由 `(equipment_id, status=ACTIVE)` 唯一索引保证。

### 4.6 自研 vs 选型 🔴

边缘网关可自研（Spring Boot / Go 轻量服务 + 上述协议库），也可选现成工业边缘网关产品：

| 选项 | 优势 | 劣势 |
|---|---|---|
| 自研 | 与领域模型（`ProtocolAdapter`/`DecodingStrategy`）贴合；协议画像热重载自主；无许可成本 | 协议栈自研量大（尤其 SECS/GEM） |
| EdgeX Foundry | 开源边缘框架，插件化协议南向；社区生态 | 需适配本系统领域事件契约 |
| Neuron / Kepware 等 | 工业协议网关成熟产品，开箱即用 | 许可成本；事件契约需适配层翻译为本系统 `dc.*` 事件 |

🔴 **选型待定**。无论自研还是选型，对外都必须产出本系统领域事件契约（`dc.*` 主题 + INV 不变式），选型产品需加 ACL 适配层把其私有报文翻译为 `DataPacket`。

---

## 5. Kafka 直连管道

### 5.1 Topic 命名空间隔离

采集数据复用业务事件的同一个 Kafka 集群，但用**独立命名空间** `dc.*`（data collection）隔离，保留策略也与业务事件不同（不混在同一 topic，见 [Outbox设计方案 §7.1](../业务事件/Outbox设计方案.md)）。主题契约见 [事件风暴 §对外契约](../../领域模型/设备管理服务/事件风暴/设备数据接入上下文.md)：

| 主题 | 内容 | 性质 |
|---|---|---|
| `dc.process.sample.raw` | 工艺参数原始采样（印刷/波峰焊/老化环境） | A/B/D 类高频窄流 |
| `dc.station.event.raw` | 工位会话级事件 + 工装计数 | C 类单件事件 |
| `dc.identity.sn.minted` | 镭雕 SN 落版 | C 类（SN 主键源头） |
| `dc.equipment.lifecycle` | 设备上线/离线/恢复/通道升级 | 低频（相对采集） |
| `dc.equipment.runtime` | 运行时长周期聚合 | 班次级 |
| `dc.equipment.alarm.raw` | 设备报警归一化 | 事件触发 |
| `dc.material.event.raw` | 线边仓出入库 / MSD | 事件触发 |
| `dc.gateway.health` | 网关与通道健康度 | 监控用 |

> `.raw` 后缀强调"未做业务解释"（INV-CX-01）。业务事件 topic（`mes.*`/`eam.*`/`mfg.*`）与采集 topic（`dc.*`）命名空间隔离，便于差异化保留策略与配额（§8.4）。

### 5.2 采集 topic 配置（差异化）

基础配置沿用 [Kafka配置说明](../基础设施/Kafka配置说明.md)，采集侧的差异化：

```bash
kafka-topics.sh --create \
  --topic dc.process.sample.raw \
  --bootstrap-server kafka-1:9092,kafka-2:9092,kafka-3:9092 \
  --partitions 24 \
  --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=259200000 \
  --config cleanup.policy=delete \
  --config segment.bytes=536870912 \
  --config compression.type=producer
```

| 参数 | 采集侧建议 | 与业务事件差异 | 说明 |
|---|---:|---|---|
| `partitions` | 12/24 起步 🔴 | 采集侧分区数通常高于业务事件 | 高吞吐 + 消费并行度；按 §1.5 实测量与网关数定 |
| `retention.ms` | 3~7 天 🔴 | 短于业务事件（7~30 天） | 采集数据价值密度低，靠温/冷层长期保存，Kafka 只做短期缓冲与重放窗口 |
| `cleanup.policy` | `delete` | 同 | 采集流不做 compact |
| `segment.bytes` | 512MB | 可大于默认 | 高写入量下大段减少段切换开销 |
| `compression.type` | `producer`（Producer 用 zstd） | 同 | 采集数据重复模式多，zstd 压缩比高 |

🔴 分区数与保留期需按 §1.5 实测速率 + 下游消费并行度 + 磁盘容量核定。

### 5.3 采集 Producer 配置（高吞吐）

采集 Producer 与业务事件 Producer（Outbox §7.4）的差异在**吞吐优先**（业务事件是可靠性优先）：

```yaml
spring:
  kafka:
    producer:   # 采集专用 Producer（client-id 区分，便于集群配额）
      client-id: ${spring.application.name}-dc
      acks: all
      retries: 10
      properties:
        enable.idempotence: true
        max.in.flight.requests.per.connection: 5
        delivery.timeout.ms: 120000
        linger.ms: 50           # 采集侧偏大，攒批提吞吐
        batch.size: 131072      # 128KB，大于业务事件默认
        compression.type: zstd
        buffer.memory: 67108864 # 64MB，高吞吐下防 block
```

| 配置 | 采集侧 | 业务事件侧 | 理由 |
|---|---:|---:|---|
| `linger.ms` | 50 | 5~20 | 采集容忍亚秒延迟，换吞吐 |
| `batch.size` | 128KB | 32KB | 窄结构化流攒大批更高效 |
| `buffer.memory` | 64MB | 默认 | 高并发发送防 Producer 阻塞 |
| `acks` / `enable.idempotence` | all / true | all / true | 两者都不降级可靠性 |

> 采集侧用**独立 client-id**（`*-dc`），便于 Kafka 集群级配额按采集/业务分别限流（§8.4）。Producer 仍开幂等，降低内部重试导致的重复写入，但**不能消除**断点续传导致的重复（§6 去重兜底）。

### 5.4 大载荷卸载到 MinIO（INV-15）

E 类数据（扭矩曲线、AOI 图像、烧录日志）**不进 Kafka**（Kafka 单消息默认 1MB 上限，见 [Kafka配置说明 §2](../基础设施/Kafka配置说明.md)），走 MinIO 对象存储，主流 `DataPacket` 只承载 `object_uri` + `sha256`：

```text
网关侧:
  1. 解析出大载荷 -> 计算 sha256
  2. 向平台请求预签名 PUT URL（或用 STS 临时凭证）
  3. 直传 MinIO（大文件走 multipart 分片续传）
  4. 上传成功 + sha256 校验通过 -> DataPacket.seal(object_uri, sha256)
  5. 主流 PacketDispatched 只带 object_uri，不发字节

平台/消费侧:
  1. 需要曲线/图像时按 object_uri 预签名 GET 拉取
  2. 浏览器直连 MinIO，流量不经过业务服务器
```

MinIO 的 bucket 规划（`dc/{kind}/{equipment_id}/{yyyy-MM-dd}/{msg_id}.{ext}`）、前缀保留期（`curve/` `aoi-image/` `log/` `deadletter/`）、预签名、multipart、CORS 等见 [MinIO配置说明](../基础设施/MinIO配置说明.md)，本文不重复。关键约束：**对象先于引用存在**--上传成功 + 校验通过后才 `seal` 报文（MinIO §6.4），否则会出现"引用指向空对象"。

### 5.5 分区键与顺序

`partition_key = equipment_id`（同聚合根用相同 key，落同一分区保序，见 [Outbox设计方案 §7.2](../业务事件/Outbox设计方案.md)）。同一设备的采集事件按 `source_ts` 有序，便于下游按时间窗口聚合。**禁止**用随机 UUID 作 key。

> 采集侧顺序是"同设备内有序"，不要求跨设备全局有序。乱序到达由平台 `BufferReorder` 矫正（§6）。

---

## 6. 平台接收：去重与乱序矫正

平台侧 `PlatformIngestionService`（领域建模 §3.6）编排四步：`PacketReceived -> Deduplicate -> BufferReorder -> PersistAndPublish`。

### 6.1 msg_id 去重（BIZ-02）

补传/重发会产生重复 `msg_id`，平台侧按 `msg_id` 去重：

```text
PacketReceived(msg_id, ...)
  -> 查去重表
      命中  -> DuplicateDiscarded        (丢弃，不落库不分发)
      未命中 -> PacketAccepted
              -> INSERT 去重表(msg_id)   (占位，防并发重复)
              -> 进入乱序矫正 / 落库 / 分发
```

去重存储选型 🔴：

| 方案 | 说明 | 适用 |
|---|---|---|
| Redis 布隆过滤器 + DB 唯一索引 | 布隆过滤快速判重（少量假阳性回退 DB），DB `msg_id` 唯一索引兜底 | 高吞吐，🔴 主推 |
| 仅 DB 唯一索引 | `msg_id` 唯一索引，INSERT 冲突即判重 | 简单，吞吐受 DB 限制 |
| Redis SET + 异步落 DB | Redis 判重快，DB 异步持久化 | 需处理 Redis 与 DB 一致窗口 |

🔴 **去重表保留期**需 ≥ 最长补传窗口 + 安全余量（建议 ≥ 7 天，与死信保留窗口对齐）。布隆过滤器需定期重建或用计数布隆避免无限增长。

### 6.2 乱序矫正

网络抖动/补传会导致 `source_ts` 乱序到达。平台维护**按设备的时间窗口缓冲**：

```text
BufferReorder:
  按 equipment_id 分组，维护近期 source_ts 窗口
  到达报文:
    source_ts 在窗口内且有序 -> 直接通过
    source_ts 落后于窗口已提交位点 -> LateArrivalReordered（打 LATE 质量标，INV-04）补入
    source_ts 超前 -> 缓冲等待中间报文，超时则放行（避免无限阻塞）
```

| 参数 | 设计目标 🔴 | 说明 |
|---|---|---|
| 乱序窗口 | 数百 ms ~ 数秒 | 按网络抖动实测定；过大增延迟，过小乱序矫正失效 |
| 超时放行阈值 | 窗口的 2~3 倍 | 防止缺失报文导致窗口永不推进 |

> 乱序矫正只对**需要严格时序的聚合**（如 SPC、运行时长）有意义；纯离散事件（过板、拧紧）按到达顺序处理即可，可绕过窗口。🔴 哪些 topic 启用乱序窗口待定。

### 6.3 落库与分发

去重 + 矫正通过后 `PersistAndPublish`：

- `DataPacket.markPersisted` -> `PacketPersisted`（落温层存储，§7.2）
- `DataPacket.markPublished` -> `PacketPublished`（分发到下游 `dc.*` 主题供上下文外消费）

> 平台落库与分发**不在同一事务**（Kafka 发送不与 DB 事务原子）--这正是采集侧的可靠性模型：落库成功但分发前宕机，靠 Kafka 重投 + 消费端 `msg_id` 幂等兜底（与 Outbox 的可靠性边界同理，见 [Outbox设计方案 §7.8](../业务事件/Outbox设计方案.md)）。

---

## 7. 存储分层（热/温/冷）

> 本节落地事件风暴 §暂缓模块的"数据存储分层（热/温/冷）与压缩算法选择"。当前系统状态：**无独立时序库**，测试数据落 MySQL 结构化表（[领域建模 §5.6.6](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)）。本文给出分层框架，并把时序库引入作为 🔴 演进决策。

### 7.1 热层：实时快照（过点查询）

过点时校验设备实时状态（当前温度、当前程序版本）要求 ≤200ms（INV-CX-04，领域建模 §4.8 `DataQueryAppService.getRealtimeDataPoints`）。这不能走 Kafka 回放或 MySQL 查询，需内存级快照：

| 维度 | 设计 |
|---|---|
| 载体 | 🔴 Redis（按 `equipment_id` hash 维护各设备最新数据点） |
| 写入 | 平台 `PersistAndPublish` 时同步刷新热层最新值（覆盖写） |
| 查询 | 过点执行上下文 REST 查 `DataQueryAppService` -> 读 Redis |
| 保留 | 只保留最新值（或最近 N 个采样），不存历史 |
| 失效 | 设备 `OFFLINE` 后保留最后值 + 标记离线时间；`CLOSED` 终态后清除 |

> 热层是**读优化投影**，不是事实来源--事实来源是 Kafka 与温层。热层丢失可从温层重建。这与领域总览 §5.2"设备状态缓存未命中降级查询"的思路一致。

### 7.2 温层：近期历史与 SPC

近期历史供追溯查询、SPC 分析、异常排查。当前落 MySQL 结构化表：

| 数据类别 | 当前存储 | 说明 |
|---|---|---|
| 测试项明细 | MySQL `test_item_measurement` 明细表（每测试项一行，领域建模 §5.6.6） | 无独立时序库，落结构化表 |
| 工艺参数采样 | 🔴 MySQL 结构化表 / 未来迁 TSDB | 当前按 `equipment_id + logical_name + source_ts` 落表 |
| 单件离散事件 | MySQL 结构化表 | 过板/拧紧/烧录等会话级事件 |
| 设备遥测流 | 🔴 MySQL / 未来迁 TSDB | 秒级流，量大，是 TSDB 首要候选 |

**当前 MySQL 方案的边界**：MES 低频业务事件 + 单件级采集事件落 MySQL 可承受；**秒级遥测流 + 老化房长期监控**落 MySQL 会撑爆（写放大 + 查询慢）。这是引入 TSDB 的触发点。

### 7.3 冷层：长期归档

超温层保留期的数据归档到冷层，供长期追溯与审计（如产品召回时查半年前的工艺参数）：

| 维度 | 设计 |
|---|---|
| 载体 | 🔴 对象存储（MinIO，复用现有）/ 数据湖（Parquet/ORC） |
| 迁移 | 温层超期数据按天/班次归档为列式文件 |
| 查询 | 离线分析（Spark / 数据应用上下文），不支撑在线低延迟查询 |
| 保留 | 按审计/法规要求 🔴（电子制造通常 1~3 年可追溯） |

> 老化房数据（§5.6.9）"极易撑爆时序库"--冷层归档 + 降采样是关键。原始数据落冷热分层，是否降采样/聚合由数据存储策略统一规则，不在事件载荷"提前聚合"（INV-CX-01）。

### 7.4 时序库引入决策 🔴

当前"无独立时序库"是阶段性现状。当秒级遥测流 + 老化房监控的写入量超过 MySQL 承受阈值（🔴 阈值待定，参考：单表日增超千万行或查询 p99 超秒级）时，引入专用时序库：

| 候选 | 优势 | 劣势 | 适用 |
|---|---|---|---|
| TDengine | 国产、超高性能、SQL 风格、内置降采样 | 生态较新 | 设备遥测流首选候选 🔴 |
| InfluxDB | 生态成熟、 flux/SQL 查询 | 高版本集群收费 | 通用时序 |
| IoTDB | 国产、工业物联网定位、Apache | 生态较新 | 工业时序 |
| ClickHouse | 列式 OLAP、查询极快、压缩比高 | 非纯时序、写入需攒批 | 分析型查询重于写入时 |

🔴 **选型与引入时机待定**，建议：先用 MySQL 落结构化采集数据跑通链路，待实测写入量接近阈值时按 §1.5 量级评估引入。引入时只迁"秒级遥测流 + 老化房监控"到 TSDB，单件离散事件与测试项明细仍留 MySQL（它们是结构化业务数据，关系查询多）。

### 7.5 降采样与压缩

- **降采样**：秒级遥测流在温层保留原始值 N 天后，降采样为分钟级均值/极值存冷层。🔴 降采样策略（保留原始多久、降采样粒度、聚合函数）归数据存储策略统一规则。
- **压缩**：Kafka 侧用 zstd（§5.3）；冷层列式文件用 Parquet + zstd/snappy；MySQL 历史表可按月分区便于归档。

---

## 8. 限流与背压

> 采集侧限流与 [Outbox设计方案 §9](../业务事件/Outbox设计方案.md) 业务事件限流**互补但不同**：业务事件限流保护 Outbox 表与业务 DB；采集限流保护网关内存、Kafka、平台去重与温层存储。两者都靠集群级配额兜底（§8.4）。

### 8.1 四类压力（采集侧）

| 压力来源 | 触发场景 | 不限流后果 | 限流手段 |
|---|---|---|---|
| **网关上行** | 大量设备同时上报 / 批量过板 | 网关内存/Producer 缓冲 OOM | 网关攒批发送 + 在途上限 |
| **Kafka 写入** | 采集 topic 突发写入 | broker 击穿、拖垮同集群业务 topic | Producer 令牌桶 + 集群配额 |
| **平台去重** | 补传排空 / 突发到达 | 去重表写放大、布隆过滤膨胀 | 去重吞吐上限 + 背压到网关 |
| **温层写入** | 高频流持续落库 | MySQL/TSDB 写放大、查询慢 | 落库批量化 + 写入限流 |

### 8.2 网关侧限流

- **攒批发送**：`linger.ms` + `batch.size`（§5.3）天然限流并提吞吐。
- **在途上限**：`Semaphore` 限制未完成 ack 的 `producer.send()` 数，防慢 broker 下 Future 堆积 OOM（与 Outbox §9.2 ④ 同构）。
- **本地缓冲水位背压**：`entered_buffer_mode` 后缓冲水位超阈值时，向设备侧降速（若协议支持）或打 BAD 质量标告警。

### 8.3 平台侧限流

- **去重吞吐上限**：`PlatformIngestionService` 限制单实例去重 QPS，超限则背压（Kafka consumer pause，与 Outbox §9.3 ④ 慢消费者 pause 同构）。
- **落库批量化**：温层写入攒批 INSERT（如测试项明细批量插入），减少 DB 往返。
- **写入限流**：温层 DB 写入令牌桶，保护 MySQL/TSDB 不被采集流写爆。

### 8.4 Kafka 集群级配额（兜底）

为采集 client-id（`*-dc`）单独设 `producer_byte_rate` 配额 🔴，与业务事件 client-id 配额隔离，防止采集突发把共享 Kafka 集群打爆、拖累业务事件（见 [Outbox设计方案 §9.4](../业务事件/Outbox设计方案.md)）。broker 通过延迟响应限速，Producer 自然退避。

### 8.5 限流与顺序的兼容

采集侧限流只节流"发多快/写多快"，**不改变分区内顺序**（`partition_key=equipment_id` + 幂等 Producer 保证）。与 Outbox §9.5 同理：限流是速率维度，顺序是分区维度，两者正交。

---

## 9. 可靠性保证

### 9.1 不丢

- 边缘缓冲：连接异常时写本地缓冲（`EdgeBuffered` / `BackfillEnqueued`）。
- 断点续传：恢复后按 `BackfillCursor` 补传，先补后新（INV-05）。
- Kafka `acks=all` + `min.insync.replicas=2`：broker 层不丢。
- 死信兜底：解码失败的原始终字节保留 7 天滚动窗口（INV-07），可回放。

### 9.2 不重

- 平台 `msg_id` 去重（BIZ-02，§6.1）。
- 消费端 `msg_id` 幂等：下游消费方按 `msg_id` 去重（与业务事件 `event_id` 幂等同构，见 [Outbox设计方案 §8.3](../业务事件/Outbox设计方案.md)）。
- 报警 Raised/Cleared 幂等（INV-10）。

### 9.3 顺序与乱序

- 分区内有序（`partition_key=equipment_id`）。
- 乱序矫正窗口（§6.2），迟到报文打 LATE 质量标补入（INV-04）。

### 9.4 数据质量

- 每条报文必须打质量标 GOOD/SUSPECT/BAD/LATE（INV-04）。
- 时钟漂移超阈值按 `ingest_ts` 修正并记 `clock_skew`（INV-11）。
- `source_ts ≤ ingest_ts` 始终成立（INV-04）。

### 9.5 终态保护

- 资产退役（`CLOSED` 终态）后任何采集事件拒收（INV-03）。
- 同设备单活跃通道（BIZ-01）。
- 跨上下文只消费不重发（INV-CX-09）。

---

## 10. 可观测性

### 10.1 指标

| 指标 | 含义 |
|---|---|
| `dc_ingest_rate_total` / `_per_equipment` | 采集入库速率（总/按设备） |
| `dc_backlog_pending` | 边缘缓冲待补传数量（网关侧） |
| `dc_backfill_total` / `_bytes` | 补传次数 / 补传字节数 |
| `dc_duplicate_discarded_total` | 平台去重命中次数（去重强度） |
| `dc_late_arrival_total` | 乱序矫正补入次数 |
| `dc_deadletter_total` | 死信入库数 |
| `dc_quality_flag_ratio{quality}` | GOOD/SUSPECT/BAD/LATE 占比（数据质量健康度） |
| `dc_persist_latency_seconds` | 落库延迟（`persisted_at - ingest_ts`） |
| `dc_storage_write_rate` | 温层写入速率 |
| `dc_object_storage_put_total` / `_failure_total` | MinIO 上传次数 / 失败次数 |
| `dc_consumer_lag{topic}` | 下游消费积压 |
| `dc_gateway_health` | 网关与通道健康度（对应 `dc.gateway.health` 主题） |

### 10.2 告警

| 告警条件 | 严重性 |
|---|---|
| 某设备 `EquipmentOffline` 持续未恢复 | 高（断采） |
| 边缘缓冲水位持续增长（补传跟不上） | 高 |
| `dc_deadletter_total` 有新增 | 中/高（解码异常） |
| `dc_quality_flag_ratio{quality="BAD"}` 占比上升 | 中（设备/协议异常） |
| `dc_duplicate_discarded_total` 异常高 | 中（疑似大量重传，查网关/网络） |
| 温层写入延迟 p99 超阈值 | 中（存储压力） |
| MinIO 上传失败率升高 | 高（大载荷丢失风险） |
| 下游 `consumer_lag` 持续增长 | 中（消费方追不上） |

---

## 11. 实施检查清单

**边缘网关**
- [ ] 协议适配按 `DecodingStrategy` 策略模式实现，解码为纯方法（§4.2）。
- [ ] 本地缓冲载体选型落地，`BackfillCursor` 单调递增、先补后新（§4.3，INV-05）。
- [ ] 大载荷经预签名 PUT 直传 MinIO，上传成功 + sha256 校验后才 `seal`（§5.4，INV-15）。
- [ ] 网关 HA 部署形态定稿，不破坏 BIZ-01 单活跃通道（§4.5）。
- [ ] 自研/选型决策落地，选型产品有 ACL 适配层产出 `dc.*` 事件（§4.6）。

**Kafka 管道**
- [ ] `dc.*` topic 显式创建，分区数与保留期按实测定（§5.2）。
- [ ] 采集 Producer 独立 client-id，高吞吐配置 + 幂等（§5.3）。
- [ ] `partition_key=equipment_id`，不用随机 UUID（§5.5）。
- [ ] 采集 client-id 集群配额已设（§8.4）。

**平台接收**
- [ ] `msg_id` 去重表落地，保留期 ≥ 最长补传窗口（§6.1，BIZ-02）。
- [ ] 乱序矫正窗口按需启用，迟到打 LATE 标（§6.2）。
- [ ] 落库与分发非原子，靠重投 + 消费端幂等兜底（§6.3）。

**存储分层**
- [ ] 热层 Redis 实时快照就位，过点查询 ≤200ms（§7.1，INV-CX-04）。
- [ ] 温层当前 MySQL 结构化表，TSDB 引入触发阈值已定（§7.2/§7.4）。
- [ ] 冷层归档策略落地，保留期按审计要求（§7.3）。

**限流与可靠性**
- [ ] 网关在途上限 + 平台去重吞吐上限 + 温层写入限流就位（§8）。
- [ ] 不丢/不重/乱序/终态四件套验证（§9）。
- [ ] 可观测指标 + 告警就位（§10）。

---

## 12. 关键原则总结

1. **高频采集走"边缘缓冲 + Kafka 直连"，不走 Outbox**--架构级排除，是 Outbox 不被写爆的根本前提。
2. **可靠性模型刻意降级**：从"同事务原子"降为"不丢不重"，靠边缘缓冲 + 断点续传 + `msg_id` 去重 + 消费端幂等三支柱叠加。降级成立的业务前提是"单条价值低、可重传、靠聚合兜底"。
3. **采集只搬运不解释**（INV-CX-01）：网关/平台不做 PASS/FAIL 判定、不推进过站、不停用资产。业务语义一律留下游上下文。
4. **大载荷走 MinIO，主流只传 URI + sha256**（INV-15）：窄而快的结构化流进 Kafka，大而慢的附件进对象存储，是吞吐与成本的核心折衷。
5. **边缘网关是采集链路的单点**：本地缓冲 + 断点续传 + HA 部署三重保护，HA 不得破坏单活跃通道（BIZ-01）。
6. **存储热/温/冷分层**：热层内存快照读过点、温层近期历史供 SPC、冷层长期归档；当前无独立时序库，TSDB 按实测写入量触发引入。
7. **限流是速率维度，顺序是分区维度，两者正交**：采集侧限流不破坏分区内顺序，与业务事件限流互补，集群级配额兜底。
8. **topic 命名空间隔离**：采集 `dc.*` 与业务 `mes.*/eam.*/mfg.*` 隔离，差异化保留与配额，不混在同一 topic。

---

## 附录 A：决策点 🔴（交还用户）

| 决策点 | 说明 | 默认建议 |
|---|---|---|
| 边缘网关本地缓冲载体 | RocksDB / SQLite / 磁盘文件 | 高吞吐选 RocksDB，简单部署选 SQLite |
| 网关 HA 部署形态 | 单网关+缓冲 / 主备 / 集群 | 关键线体主备；切换 RTO 按可用性预算定 |
| 网关自研 vs 选型 | 自研 / EdgeX / Neuron / Kepware 等 | 协议栈自研量大时选型 + ACL 适配层 |
| 协议栈客户端库选型 | SECS/GEM / OPC-UA / Modbus / MQTT 库 | 按设备实际协议栈与许可成本定 |
| `dc.*` topic 分区数与保留期 | 取决于实测速率与消费并行度 | 12/24 分区、3~7 天保留，上线前压测定 |
| 采集 Producer 集群配额 | `producer_byte_rate` per client-id | 按采集实测字节率设，与业务配额隔离 |
| 平台去重存储方案 | Redis 布隆+DB 唯一 / 仅 DB 唯一 / Redis SET+异步 DB | 主推 Redis 布隆 + DB 唯一兜底 |
| 去重表保留期 | ≥ 最长补传窗口 | ≥ 7 天，与死信保留窗口对齐 |
| 乱序矫正窗口启用范围 | 哪些 topic 启用 | 仅需严格时序的聚合类 topic 启用 |
| 热层载体 | Redis / 内存 | Redis（跨实例共享 + 重建友好） |
| 温层是否引入 TSDB 及选型 | MySQL 现状 vs TDengine/InfluxDB/IoTDB/ClickHouse | 先 MySQL 跑通，实测超阈值再迁；遥测流+老化房优先迁 |
| TSDB 引入触发阈值 | 单表日增行数 / 查询 p99 | 日增超千万行或 p99 超秒级为参考 |
| 冷层保留期 | 按审计/法规 | 电子制造 1~3 年可追溯 |
| 降采样策略 | 原始保留多久、降采样粒度、聚合函数 | 归数据存储策略统一规则 |
| 断网超缓冲容量的丢弃策略 | 打 BAD 标+告警 / 仅告警 | 建议 BAD 标 + 告警，保链路不阻塞 |

> 以上阈值在真实集群容量与车间实测量明确前，均为**设计目标 + 假设**，不作线上实绩承诺（口径见 §0）。
