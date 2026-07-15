# FactoryRAG agentic 路线集成测试补全 · 面试闪光点与话术（测真实路径而非降级路径 / 真实组件最大化 / LangGraph 状态按声明过滤 / 集成测试作缺陷发现工具）

> **定位**：本文是对 FactoryRAG（rag-service）E 路线（agentic）**P0 #5 测试缺口**的面试纵深展开，接续 [httpx 资源泄漏修复](FactoryRAG-httpx资源泄漏修复-面试闪光点与话术.md) 那轮遗留的 "#5 agentic 集成测试 | 零集成测试"。与 httpx 那轮"资源生命周期归属"不同，这次是**测试工程与缺陷发现**：E 路线是新增功能但零集成测试，唯一存在的 `test_route_graph.py` 只覆盖 langgraph 不可用时的 `_FallbackGraph` 降级路径--真实 `CompiledStateGraph` 路径完全裸奔。守口径纪律：做了什么如实讲（14 例端到端 + 真实组件最大化 + 3 个 bug 修复），不确定的决策点交还用户拍板（AgentState 修法三选一），残留项诚实交代（GatewayService.chat 层非 HTTP 端点 / 70s 超时未直测 / 证据链读侧未跑真实 DB）。
>
> **核心矛盾**：一套分层工整、`_FallbackGraph` 还有测试的 E 路线，真实路径却"裸奔"，而且既有测试给的安心是**虚假**的。补集成测试的第一个用例就失败了--断言 `route_trace.audit_id` 非空，实际是空串。根因：`AgentState` TypedDict 漏声明 `audit_id`/`trace_id`，而 LangGraph `StateGraph` **按声明字段过滤状态**，未声明的键在节点间被丢弃（`traceparent`/`session_id` 声明了所以活下来），`ToolExecutor` 读到 `audit_id=""`，`route_trace` 行挂不上 `answer_audit`，`/agent/explain/{audit_id}` 证据链断裂。而这个 bug **只在真实 langgraph 路径显现**--`_FallbackGraph` 走 plain dict 不过滤，既有测试全绿但生产裸奔。三个判断贯穿全文：**测真实路径而非降级路径**、**真实组件最大化才挖得到集成层 bug**、**集成测试是缺陷发现工具不是覆盖率数字**。

---

## 0. 口径纪律（先读这一条）

| 类别 | 能不能讲 | 怎么说 |
|------|---------|--------|
| 已做的（14 例集成测试 / AgentState 补声明 / _build_answer 失败分支 + route_taken 统一 / _materialize 收紧契约） | ✅ 直接讲 | 有 diff 有测试可查 |
| 测试增长（82 -> 96，+14 集成测试） | ✅ 直接给 | pytest 实跑 |
| bug 发现方式（audit_id 由失败用例暴露 / 另 2 个写测试时读码发现） | ✅ 直接讲，区分清楚 | 不把读码发现说成测试发现 |
| 决策点（AgentState 修法三选一） | ✅ 直接讲 | 交还用户拍板，有权衡 |
| "证据链断裂已上线验证" / "彻底杜绝转人工误判" | ❌ 禁止 | 读侧未跑真实 DB，未部署 |
| "route_trace 读侧空" | ⚠️ 限定讲 | 写侧（audit_id=""）由测试证；读侧 `/agent/explain` 空是代码推断 |
| 70s 超时路径 | ⚠️ 限定讲 | 没直测（太慢），用抛异常伪图测了两条 except 分支 |
| 残留（HTTP 端点未测 / 70s 未直测 / 读侧未跑 DB / 4 预存失败） | ⚠️ 主动交代 | 诚实是加分项 |

一句话：**测试缺口讲透、bug 根因讲清、发现方式不夸大、决策点摆明谁拍板、残留不藏，价值在"测对路径 + 集成测试挖出生产 bug"不在"加了几个测试"**。

---

## 1. 30 秒电梯陈述

"我在 FactoryRAG 补了 E 路线（agentic）的集成测试缺口--P0 #5。E 路线是新增功能但零集成测试，唯一的 `test_route_graph.py` 只覆盖 langgraph 不可用时的 `_FallbackGraph` 降级路径，真实 `CompiledStateGraph` 路径裸奔。我按'真实组件最大化、伪造件只替代叶端基础设施'补了 14 例 `GatewayService.chat` 端到端测试。第一个用例就失败了--断言 `route_trace.audit_id` 非空，实际空串。根因：`AgentState` TypedDict 漏声明 `audit_id`/`trace_id`，LangGraph `StateGraph` 按声明字段过滤状态，没声明的键节点间被丢（`traceparent`/`session_id` 声明了活下来），`ToolExecutor` 读到 `audit_id=""`，`route_trace` 挂不上 `answer_audit`，`/agent/explain/{audit_id}` 证据链断裂。这 bug 只在真实路径显现--`_FallbackGraph` 走 plain dict 不过滤，既有测试全绿但生产裸奔，给的是虚假安心。修法我列三选一交还用户，用户选补声明入 `AgentState`。写测试时读码又发现两个 `_build_answer` 行为 bug：失败原因被丢弃（`state["answer"]` 是死代码，summary 退化通用文案）+ `route_taken` 不一致（节点内失败保留所尝试路线、图级失败才 HUMAN）。一并修了：失败分支用 `state["answer"]` 作 summary + 进 `detail["reason"]`，`route_taken` 以 `tool_result is None` 统一 HUMAN。测试 82 到 96，4 个失败 git stash 证为预存。残留：HTTP 端点未测、70s 超时用伪图测的 except 分支不是真超时、读侧未跑真实 DB。"

