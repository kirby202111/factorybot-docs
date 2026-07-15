# FactoryRAG httpx 资源泄漏修复 · 面试闪光点与话术（资源生命周期归属 / 构造者即销毁者 / 灰度懒装配的 dispose 条件性）

> **定位**：本文是对 FactoryRAG（rag-service）一条 **P0 资源泄漏缺陷**的面试纵深展开。与 factorybot 那轮"代码优化"里 `wiring.py` 的 httpx 泄漏是**同类不同实例**——这次是 E 路线（agentic）装配时创建的 L1/L2 委托 httpx 客户端未纳入 Container 生命周期。守口径纪律：做了什么如实讲（构造上移 + dispose 收口 + keyword-only 注入 + None 守卫），不确定的决策点交还用户拍板（client 由谁构造），残留项诚实交代（生命周期测试未补属待办 #5、启动失败路径未单独兜底、4 个预存测试失败非本次引入）。
>
> **核心矛盾**：一个分层工整、DDD 组合根清晰的服务，资源却"造了不收"。`build_gateway_service` 在装配时 new 了两个 `httpx.AsyncClient`，传给委托客户端后引用就丢了——既不存 container，`dispose` 也不关。根因不是"忘了写 aclose"，而是**资源生命周期归属错位**：构造资源的地方（路线装配函数）不拥有销毁权，拥有销毁权的地方（`Container.dispose`）不知道资源存在。两个判断贯穿全文：**构造者即销毁者**、**灰度懒装配决定 dispose 必须条件性**。

---

## 0. 口径纪律（先读这一条）

| 类别 | 能不能讲 | 怎么说 |
|------|---------|--------|
| 已做的修复（container 构造+注入 / dispose 收口 / keyword-only / 移除多余 import） | ✅ 直接讲 | 有 diff 可查 |
| 验证（AST+import / 82 测试 78 过 / 4 失败证为预存） | ✅ 直接给 | pytest 实跑 |
| 决策点（client 由谁构造） | ✅ 直接讲 | 交还用户拍板，有权衡 |
| "彻底杜绝资源泄漏" / "已上线验证" | ❌ 禁止 | 启动失败路径未兜底，未真实部署 |
| "dispose 已测试验证关闭" | ❌ 禁止 | 没补行为测试，靠审查 |
| 残留（生命周期测试未补 / 启动失败路径 / 4 预存失败） | ⚠️ 主动交代 | 诚实是加分项，被追问才说不加分反扣分 |
| "性能提升 X%" | ❌ 禁止 | 没测性能，不编 |

一句话：**归属讲透、修复讲清边界、决策点摆明谁拍板、残留不藏，价值在"生命周期归属判断"不在"改了几行"**。

---

## 1. 30 秒电梯陈述

"我在 FactoryRAG 修了一个 P0 资源泄漏：E 路线装配时创建的 L1/L2 委托 httpx 客户端，传给委托客户端后引用丢失，`Container.dispose` 只关 MES 的 client，L1/L2 连接池 shutdown 时泄漏。根因不是忘了 aclose，是资源生命周期归属错位——造资源的地方（路线装配函数）不拥有销毁权。一个决策点我交还用户拍板：client 由谁构造。用户选了 container 构造并注入——构造者即销毁者，与现有 MES 的 `self._http` 模式一致。具体：container 在 `_wire_agentic` 构造两个 client 存实例属性、注入 `build_gateway_service`（keyword-only 防误传），`dispose` 遍历关闭并 None 守卫（agentic 是灰度开关可能没装配）。验证：AST + import 通过，82 测试 78 过，4 个失败我用 git stash 在干净树上证为预存、与本次无关。残留我诚实交代：生命周期测试没补（属待办 #5 agentic 集成测试）、启动失败路径没单独兜底（与现有 `self._http` 处理一致）。"

**三个抓手**：资源生命周期归属 / 构造者即销毁者 / 灰度懒装配的 dispose 条件性。

---

## 2. 闪光点详解（按主线）

### A. 资源生命周期归属错位（核心判断）

#### A.1 造资源的不管销毁，管销毁的不知道资源