**三个抓手**：测真实路径而非降级路径 / LangGraph 状态按声明过滤 / 集成测试作缺陷发现工具。

---

## 2. 闪光点详解（按主线）

### A. 测真实路径而非降级路径（核心判断）

#### A.1 既有测试覆盖的是 B 计划，A 计划裸奔

**识别过程**：看 [test_route_graph.py](../FactoryRAG/tests/test_route_graph.py)，三个用例全是 `_FallbackGraph`--构造 `_FallbackGraph(builder, intent)` 直接调 `ainvoke`，验证 TRACE_FACT 走 tool、ROOT_CAUSE 走 delegate、UNKNOWN 仅 converge。乍看三意图分支都覆盖了。但 [route_graph_builder.py](../FactoryRAG/app/routes/agentic/infrastructure/ai/route_graph_builder.py) 的 `build()` 是双分支：langgraph 可用走 `StateGraph(...).compile()`（生产路径），不可用才降级 `_FallbackGraph`。既有测试**直接 new `_FallbackGraph`**，连 `build()` 都没调，真实 `CompiledStateGraph` 路径零覆盖。

后果：生产环境 langgraph 是装着的（`pyproject.toml` 里 `langgraph>=0.2`），每次问答走的是 `CompiledStateGraph`，而它从未被测过。待办 #5 自己的话--"E 路线是新增，等于裸奔"--说的就是这个。

**为什么是亮点**：这是"测试覆盖率的虚假安全感"的典型。多数人看 `test_route_graph.py` 有三个用例、覆盖了三意图分支，就以为 E 路线测了。但那三个用例测的是**降级执行器**，不是**生产执行器**。降级路径要走通（langgraph 没装时能跑），但它是 B 计划不是 A 计划。**识别出"既有测试测的是降级路径、生产路径裸奔"**，体现的是对"测什么路径"的理解，不是"加了几个测试"的本能反应。面试官问"你怎么发现缺口"，答"看 test_route_graph.py 三个用例都直接 new _FallbackGraph，没调 build()，而 build() 在 langgraph 可用时返回 CompiledStateGraph--既有测试连生产执行器都没碰"。

**防守话术**："`test_route_graph.py` 三个用例直接构造 `_FallbackGraph` 测，没调 `build()`。`build()` 在 langgraph 可用时返回 `CompiledStateGraph`、不可用才降级 `_FallbackGraph`。生产 langgraph 装着走 CompiledStateGraph，那条路径零覆盖。测的是 B 计划，A 计划裸奔。"

---

### B. 真实组件最大化，伪造件只替代叶端基础设施（测试哲学）

#### B.1 真 IntentRouter / RouteGraphBuilder / ToolExecutor / Delegator / L1L2Client，只伪造叶端

**处理**：14 例测试里，`GatewayService` 的所有协作者都用真的：真 `IntentRouter`（规则优先 + LLM 兜底）、真 `RouteGraphBuilder`（真 langgraph `CompiledStateGraph`）、真 `ToolExecutor`（权限校验 + trace 记录 + handler 调用）、真 `SubAgentDelegator`、真 `L1DelegationClient`/`L2DelegationClient`（真 httpx 调用契约）。只伪造**叶端基础设施**：A/B Port 的 handler（`trace_rag_port.expand` / `doc_rag_port.search` 用 `AsyncMock`）、L1/L2 的 httpx（`MagicMock` + `post=AsyncMock`）、Redis（`_FakeCache`）、MySQL session（`AsyncMock` 的 `audit_repo` / `trace_repo`）、obs（`MagicMock`）。与 [_mock_rag_infra.py](../FactoryRAG/tests/_mock_rag_infra.py) 的"真实组件最大化、伪造件只替代基础设施"口径一致。

**为什么是亮点**：两个判断。一是**真实组件最大化才挖得到集成层 bug**。如果我把 `ToolExecutor`/`Delegator` 也 mock 掉，`GatewayService` 就只剩个空壳路由，`audit_id` 从 state 读出来这条路径根本不跑--bug 就藏住了。真实组件最大化不是为了好看，是为了让集成测试**真能穿过集成层**（图状态在节点间流转、tool_result 物化、trace_repo 写入）。二是**伪造件只替代叶端**--A/B Port 的 handler、httpx、Redis、DB 这些外部依赖用伪造件满足契约，使 application/domain 层逻辑被完整覆盖且零外部依赖可跑。面试官问"为什么不直接 mock GatewayService 的协作者"，答"mock 协作者就只剩空壳路由，集成层的 bug--比如 state 在节点间丢键--根本走不到。真实组件最大化才能让集成测试挖到集成层的 bug"。

**防守话术**："真 IntentRouter / RouteGraphBuilder（真 langgraph）/ ToolExecutor / Delegator / L1L2Client 全用真的，只伪造叶端--Port handler、httpx、Redis、DB、obs。跟 `_mock_rag_infra.py` 口径一致。mock 协作者就剩空壳路由，state 丢键这种集成层 bug 走不到，bug 就藏住。真实组件最大化才能挖到集成层 bug。"

---

### C. LangGraph StateGraph 按声明字段过滤状态（P0 bug 根因）

#### C.1 audit_id/trace_id 没声明就被丢，traceparent/session_id 声明了活下来

**识别过程**：第一个测试 `test_chat_trace_fact_routes_to_a_tool` 断言 `trace_repo.save_ok` 收到的 `audit_id` 非空，失败--实际是 `''`。`ToolExecutor` 读 `state.get("audit_id", "")`，默认空串，说明 state 里没这个键。但 `GatewayService._run_graph` 明明把 `audit_id` 放进了 `initial` state。用一段探针脚本（伪 tool 节点打印收到的 state keys）证实：tool 节点收到的 keys 恰好是 `AgentState` TypedDict 声明的那 8 个，`audit_id`/`trace_id` 不在其中。根因：`AgentState` 漏声明 `audit_id`/`trace_id`，而 LangGraph `StateGraph(AgentState)` **按 TypedDict 声明字段过滤状态**，未声明的键在节点间被丢弃。`traceparent`/`session_id` 声明了所以活下来（这也解释了为什么 L1 委托的 traceparent header 一直正常、没人发现 audit_id 丢了）。

**后果链**：`ToolExecutor`/`Delegator` 读 `audit_id=""` -> `route_trace` 行 `audit_id` 为空 -> `AnswerAuditRepo.find_by_id(audit_id)` 用 `WHERE RouteTraceModel.audit_id == audit_id` 关联 -> **返回空 route_traces** -> `GET /agent/explain/{audit_id}`（正是 P0 #3 那个端点）只显示审计行、没有工具/委托证据。证据链断裂。

**为什么只真路径显现**：`_FallbackGraph.ainvoke` 把 state 当 plain dict 透传（`state = await self._builder._tool_executor(state)`），不做 schema 过滤，`audit_id` 在。所以既有 `test_route_graph.py` 全绿--它测的路径不丢键。生产走 `CompiledStateGraph` 才丢。**既有测试给的是虚假安心**。

**为什么是亮点**：这是"框架隐式契约"的典型坑。LangGraph `StateGraph` 的状态过滤是**隐式行为**--你传一个 dict 进去，它静默地只保留 TypedDict 声明的键，不报错不警告。`traceparent`/`session_id` 声明了活下来，给人"状态透传没问题"的错觉，`audit_id`/`trace_id` 漏声明就静默丢失。**识别出"`traceparent` 活下来不是因为我透传对了，是因为它碰巧声明了；`audit_id` 丢不是因为没传，是因为没声明"**，体现的是对框架状态模型的把握，不是"再加一个字段"的本能反应。而且这个 bug 直接断了 MES 的核心关注点--追溯性（`/agent/explain/{audit_id}` 是证据链入口）。面试官问"你怎么发现的"，答"第一个测试断言 audit_id 非空就失败了，写探针脚本看 tool 节点收到的 keys，发现恰好是 AgentState 声明的那几个，audit_id 没声明被 StateGraph 滤掉了"。

**防守话术**："`AgentState` 漏声明 `audit_id`/`trace_id`，LangGraph `StateGraph` 按 TypedDict 声明过滤状态，没声明的键节点间被丢。`traceparent`/`session_id` 声明了活下来，所以 L1 委托的 traceparent 一直正常、没人发现 audit_id 丢了。`ToolExecutor` 读到空串，route_trace 挂不上 answer_audit，`/agent/explain/{audit_id}` 证据链断。`_FallbackGraph` 走 plain dict 不过滤，既有测试全绿但生产裸奔--虚假安心。"

---

### D. 集成测试作缺陷发现工具（1 测试失败 + 2 读码发现，不混为一谈）

#### D.1 三个 bug 的发现方式不一样，诚实区分

**三个 bug 的发现路径**：
1. **audit_id 断链**：**失败的测试用例直接暴露**。`test_chat_trace_fact_routes_to_a_tool` 断言 `ok_kwargs["audit_id"]` 非空，pytest 红了 `assert ''`。这是最强的发现--测试替你抓到了。
2. **失败原因丢弃**：**写测试时读 `_build_answer` 发现**。我写失败路径测试时发现 `_build_answer` 一律用 `_materialize(tool_result)` 算 summary、从不读 `state["answer"]`，而 `ToolExecutor`/`Delegator` 在失败分支写的 `state["answer"]` 全是死代码。不是测试失败暴露的，是读码发现的。
3. **route_taken 不一致**：**写测试时读码发现**。断言 delegation 失败的 `route_taken` 时，发现它是 "L1"（保留所尝试路线），而图级失败是 "HUMAN"，同一语义两种表示。

**为什么是亮点**：**不把读码发现说成测试发现**。很多人会把"写测试时顺便发现的 bug"都归功于"集成测试发现的"，显得测试威力大。但 audit_id 是测试**失败**抓到的（测试替你工作了），后两个是**读码**发现的（你替测试工作了）。区分清楚反而更显功力：集成测试的价值有两个维度--一是**自动抓 bug**（audit_id），二是**强迫你读路径**（读码时发现死代码和不一致）。面试官问"集成测试发现了几个 bug"，答"测试直接抓了 1 个--audit_id 断链，用例红了；另外 2 个是我写测试时读 _build_answer 发现的--失败原因丢弃和 route_taken 不一致。前一个是测试替你抓，后两个是你替测试读。我不把读码发现说成测试发现"。

**防守话术**："三个 bug 发现方式不一样，我不混为一谈。audit_id 是测试用例失败直接抓的--断言 audit_id 非空，pytest 红了 assert ''。失败原因丢弃和 route_taken 不一致是我写测试时读 _build_answer 发现的，不是测试失败暴露。前一个是测试替你抓，后两个是你替测试读。"

---

### E. _build_answer 两 bug：失败原因丢弃 + route_taken 不一致

#### E.1 state["answer"] 是死代码，summary 退化通用文案