**识别过程**：看 [build_gateway_service](../FactoryRAG/app/routes/agentic/__init__.py) 装配 E 路线：`l1_http = httpx.AsyncClient(...)` / `l2_http = ...` 两个客户端，紧接着 `L1DelegationClient(http=l1_http, ...)` 包了一层。乍看有传参、有包装，很完备。但两个细节：

- **原始 client 引用装配完即丢**：`l1_http`/`l2_http` 是函数局部变量，传进委托客户端后，build 函数返回 gateway，局部变量引用消失。只有委托客户端内部 `self._http` 持有，而 Container 不持有委托客户端的 httpx 句柄。
- **dispose 不知道它们存在**：[Container.dispose()](../FactoryRAG/app/shared/web/container.py) 只 `await self._http.aclose()`（MES 客户端）+ engines / embedding / reranker。L1/L2 的 `httpx.AsyncClient` 不在关闭清单里。

后果：lifespan shutdown 调 dispose，MES client 关了，L1/L2 的连接池没人关——连接句柄泄漏，长时间运行的 Pod 反复滚动更新累积。

**为什么是亮点**：这是"分层工整但生命周期悬空"的典型。多数人看 `build_gateway_service` 有完整的 DDD 分层（domain/application/infrastructure）、有组合根、有 dispose，就以为资源管理完备。但 dispose 的关闭清单是**静态枚举**的（写死 `self._http`/engines/...），新建的资源如果不主动登记进 dispose，就永远悬空。**识别出"dispose 是枚举式而非注册式，新增资源不会自动纳入"**，体现的是对资源生命周期归属的理解，不是"再加一行 aclose"的本能反应。面试官问"你怎么发现的"，答"我看 build_gateway_service new 了 httpx 客户端，再去 dispose 找关闭它们的代码——没有，dispose 只关写死的 self._http。造资源的函数不拥有 dispose 权，拥有 dispose 权的 container 不知道资源存在，归属错位"。

**防守话术**："`build_gateway_service` 是路线装配函数，造了 L1/L2 两个 httpx 客户端但它是局部变量，函数返回就丢。`Container.dispose` 是枚举式关闭，只关写死的 `self._http`，新造的资源不主动登记就悬空。根因是生命周期归属错位——造的不管销毁，管的不知道有它。"

---

### B. 构造者即销毁者（修复判断 + 决策交还用户）

#### B.1 决策点：client 由谁构造

修之前我没自行假设，而是把选项和权衡列给用户：

1. **container 构造并注入**（用户选定）：在 `_wire_agentic` 里造 `l1_http`/`l2_http` 存实例属性，作为参数注入 `build_gateway_service`。container 同时负责构造与 dispose，与现有 `self._http` 模式一致；build 函数专注领域装配。代价：改 `build_gateway_service` 签名。
2. **build 函数构造 + 注册**：`build_gateway_service` 内继续造，通过 `container.register_http_client()` 登记到待关闭列表，dispose 遍历关。改动最小、route E 构造自包含。代价：build 函数要调 container 注册方法，且 dispose 是列表遍历。

用户选了 1。

**为什么是亮点**：两个判断。一是**构造者即销毁者**——谁 dispose 就该谁 construct（至少拥有从构造起的引用），否则 dispose 闭包里关的对象跟构造处不是同一份心智模型，容易漏。container 是生命周期所有者（有 dispose），由它构造 L1/L2 client，与 `self._http` 同构。二是**不自行假设、不确定交还用户**——构造位置是组合根职责划分的真实取舍（资源所有权归 container vs 装配自包含），自行拍板可能选错用户意图。把选项、权衡、推荐摆清楚让用户定，是资深信号。面试官问"你怎么定的"，答"我没自己拍，列了两个选项交还用户——container 构造注入 vs build 函数造+注册，前者构造者即销毁者与 self._http 一致，后者改动最小。用户选前者我执行"。

**防守话术**："构造者即销毁者——container 有 dispose 就该它 construct L1/L2 client，跟 `self._http` 同构。但这是组合根职责划分的取舍，我没自己拍，列选项交还用户：container 构造注入 vs build 函数造+注册。用户选了 container 构造注入。"

---

### C. keyword-only 注入 + 移除多余 import（契约显式化）

#### C.1 `*, l1_http, l2_http` 防误传