**问题**：`_build_answer` 一律 `summary, sources, confidence = self._materialize(tool_result, intent)`，从不读 `state["answer"]`。但 `ToolExecutor`/`Delegator` 在失败分支精心写了 `state["answer"]`--"权限不足，建议转人工。"/"工具 X 执行失败：{exc}"/"子代理委托超时/失败，已转人工：{exc}"。`tool_result` 在失败时是 `None`，`_materialize(None)` 返回通用文案"未能获取结果，建议转人工。"。**失败原因全成死代码**，工程师看到转人工但不知道为什么转，`detail` 也不含原因。

**处理**：`_build_answer` 对 `tool_result is None` 单独走失败分支：`reason = state.get("answer") or "未能获取结果，建议转人工。"`，`summary=reason`、`detail={"reason": reason}`（与 `_human_fallback` 的 `detail={"reason": ...}` 对齐）、`confidence=0.0`、`needs_human_review=True`。成功分支才调 `_materialize`。`_materialize` 的 `None` 分支随之移除--契约收紧为"仅处理非空 tool_result"，失败统一由 `_build_answer` 兜底，消除误导性死代码。

#### E.2 route_taken：节点内失败保留所尝试路线，图级失败才 HUMAN

**问题**：节点内失败（委托超时/工具异常/权限不足）`tool_result` 是 `None`，但 `_route_taken(intent, tool_chain)` 仍按意图返回 "L1"/"A"（保留所尝试路线）。而图级异常/超时走 `_human_fallback` 设 `route_taken="HUMAN"`。**同一"转人工"语义两种表示**。

**处理**：`_route_taken(intent, tool_result)` 改为以 `tool_result is None` 为失败信号统一返回 "HUMAN"（UNKNOWN 仍 HUMAN）。所尝试的工具/委托记在 `tool_chain`（如 `["L1:diagnose"]`/`["delegation:failed"]`/`["query_traceability_graph"]`），不靠 `route_taken` 表达。三条转人工路径现在一致：`route_taken="HUMAN"` + `detail["reason"]`。

**为什么是亮点**：两个判断。一是**死代码识别**--`state["answer"]` 被写但不被读，是典型的"作者意图没落到消费侧"。`_build_answer` 重算 summary 把它架空了。识别出"写入侧精心构造、消费侧从不读取"的死代码，比删几行更有价值。二是**语义一致性**--`route_taken` 在失败时该表达"结果"（HUMAN）还是"尝试"（L1/A）是设计选择，但不能同一个字段两种语义。统一成"结果"（HUMAN），"尝试"让 `tool_chain` 表达，职责切干净。面试官问"你怎么发现这两个 bug"，答"写失败路径测试时读 _build_answer，发现它从不读 state['answer']，而 ToolExecutor 写了一堆--死代码；再断言 route_taken 时发现节点内失败是 L1、图级失败是 HUMAN，同一语义两种表示"。

**防守话术**："`_build_answer` 一律用 `_materialize(tool_result)` 算 summary，从不读 `state['answer']`，ToolExecutor/Delegator 写的失败原因全成死代码，summary 退化为通用文案。修：`tool_result is None` 走失败分支用 `state['answer']` 作 summary + 进 `detail['reason']`。route_taken 原来节点内失败保留 L1/A、图级失败才 HUMAN，同一语义两种表示；改成以 `tool_result is None` 统一 HUMAN，所尝试路线记 tool_chain。`_materialize` 的 None 分支移除，契约收紧。"

---

### F. 决策点交还用户：AgentState 修法三选一

#### F.1 加到 AgentState / 走 config.configurable / 仅记录不修

audit_id 断链的修法我没自行假设，列了三个选项和权衡交还用户：

1. **加到 `AgentState` TypedDict**（用户选定）：补声明 `audit_id: str` / `trace_id: str`，与已声明的 `traceparent`/`session_id` 一致。最小改动（2 行）、根因直击、route_trace 重新挂回 audit_id。代价：状态多两个键。
2. **走 `config["configurable"]`**：`audit_id` 放进 `ainvoke` 的 `config["configurable"]`（已有 `thread_id`），节点从 config 读。把非业务字段挪出图状态。代价：`ToolExecutor`/`Delegator` 签名要接 config，改动面大。
3. **仅记录不修**：把 audit_id 断言标 `xfail` 注明 bug，等单独排期。测试先行覆盖，但证据链仍断裂。

用户选了 1。

**为什么是亮点**：两个判断。一是**根因直击 vs 规避**--选项 1 在状态 schema 层补声明，是直击"LangGraph 按声明过滤"的根因；选项 2 把 audit_id 挪出状态是绕开过滤、治标。二是**不自行假设、不确定交还用户**--audit_id 走状态还是 config 是"图状态该承载什么"的真实取舍（业务关联 ID 算不算图状态），自行拍板可能选错用户意图。把选项、权衡、推荐摆清楚让用户定，是资深信号。面试官问"你怎么定的修法"，答"我没自己拍，列了三个选项--补声明入 AgentState / 走 config.configurable / 仅记录不修。我推荐补声明，跟 traceparent/session_id 一致、根因直击。用户选了这个"。

**防守话术**："修法我列三选一交还用户：补声明入 AgentState / 走 config.configurable / 仅记录不修。我推荐补声明--跟已声明的 traceparent/session_id 一致、最小改动、根因直击；config.configurable 是把 audit_id 挪出状态绕开过滤、治标。用户选了补声明。audit_id 走状态还是 config 是'图状态该承载什么'的取舍，我没自己拍。"

---

### G. 口径纪律：测试边界 + 4 预存失败证预存

#### G.1 测的是 GatewayService.chat 层，不是 HTTP 端点