**处理**：`build_gateway_service` 签名从 `(container)` 改成 `(container, *, l1_http, l2_http)`。l1/l2 用 keyword-only，强制调用方具名传参，防止位置颠倒（l1/l2 base_url 不同，传反了会调错服务且不报错）。

#### C.2 移除不再使用的 import httpx

httpx 构造上移到 container 后，`build_gateway_service` 里不再用 httpx，删掉函数内 `import httpx`。`l1_http`/`l2_http` 参数类型标 `Any`（与 `L1DelegationClient` 的 `http: Any` 一致），不为类型标注单独 import httpx。

**为什么是亮点**：两处都体现**契约显式化 + 改动边界跟问题边界对齐**。keyword-only 把"这两个是外部注入的依赖"做成语法契约，不是注释约定。删多余 import 是顺手清理但不越界——没去动 L1/L2 委托客户端本身的逻辑（它们照旧 `self._http.post`）。面试官问"为什么用 keyword-only"，答"l1/l2 是同型注入，位置传参传反了不报错但调错服务，keyword-only 强制具名"。

**防守话术**："l1/l2 用 keyword-only 注入，防止位置颠倒调错服务。httpx 构造上移后 build 函数不用 httpx 了，删掉多余 import，类型标 Any 跟委托客户端一致。改动边界跟问题边界对齐，没动委托客户端逻辑。"

---

### D. 灰度懒装配的 dispose 条件性（None 守卫）

#### D.1 agentic 是灰度开关，dispose 时 client 可能是 None

**问题**：agentic 路线是灰度开关（`settings.agentic.enabled`），没启用时 `_wire_agentic` 不执行，`_l1_http`/`_l2_http` 保持 None。dispose 是无条件的（lifespan shutdown 总会调），如果直接 `await self._l1_http.aclose()` 会 AttributeError。

**处理**：dispose 里 `for client in (self._l1_http, self._l2_http): if client is not None: await client.aclose()`。None 守卫。

**为什么是亮点**：体现**懒装配 + 灰度开关对 dispose 的影响**。MES 的 `self._http` 在 `__init__` 构造（急切），dispose 时一定非 None；L1/L2 在 `_wire_agentic` 构造（懒、按开关），dispose 时可能 None。同一个 dispose 方法要同时兜两种——急切的直接关、懒的先判 None。很多人复制 `self._http` 的关闭模式会漏掉 None 判断，灰度未启用时 shutdown 崩。面试官问"为什么不像 `self._http` 那样直接关"，答"`self._http` 是 `__init__` 急切造一定非 None，L1/L2 是 `_wire_agentic` 懒造、agentic 没启用就是 None，dispose 无条件执行必须 None 守卫，否则灰度关时 shutdown 崩"。

**防守话术**："agentic 是灰度开关，没启用 `_wire_agentic` 不跑，`_l1_http` 是 None。dispose 是 shutdown 无条件调的，不能像 `self._http` 那样直接 aclose——`self._http` 是 `__init__` 急切造一定非 None，L1/L2 是懒造可能 None。所以 None 守卫，灰度关时 shutdown 不崩。"

---

### E. 验证纪律：4 个失败证为预存（不甩锅给"测试本来就坏"）

#### E.1 git stash 在干净树上复现

**验证**：跑全量 82 测试，78 过 4 失败。4 个失败（`test_recall_relevance[bm25/hybrid]`、`test_seed_resolver_regex`、`test_a_enriches_suggested_action_from_b`）都是 B/A 路线检索质量相关，跟 httpx 生命周期八竿子打不着。但"看着无关"不够，我用 git stash 把我的改动暂存、在干净树上重跑这 4 个——同样失败。证为预存，与本次无关，恢复改动。

**为什么是亮点**：这是"测试通过 ≠ 我的改动没引入问题"的逆向版本——**测试失败也要证明不是我引入的**。很多人改完看到失败就甩锅"这测试本来就坏"或"跟我无关"，但不验证。用 git stash 在干净树上复现，是把"无关"从断言变成证据。而且 4 个失败恰好是 B/A 路线（document/traceability），我改的是 E 路线（agentic）+ container dispose，路由上隔离——但隔离是推断，stash 复现是实证。面试官问"测试有失败你怎么确定不是你搞的"，答"git stash 暂存我的改动，干净树上跑同样 4 个失败，证为预存。4 个都是 B/A 检索质量，我改 E+container，但隔离是推断、stash 是实证"。