**边界**：14 例在 `GatewayService.chat` 层做端到端，没到 `POST /agent/chat` 的 HTTP 端点（`chat_router` 的请求解析/响应序列化/异常映射没覆盖）。`_run_graph` 的 70 秒超时没直测（太慢），用一个 `ainvoke` 抛异常的伪图（`_RaisingGraphBuilder`）测了两条 except 分支（`asyncio.TimeoutError` -> "路由图超时" / 通用 `Exception` -> str(exc)）。证据链的**读侧**（`AnswerAuditRepo.find_by_id` 真实 DB 关联返回空）没跑真实 DB--写侧 `audit_id=""` 由测试证实，读侧空是代码推断。

#### G.2 4 个预存失败 git stash 证预存

**验证**：全量 96 测试 92 过 4 失败。4 个失败（`test_recall_relevance[bm25/hybrid]`、`test_seed_resolver_regex`、`test_a_enriches_suggested_action_from_b`）都是 B/A 路线检索质量，跟 E 路线集成测试八竿子打不着。用 git stash 暂存改动、干净树上重跑同样 4 失败，证为预存。

**为什么是亮点**：两处诚实。一是**测试边界不夸大**--没说"端到端覆盖了 /agent/chat"，老实讲是 application service 层、HTTP 端点没测、70s 是伪图测的 except 分支不是真超时、读侧是推断。二是**测试失败证预存**--跟 httpx 那轮同构（见 [httpx 修复 §E](FactoryRAG-httpx资源泄漏修复-面试闪光点与话术.md)），不靠"看着无关"甩锅，git stash 干净树复现把"无关"从断言变证据。面试官问"测试有失败你怎么确定不是你引入的"，答"git stash 暂存改动、干净树复现同样 4 失败，证为预存。4 个都是 B/A 检索质量，我改 E 路线，但隔离是推断、stash 是实证"。

**防守话术**："测的是 GatewayService.chat 层不是 HTTP 端点；70s 超时没直测，用抛异常伪图测了两条 except 分支；证据链读侧没跑真实 DB，写侧 audit_id='' 由测试证、读侧空是推断。4 个失败 git stash 干净树复现证为预存，跟 httpx 那轮同构。"

---

### H. 与 MES 追溯同构：证据链 / 可回溯（领域呼应）

**呼应**：`/agent/explain/{audit_id}` 是 E 路线的证据链入口--工程师拿 audit_id 回查"这次自动问答走了哪条路线、调了什么工具、命中什么 trace/SOP"。`route_trace` 行通过 `audit_id` 挂到 `answer_audit`，正是 MES"可回溯"关注点在 agentic 层的投影。audit_id 断链 = 证据链断 = 自动问答变成"黑箱结论不可追溯"，违背 MES"宁可拦下让人判、不可错放"的口径（`GatewayService` 模块注释原话）。

**为什么是亮点**：**测试缺口不是抽象的覆盖率问题，是会断领域核心契约的生产 bug**。补集成测试挖出的不是"某行没覆盖"，是"`/agent/explain` 证据链断裂"这种直接违背追溯性的 P0。能讲"我补 E 路线集成测试，第一个用例就抓到证据链断裂--route_trace 挂不上 answer_audit，这正好是 MES 追溯性在 agentic 层的投影"，把测试工程和领域关注点焊在一起。面试官问"这个 bug 影响什么"，答"route_trace 行 audit_id 为空挂不上 answer_audit，/agent/explain/{audit_id} 取不到工具/委托证据，自动问答成黑箱结论不可追溯--违背 MES 可回溯的关注点"。

**防守话术**："`/agent/explain/{audit_id}` 是证据链入口，route_trace 靠 audit_id 挂 answer_audit。audit_id 断链就是证据链断，自动问答变黑箱结论不可追溯，违背 MES 可回溯。补集成测试挖出的不是覆盖率空洞，是断领域核心契约的 P0。"

---

## 3. 核心应答话术（高频问题，口语化背熟）

### 话术 1：你这次最核心的发现是什么

"最核心是**集成测试作缺陷发现工具**。E 路线零集成测试，唯一 的 `test_route_graph.py` 只测 `_FallbackGraph` 降级路径，真实 `CompiledStateGraph` 裸奔。我按真实组件最大化补了 14 例，第一个用例就失败--断言 `route_trace.audit_id` 非空，实际空串。根因：`AgentState` 漏声明 `audit_id`/`trace_id`，LangGraph 按声明过滤状态把它们丢了，`ToolExecutor` 读到空串，route_trace 挂不上 answer_audit，`/agent/explain/{audit_id}` 证据链断。**这 bug 只在真实路径显现，_FallbackGraph 走 plain dict 不过滤，既有测试全绿但生产裸奔--虚假安心**。"

### 话术 2：为什么说既有测试是虚假安心

"既有 `test_route_graph.py` 三个用例直接 new `_FallbackGraph` 测，没调 `build()`。`build()` 在 langgraph 可用时返回 `CompiledStateGraph`、不可用才降级 `_FallbackGraph`。生产 langgraph 装着走 CompiledStateGraph，那条路径零覆盖。而两条路径的状态处理不一样：CompiledStateGraph 走 StateGraph 的 schema 过滤，`_FallbackGraph` 走 plain dict。audit_id 丢失就是因为这个差异--`_FallbackGraph` 的 state 是原样 dict，audit_id 在；CompiledStateGraph 把没声明的键滤掉了。所以既有测试全绿但 bug 在生产裸奔。"

### 话术 3：为什么用真实组件而不是 mock 协作者