**防守话术**："改完 82 测试 78 过 4 失败。4 个是 B/A 检索质量，看着跟 httpx 无关，但我不靠'看着无关'甩锅——git stash 暂存改动、干净树复现同样 4 失败，证为预存。隔离是推断，stash 是实证。"

#### E.2 Windows Store python 占位符的坑

附带：本机 `python`/`python3` 是 Windows Store 占位符（未真正安装），跑 `python -c` 静默 exit 49、无输出。改用 `.venv/Scripts/python.exe` 做 AST/import 验证。

**为什么是亮点**：环境排障的小诚实——不把环境问题当"测试过了"，找到真正的解释器验证。

---

### F. 范围纪律：不顺手扩到 #2/#5

**处理**：待办清单里 #1（httpx 泄漏）和 #2（MES base_url 硬编码）相邻、#5（agentic 集成测试）相关。我只做 #1，没顺手改 #2（配置项）也没补 #5（集成测试）。

**为什么是亮点**：**改动原子性**。#1 是资源泄漏、#2 是配置可配置化、#5 是测试补全，三个独立问题。顺手夹带会让"修泄漏"这个 diff 混入配置变更和测试，reviewer 难审。每个待办独立做、独立验证。面试官问"为什么不顺手把 MES 硬编码也改了"，答"那是 #2 独立问题，配置可配置化跟资源泄漏不是一个变更，夹带让 diff 难审。我只做 #1，#2/#5 另开"。

**防守话术**："待办 #1/#2/#5 相邻但独立——泄漏/配置/测试。我只做 #1，没顺手改 #2 配置也没补 #5 测试，变更原子性，diff 好审。"

---

### G. 与 factorybot httpx 泄漏的同构（跨服务复现模式）

**识别**：factorybot 那轮代码优化里，`wiring.py` 创建 `httpx.AsyncClient`、lifespan shutdown 只打日志不 aclose，是同类泄漏（见《代码优化与重构实战》卡片 B）。这次 FactoryRAG 的 L1/L2 是同一类病的不同实例——**跨服务复现的资源生命周期归属问题**。

**为什么是亮点**：**同一类 bug 在两个服务都出现，说明是系统性模式而非个案**。处理思路也同构：把资源收口到生命周期所有者（factorybot 收到 `Container.shutdown`，FactoryRAG 收到 `Container.dispose`）。能讲"我在两个服务都识别并修了 httpx 生命周期悬空，形成构造者即销毁者的统一纪律"，比单次修复有体系感。面试官问"这个泄漏你怎么一眼看出的"，答"factorybot 那轮修过同类——`wiring.py` 造 httpx 不关，所以看 FactoryRAG 的 `build_gateway_service` 造 httpx 就直接去 dispose 找关闭，没有就是同病"。

**防守话术**："factorybot 的 `wiring.py` 修过同类——造 httpx 不关。所以看 FactoryRAG `build_gateway_service` 造 L1/L2 httpx，直接去 dispose 找关闭代码，没有就是同病。跨服务复现说明是系统性模式，统一用构造者即销毁者收口。"

---

## 3. 核心应答话术（高频问题，口语化背熟）

### 话术 1：你这次最核心的发现是什么

"最核心是**识别资源生命周期归属错位**。`build_gateway_service` 装配 E 路线时造了 L1/L2 两个 httpx 客户端，传给委托客户端后引用就丢了，`Container.dispose` 是枚举式关闭只关写死的 `self._http`，L1/L2 悬空泄漏。根因不是忘了 aclose，是造资源的地方不拥有销毁权。一个决策点我交还用户——client 由谁构造，用户选 container 构造注入，构造者即销毁者，跟 `self._http` 同构。**dispose 是枚举式不是注册式，新资源不登记就悬空**——这是最大的判断。"

### 话术 2：为什么不直接在 build 函数里加 aclose