"mock 协作者就剩空壳路由，集成层的 bug 走不到。audit_id 从 state 读出来这条路径，如果 ToolExecutor 被 mock 掉根本不跑，bug 就藏住。真实组件最大化--真 IntentRouter / RouteGraphBuilder（真 langgraph）/ ToolExecutor / Delegator / L1L2Client，只伪造叶端 Port handler / httpx / Redis / DB / obs--让集成测试真能穿过集成层。跟 `_mock_rag_infra.py` 口径一致。"

### 话术 4：LangGraph 按声明过滤状态这个坑你怎么定位的

"第一个测试断言 `audit_id` 非空就红了 `assert ''`。`ToolExecutor` 读 `state.get('audit_id','')` 得空串，说明 state 里没这键，但 `_run_graph` 明明放进了 initial。我写了个探针脚本--伪 tool 节点打印收到的 state keys--发现恰好是 `AgentState` TypedDict 声明的那 8 个，`audit_id`/`trace_id` 不在。LangGraph `StateGraph` 按 TypedDict 声明过滤状态，没声明的键静默丢弃。`traceparent`/`session_id` 声明了活下来，所以 L1 委托的 traceparent 一直正常、没人发现 audit_id 丢了。"

### 话术 5：三个 bug 都是怎么发现的

"我不混为一谈。audit_id 断链是**测试用例失败直接抓的**--断言 audit_id 非空，pytest 红了 `assert ''`。失败原因丢弃和 route_taken 不一致是**写测试时读 `_build_answer` 发现的**，不是测试失败暴露。前一个是测试替你抓，后两个是你替测试读。把读码发现说成测试发现是不诚实。"

### 话术 6：AgentState 修法你怎么定的

"我没自己拍，列三选一交还用户：补声明入 `AgentState` / 走 `config.configurable` / 仅记录不修。我推荐补声明--跟已声明的 `traceparent`/`session_id` 一致、最小改动、根因直击。`config.configurable` 是把 audit_id 挪出状态绕开过滤、治标。用户选了补声明。audit_id 走状态还是 config 是'图状态该承载什么'的取舍，我没自己拍。"

### 话术 7：测试边界到哪，有没有夸大

"没夸大。测的是 `GatewayService.chat` 层端到端，没到 HTTP `/agent/chat` 端点；70s 超时没直测（太慢），用抛异常伪图测了两条 except 分支；证据链读侧没跑真实 DB，写侧 audit_id='' 由测试证、读侧空是代码推断。4 个全量失败 git stash 干净树证为预存。"

---

## 4. 深度问答（技术深挖）

**Q：LangGraph StateGraph 为什么按 TypedDict 声明过滤状态？这是 bug 还是设计？**
A：是设计。`StateGraph(AgentState)` 的 `AgentState` 是状态 schema，LangGraph 用它确定哪些键在节点间流转、以及每个键的 reducer（默认 replace，也可 `Annotated[list, add]` 做累加）。未声明的键不在 schema 里，节点返回时被滤掉。设计意图是让状态显式化、可控。坑在于它是**静默过滤**--不报错不警告，传了不声明的键就悄悄没。`traceparent`/`session_id` 声明了活下来给人"透传没问题"的错觉。修法是补声明，不是绕开。

**Q：为什么不把 audit_id 放 config.configurable，反而放状态？**
A：用户选了放状态。权衡：放状态（补声明）与 `traceparent`/`session_id` 一致、节点直接 `state.get("audit_id")` 读、根因直击；放 config 把 audit_id 挪出图状态，`ToolExecutor`/`Delegator` 要从 config 读、签名要接 config、改动面大。audit_id 是这次问答的业务关联 ID（串联 answer_audit + route_trace），跟 traceparent 同性质--traceparent 在状态里，audit_id 也在状态里，一致。如果以后状态键膨胀，再考虑把非业务元数据挪 config，目前 2 个键不值得。

**Q：_FallbackGraph 走 plain dict 不过滤，那它是不是也有隐患？**
A：方向相反。`_FallbackGraph` 不过滤意味着 state 是原样 dict，键都在--audit_id 不丢。但它不过滤也有反面：如果某节点返回了不该出现的键，`_FallbackGraph` 会保留，而 `CompiledStateGraph` 会滤掉。两条路径的状态语义有微妙差异，这正是"只测降级路径"的危险--降级和生产路径行为不完全一致，测了降级不代表生产没问题。修完 `AgentState` 后，两条路径的 audit_id 都能透传，差异收敛。

**Q：失败原因丢弃这个 bug，为什么之前没人发现？**
A：因为 E 路线零集成测试，失败路径从来没被端到端跑过。`state["answer"]` 被 `ToolExecutor`/`Delegator` 写入，但 `_build_answer` 用 `_materialize(tool_result)` 重算 summary 把它架空--写入侧和消费侧脱节。单测若只测 `ToolExecutor` 会看到 `state["answer"]="权限不足"` 觉得没问题，只测 `_build_answer` 会看到 `_materialize` 逻辑觉得没问题，**集成才暴露写入的消费侧从不读它**。这正是集成测试的价值--跨组件的脱节只有穿过整条链才看得到。

**Q：route_taken 统一成 HUMAN，会不会丢失"尝试了哪条路线"的信息？**
A：不会，所尝试的路线记在 `tool_chain`。`tool_chain` 是 `["L1:diagnose"]`/`["delegation:failed"]`/`["query_traceability_graph"]`，能看出尝试了什么、是否失败。`route_taken` 表达**结果**（成功走 A/B/L1/L2、失败转 HUMAN），`tool_chain` 表达**过程**（尝试了哪些工具/委托）。职责切开后，"结果"和"过程"各归各位，比一个字段两种语义清晰。