"因为 build 函数不拥有销毁时机。`build_gateway_service` 是装配函数，造完就返回，lifespan shutdown 调的是 `Container.dispose` 不是 build 函数。在 build 函数里加 aclose 没有触发点。而且构造者即销毁者——谁 dispose 谁该 construct，否则 dispose 闭包关的对象跟构造处不是同一心智模型容易漏。所以把构造上移到 container，dispose 统一关。"

### 话术 3：client 由谁构造这个决策你怎么定的

"我没自己拍，列了两个选项交还用户：container 构造注入 vs build 函数造+注册。前者构造者即销毁者、跟 `self._http` 同构，代价是改 build 签名；后者改动最小、route E 自包含，代价是 build 要调 container 注册。这是组合根职责划分的真实取舍，用户选了前者我执行。不确定的不假设，让用户定。"

### 话术 4：dispose 为什么不像 self._http 直接关

"`self._http` 是 `__init__` 急切造的，dispose 时一定非 None。L1/L2 是 `_wire_agentic` 懒造的，agentic 是灰度开关没启用就保持 None，而 dispose 是 shutdown 无条件调的。直接 aclose 会 AttributeError。所以 None 守卫——急切的直接关、懒的先判 None，同一个 dispose 兜两种。"

### 话术 5：测试有失败你怎么确定不是你引入的

"改完 82 测试 78 过 4 失败。4 个是 B/A 路线检索质量，我改 E+container，看着无关。但我不靠'看着无关'甩锅——git stash 暂存我的改动、干净树上复现同样 4 失败，证为预存。隔离是推断，stash 是实证。agentic 的 `test_route_graph` 全绿。"

---

## 4. 深度问答（技术深挖）

**Q：httpx.AsyncClient 不 aclose 真的会泄漏吗？进程退出不就回收了？**
A：正常 shutdown（SIGTERM 优雅下线）走 lifespan -> dispose，这时进程不退出、Pod 可能被复用或滚动更新，不关的连接池句柄在反复重启里累积。进程被 kill -9 时 OS 确实回收 socket，但优雅下线场景不关就是泄漏。而且 dispose 关的是"主动释放连接池、发优雅关闭帧"的语义，不只是防句柄泄漏。

**Q：为什么用 keyword-only 而不是普通位置参数？**
A：l1_http/l2_http 是同型注入（都是 httpx.AsyncClient），位置传参如果传反了——l1 的 base_url 指向 l2 的服务——类型不报错、运行时调错服务（diagnose 打到 draft 端点），难排查。keyword-only 强制具名 `l1_http=...`，传反在调用处就明显。这是把"别传反"从注释约定做成语法契约。

**Q：dispose 的关闭顺序重要吗？**
A：L1/L2 的 httpx client 跟 engines（DB）/embedding/reranker 无依赖，关哪个先都行。我把 L1/L2 紧跟 `self._http` 后面关，纯为可读性——httpx 客户端聚一组。没跨依赖所以无序敏感。如果 L1/L2 依赖某个 engine（比如委托前要先查 DB），那要先关 L1/L2 再关 engine，这里没这依赖。

**Q：启动失败时（wire_routes 抛错）L1/L2 会泄漏吗？**
A：会，但这是预存行为不是本次引入。lifespan 是 asynccontextmanager，yield 前抛错（wire_routes 失败）则 yield 后的 cleanup（stop_consumers + dispose）不执行——所以 `self._http` 和 engines 在启动失败时也不关。我没为 L1/L2 单独加 try/except，保持与 `self._http` 一致。而且启动失败意味着进程起不来、随即退出，OS 回收 socket，不是运行期累积泄漏。真正的运行期泄漏（优雅 shutdown）已由本次 dispose 修复覆盖。如果要彻底，lifespan 该用 try/finally 包 yield 前后，那是独立加固不在本次范围。

**Q：构造上移到 container，会不会让 container 知道太多 agentic 细节？**
A：container 是组合根，本来就知道 `settings.agent`（l1_base_url 等）。造两个 httpx client 是基础设施构造，跟它造 `self._http`（MES）同性质——都是"为某条路线造 http 客户端"。领域装配（registry/delegator/gateway）仍在 `build_gateway_service`。资源归 container、领域装配归 build 函数，职责切干净。如果觉得 container 不该知道 agent 配置，可以把 client 构造也下沉到一个 agentic 专用的工厂，但那是过度设计——container 造 httpx client 跟它造 engines 一样自然。

**Q：None 守卫会不会掩盖"应该造却没造"的 bug？**
A：不会，因为 None 是有意的灰度状态。`agentic.enabled=False` 时 `_wire_agentic` 不跑、`_l1_http` 故意保持 None，dispose 跳过是正确行为。如果 `agentic.enabled=True` 但 `_wire_agentic` 中途抛错导致 `_l1_http` 造了 `_l2_http` 没造，None 守卫会让 `_l1_http` 关、`_l2_http` 跳过——但那种情况启动就失败了（`build_gateway_service` 抛错），进程起不来，dispose 也不执行（见上 Q）。所以 None 守卫只会在"灰度关"这种正常状态下生效，不掩盖 bug。

---

## 5. 压力追问（陷阱题，考诚实）

**Q：你说构造者即销毁者，那 self._http 也是 container 造的，你怎么没把它也改成注入式？**
A：`self._http` 是 shared 基础设施（MES 客户端），在 `__init__` 急切造、所有路线共享，不需要注入——它就是 container 自己的属性。L1/L2 是 E 路线专属、灰度懒装配，才需要"造了注入给 build 函数"。两者都是 container 构造 container 销毁，归属一致；区别是急切 vs 懒、shared vs 路线专属，注入与否是装配方式选择不是归属问题。**构造者即销毁者讲的是归属，`self._http` 和 L1/L2 都符合**。

**Q：4 个预存失败你不修就走，是不是放过问题？**
A：没放过，是变更原子性。4 个失败是 B/A 路线检索质量（BM25/hybrid 召回、seed resolver、SOP 富化），跟 httpx 生命周期无关，属于另一条待办线。我在 git stash 干净树上证了它们预存。修它们要另开变更、定位检索质量问题，不是"修泄漏"的附带品。混进来会让"修泄漏"diff 混入检索逻辑改动，难审。**放过不是不管，是不在这个变更里改，它们在待办里有独立追踪**。

**Q：你只改了 2 个源码文件 +25/-8，这么小的改动叫 P0？**
A：P0 是按生产风险分级不是按行数。这个泄漏在优雅 shutdown 路径累积连接句柄，长时间运行的 Pod 反复滚动更新会放大，是真实生产风险。改动小恰恰说明根因定位准——归属错位修对了，几行就收口。行数不是风险等级也不是价值的指标。而且 factorybot 修过同类，识别快所以改动精准。

**Q：生命周期测试没补，你怎么保证 dispose 真的关了？**
A：诚实讲：没补行为测试，靠代码审查 + import/AST 验证 + 既有测试无回归。补 dispose 行为测试要完整构造 Container（要 mysql/redis/llm/embedding 全套），属待办 #5（agentic 集成测试）的范畴，本次没做。代码上 dispose 的 None 守卫 + aclose 调用是直白逻辑，审查可验。**不吹"已验证关闭"，标了未测**。

**Q：keyword-only 防误传，但 container 调用就一处，有必要吗？**
A：当前调用方就 `_wire_agentic` 一处，但 `build_gateway_service` 是组合根入口、文档化的公共装配点，以后可能多处调或被测试直接调。keyword-only 是廉价的前置契约——一处调用时零成本，多处或误调时立刻挡住。比"注释写别传反"可靠。防御性契约在组合根这种稳定接口上是值得的。

---

## 6. 指标卡片（背下来）

### 卡片 A：改动规模（最硬，直接给）

| 维度 | 数值 | 出处 |
|------|------|------|
| 改动文件 | 2 源码 + 1 待办勾选 | git diff --stat |
| 净改动 | +25/-8（3 文件） | git diff --stat |
| 修复位置 | 2（container `_wire_agentic`+dispose / `build_gateway_service` 签名） | [container.py](../FactoryRAG/app/shared/web/container.py) / [__init__.py](../FactoryRAG/app/routes/agentic/__init__.py) |
| 测试 | 82 收集 / 78 过 / 4 失败（预存） | pytest 实跑 |
| 预存失败验证 | git stash 干净树复现 4 失败 | 本次 |
| 交还用户决策点 | 1（client 由谁构造） | 本次对话 |
| 验证手段 | AST 解析 + 模块 import + 签名 inspect | .venv python |