**Q：真实组件最大化，测试会不会很慢/很脆？**
A：14 例 1.15 秒跑完，不慢。因为伪造的是叶端基础设施（Port handler / httpx / Redis / DB 都是内存 mock），真实的只是 application/domain 逻辑和 langgraph 图编排--这些是纯计算没 IO。脆性方面，真实 langgraph 版本升级可能影响图行为，但这正是集成测试该捕获的--如果 langgraph 升级改了状态过滤语义，我的测试会红，这是好事不是坏事。

**Q：14 例覆盖了哪些分支？**
A：三意图正常路径（TRACE_FACT->A 工具 / DOC_LOOKUP->B 工具 / ROOT_CAUSE->L1 委托 / DRAFT_REQUEST->L2 委托）、L1 委托超时转人工、图异常 + 图超时（`_run_graph` 两条 except）、缓存命中短路、权限不足转人工、工具失败转人工、UNKNOWN 转人工、`IntentRouter` 规则优先 + LLM 兜底（成功/失败）。失败路径测试还断言了失败原因保留 + route_taken=HUMAN + 证据链 audit_id 挂得上。

---

## 5. 压力追问（陷阱题，考诚实）

**Q：你说集成测试挖出 P0 bug，但 audit_id 断链是你写测试时断言出来的，不算测试"自动"发现吧？**
A：分两种。audit_id 是测试**自动**抓的--我写的断言是 `assert ok_kwargs["audit_id"]`（非空），跑起来 pytest 红了 `assert ''`，是测试替我抓到的，我没预期能抓到 bug。后两个（失败原因丢弃、route_taken 不一致）是我**写测试时读码**发现的，不是测试失败暴露。我不会把读码发现说成测试自动发现。集成测试的价值两个维度：自动抓 bug（audit_id）+ 强迫读路径（读码发现死代码）。两个都讲，不混。

**Q：证据链断裂你验证了吗？还是推断的？**
A：诚实讲：**写侧验证了，读侧推断的**。写侧--测试断言 `trace_repo.save_ok` 收到的 `audit_id` 是 `''`，证实 ToolExecutor 传了空串给 route_trace。读侧--`AnswerAuditRepo.find_by_id` 用 `WHERE RouteTraceModel.audit_id == audit_id` 关联，audit_id="" 匹配不上，返回空 route_traces，这是**代码推断**，没跑真实 DB join。我没说"已验证 /agent/explain 返回空"，我说的是"写侧由测试证实、读侧是代码推断"。要彻底验证读侧得起真实 MySQL 跑 find_by_id，本次没做。

**Q：70s 超时你说测了，到底测没测？**
A：**没直测真超时**。`_run_graph` 用 `asyncio.wait_for(timeout=70.0)`，直测要等 70 秒，太慢。我用一个伪图 `_RaisingGraphBuilder`--它的 `ainvoke` 直接抛 `asyncio.TimeoutError`--测了 `except asyncio.TimeoutError` 分支（-> "路由图超时"转人工）和抛 `RuntimeError` 测了 `except Exception` 分支。这测的是**异常处理分支**，不是"真等 70 秒触发 wait_for 自身超时"那条完整路径。两条 except 的处理逻辑被覆盖了，wait_for 本身的计时行为没测（那是标准库的行为，不该由我测）。

**Q：你只改了 2 个源码 +41/-7 + 1 个测试文件，这叫 P0？**
A：P0 按生产风险分级不按行数。audit_id 断链直接断 `/agent/explain/{audit_id}` 证据链，自动问答变黑箱结论不可追溯，违背 MES 可回溯的关注点。而且它只在真实 langgraph 路径显现、既有测试全绿--生产裸奔的 bug 最危险。改动小恰恰说明根因定位准：`AgentState` 补 2 个字段直击"按声明过滤"根因。行数不是风险等级也不是价值指标。

**Q：为什么不顺手把 HTTP 端点也测了，凑更完整的端到端？**
A：变更原子性。这次的任务是 P0 #5 补集成测试 + 修挖出的 bug，`GatewayService.chat` 层端到端已覆盖三意图分支和转人工路径。HTTP 端点（`chat_router` 的请求解析/响应序列化/异常映射）是另一层，夹带进来会让"补集成测试"diff 混入 router 测试，难审。HTTP 端点测试另开。跟 httpx 那轮不顺手改 #2/#5 是同一种纪律（见 [httpx 修复 §F](FactoryRAG-httpx资源泄漏修复-面试闪光点与话术.md)）。

**Q：_FallbackGraph 既然不过滤状态没这 bug，那它是不是更"安全"？是不是该生产也用它？**
A：绝对不行。`_FallbackGraph` 是降级执行器，单趟定长（router -> 单分支 -> converge），没有 langgraph 的条件边、检查点、`Command(resume=…)` 语义。用它替代 CompiledStateGraph 等于砍掉 E 路线的图编排能力。它"不过滤状态"只是 plain dict 透传的副作用，不是设计上的安全优势。正确的修法是让真实路径的状态 schema 正确（补声明），不是退回降级路径。

---

## 6. 指标卡片（背下来）

### 卡片 A：改动规模（最硬，直接给）