### 卡片 B：归属与修复（识别 + 处理）

| 缺陷 | 后果 | 修复 |
|------|------|------|
| L1/L2 httpx client 装配后引用丢失 | shutdown 连接池泄漏 | 构造上移 container + dispose 收口 |
| dispose 枚举式不含 L1/L2 | 新资源悬空 | dispose 遍历 `_l1`/`_l2_http` |
| agentic 灰度未启用时 client 为 None | 直接 aclose 会崩 | None 守卫 |
| l1/l2 同型位置传参易传反 | 调错服务不报错 | keyword-only 注入 |
| build 函数残留 import httpx | 死 import | 移除 |

### 卡片 C：能力表述（讲做了什么）

| 能力 | 表述 |
|------|------|
| 资源生命周期 | 识别"造的不管销毁"归属错位，构造者即销毁者收口 |
| 组合根职责 | 资源归 container、领域装配归 build 函数，切干净 |
| 灰度懒装配 | 急切（self._http）vs 懒（L1/L2）的 dispose 条件性，None 守卫 |
| 验证纪律 | 测试失败用 git stash 证预存，不甩锅 |
| 工程纪律 | 决策交还用户、范围原子（不扩 #2/#5）、残留诚实交代 |

---

## 7. 红线与遗留（面试别翻车）

**红线**：
- ❌ "彻底杜绝资源泄漏" / "已上线验证" -- 启动失败路径未兜底，未真实部署。
- ❌ "dispose 已测试验证关闭" -- 没补行为测试，靠审查。
- ❌ "性能提升 X%" -- 没测性能，不编。
- ❌ "4 个失败是我修好的" -- 是预存，与本次无关。
- ❌ "client 由谁构造我自己定的" -- 是交还用户拍板的，别揽功也别甩锅。
- ✅ 正确讲法："我修了 FactoryRAG E 路线 L1/L2 委托 httpx 客户端的资源泄漏：根因是生命周期归属错位（造的不管销毁），决策点交还用户选了 container 构造注入（构造者即销毁者），dispose 收口 + None 守卫（灰度懒装配）+ keyword-only 防误传。验证 82 测试 78 过、4 失败 git stash 证为预存。残留：生命周期测试未补属 #5、启动失败路径未单独兜底与 `self._http` 一致。"

**遗留项清单（主动交代，反加分）**：

| 遗留 | 现状 | 倾向 |
|------|------|------|
| dispose 生命周期行为测试 | 未补（构造 Container 重） | 属待办 #5 agentic 集成测试 |
| 启动失败路径资源回收 | lifespan yield 前抛错不调 dispose（预存） | 独立加固 try/finally 包 yield |
| ~~4 个预存测试失败~~ | ✅ 已修(2026-07-15)：1 真 prod bug(SN 正则截断) + 1 测试基建 flaky(FakeEmbedder hash()) + 2 测试调整 | 见 [4预存测试失败修复篇](FactoryRAG-4预存测试失败修复-面试闪光点与话术.md) |
| MES base_url 硬编码（#2） | 相邻未改 | 独立 #2 |
| ~~agentic 集成测试（#5）~~ | ✅ 已修(2026-07-15)：14 例端到端 + 修 AgentState audit_id 证据链断链 + 2 个 _build_answer 行为 bug | 见 [agentic集成测试补全篇](FactoryRAG-agentic集成测试补全-面试闪光点与话术.md) |

被问"还有什么没做"时，老实列出遗留 + 倾向，体现**知道自己不知道什么**，比假装完成得分高。

---

## 8. 一句话定位（收尾用）

"这次修的价值不在改了几行，而在**识破资源生命周期归属错位——造 httpx 客户端的地方不拥有销毁权，dispose 是枚举式不是注册式、新资源就悬空；用构造者即销毁者把 L1/L2 收口到 Container（与 `self._http` 同构），用 None 守卫兜灰度懒装配、用 keyword-only 显式化注入契约**——做了的讲透、决策点摆明谁拍板、4 个失败用 stash 证为预存不甩锅、残留不藏，与 MES 追溯的'可回溯、不悬空'同构：资源有归属、关闭有收口、灰度有守卫。"