| 维度 | 数值 | 出处 |
|------|------|------|
| 新增测试文件 | 1（`test_agentic_gateway.py`，491 行 / 14 例） | [tests/test_agentic_gateway.py](../FactoryRAG/tests/test_agentic_gateway.py) |
| 源码改动 | 2 文件 +41/-7 | git diff --stat |
| 修复位置 | `gateway_service.py`（_build_answer/_route_taken/_materialize）+ `route_graph_builder.py`（AgentState） | [gateway_service.py](../FactoryRAG/app/routes/agentic/application/gateway_service.py) / [route_graph_builder.py](../FactoryRAG/app/routes/agentic/infrastructure/ai/route_graph_builder.py) |
| 测试 | 82 -> 96（+14）/ 92 过 / 4 失败（预存） | pytest 实跑 |
| 预存失败验证 | git stash 干净树复现 4 失败 | 本次 |
| 交还用户决策点 | 1（AgentState 修法三选一） | 本次对话 |
| bug 发现方式 | 1 测试失败暴露（audit_id）+ 2 读码发现（失败原因/route_taken） | 本次 |

### 卡片 B：缺陷与修复（识别 + 处理）

| 缺陷 | 后果 | 修复 |
|------|------|------|
| E 路线零集成测试，仅测 _FallbackGraph | 真实 CompiledStateGraph 路径裸奔 | 补 14 例真实路径端到端测试 |
| AgentState 漏声明 audit_id/trace_id | LangGraph 滤掉 -> route_trace.audit_id="" -> /agent/explain 证据链断 | 补声明入 AgentState |
| _build_answer 不读 state["answer"] | 失败原因成死代码，summary 退化通用文案 | tool_result is None 走失败分支用 state["answer"] + detail["reason"] |
| route_taken 节点内失败保留尝试路线 | 同一转人工语义两种表示（L1/A vs HUMAN） | _route_taken 以 tool_result is None 统一 HUMAN |
| _materialize 含 None 死分支 | 误导性死代码（None 实由 _build_answer 处理） | 移除 None 分支，契约收紧 |

### 卡片 C：能力表述（讲做了什么）

| 能力 | 表述 |
|------|------|
| 测试工程 | 识别"测的是降级路径、生产路径裸奔"，补真实路径集成测试 |
| 测试哲学 | 真实组件最大化、伪造件只替代叶端基础设施，让集成测试穿过集成层 |
| 框架掌握 | 定位 LangGraph StateGraph 按声明过滤状态的隐式契约 |
| 缺陷发现 | 集成测试作缺陷发现工具（1 测试抓 + 2 读码发现），发现方式不混为一谈 |
| 领域呼应 | 证据链断裂 = 违背 MES 可回溯，测试缺口直击领域核心契约 |
| 工程纪律 | 决策交还用户、测试边界不夸大、4 失败 stash 证预存、残留诚实交代 |

---

## 7. 红线与遗留（面试别翻车）

**红线**：
- ❌ "证据链断裂已上线验证" / "彻底杜绝转人工误判" -- 读侧未跑真实 DB，未部署。
- ❌ "3 个 bug 都是集成测试自动发现的" -- audit_id 是测试失败抓的，另 2 个是读码发现，不能混。
- ❌ "70s 超时已验证" -- 没直测真超时，用伪图测的 except 分支。
- ❌ "HTTP 端点端到端覆盖" -- 测的是 GatewayService.chat 层，没到 /agent/chat 端点。
- ❌ "4 个失败是我修好的" -- 是预存，与本次无关。
- ❌ "AgentState 修法我自己定的" -- 是交还用户拍板的。
- ✅ 正确讲法："我补了 FactoryRAG E 路线的集成测试缺口（P0 #5）：识别既有测试只覆盖 _FallbackGraph 降级路径、真实 CompiledStateGraph 裸奔，按真实组件最大化补 14 例端到端测试。第一个用例就失败抓到 P0 bug--AgentState 漏声明 audit_id/trace_id 被 LangGraph 按声明过滤丢弃，route_trace 挂不上 answer_audit、/agent/explain 证据链断（只在真实路径显现，_FallbackGraph 不过滤所以既有测试全绿给虚假安心）。修法三选一交还用户选了补声明。写测试读码又发现 _build_answer 失败原因丢弃 + route_taken 不一致，一并修。测试 82->96，4 失败 git stash 证预存。残留：HTTP 端点未测、70s 用伪图测 except 分支、读侧未跑真实 DB。"

**遗留项清单（主动交代，反加分）**：

| 遗留 | 现状 | 倾向 |
|------|------|------|
| HTTP `/agent/chat` 端点测试 | 未测（GatewayService.chat 层止） | 另开 router 层测试 |
| 70s 超时完整路径 | 未直测（用伪图测 except 分支） | 标准库行为，可不强测 |
| 证据链读侧（find_by_id 真实 DB） | 未跑（写侧已证、读侧推断） | 另开 DB 集成测试 |
| 4 个预存测试失败 | B/A 检索质量 | 独立待办线 |
| P0 #3 router 穿透 application 层 | 待办 #3 未做 | 独立 #3 |

被问"还有什么没做"时，老实列出遗留 + 倾向，体现**知道自己不知道什么**，比假装完成得分高。

---

## 8. 一句话定位（收尾用）

"这次的价值不在加了 14 个测试，而在**识破既有测试的虚假安心--它只测了 _FallbackGraph 降级路径、真实 CompiledStateGraph 裸奔，而 P0 bug 恰好只在真实路径显现；用真实组件最大化让集成测试穿过集成层，第一个用例就抓到 AgentState 漏声明 audit_id/trace_id 被 LangGraph 按声明过滤丢弃、route_trace 挂不上 answer_audit 证据链断；写测试读码又修了失败原因丢弃和 route_taken 不一致**--做了的讲透、发现方式不混为一谈（1 测试抓 + 2 读码发现）、修法三选一交还用户、测试边界不夸大、4 失败 stash 证预存、残留不藏，与 MES 追溯的'可回溯、不黑箱'同构：自动问答的每一步都有证据可查、失败有原因可追。"
