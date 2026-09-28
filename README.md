# K8s 运维助手 Agent（LangChain + MCP）

面向 SRE 日常运维的 AI Agent：通过 MCP 连接已有的 K8s MCP Server 查询集群状态，通过互联网搜索检索官方文档与排障指南，支持 CLI 多轮对话与飞书机器人两种入口。

需求来源：`E:\SRELinux\AI Agent\day07\5.K8s 运维助手 Agent 开发项目（基于 LangChain + MCP）.pdf`（31 页）。

## 快速开始

```bash
# 1. 激活虚拟环境（PowerShell 用 .venv\Scripts\Activate.ps1）
source .venv/Scripts/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 准备配置
cp .env.example .env    # 然后填写真实值

# 4. 启动 CLI
python cli.py

# 或启动飞书机器人（需先配好 FEISHU_APP_ID / FEISHU_APP_SECRET，见下方「飞书入口」）
python -m feishu.feishu_bot
```

配置要点：LLM 用 `qwen-plus`（走 DashScope 兼容接口）；搜索用 `duckduckgo`（免费无需 Key），
但在本机网络下必须走代理，需在 `.env` 里把 `SEARCH_PROXY` 填成你的代理地址（详见下方「搜索工具」）。

## 架构

四层，自上而下（对应 PDF §3.1）：

```
交互入口层     cli.py（终端）        feishu/feishu_bot.py（飞书）
                    \                    /
Agent 核心层         agent.py
                 意图识别 · 工具路由 · 对话记忆
                 LangChain 1.2.10 create_agent（底层 LangGraph）
                    /                    \
工具层        工具            tools/search.py    互联网搜索
                    \                    /
外部依赖层     mcp-k8s-eye (SSE)   DashScope qwen-plus   飞书开放平台
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 读取 `.env`，集中管理 LLM / MCP Server / 搜索 / 飞书 配置。所有魔法值只在这里出现一次 |
| `console_boot.py` | 控制台 UTF-8 引导，两个入口共用，必须在导入 rich 之前调用 |
| `prompts.py` | K8s SRE 专家系统提示词 |
| `tools/search.py` | 搜索工具封装，`duckduckgo` 与 `tavily` 可切换 |
| `agent.py` | 初始化 MCP 客户端、发现并加载工具、创建 Agent、管理对话记忆、流式输出 |
| `cli.py` | 终端交互循环、`quit` / `clear` / `tools` 命令、工具调用过程可视化 |
| `feishu/feishu_bot.py` | 飞书机器人入口，长连接接收事件、卡片形式回复 |

## 开发阶段与提交记录

按阶段推进，每阶段一次 commit，git 历史即学习路径。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 仓库初始化、依赖、配置模板 | ✅ |
| 1 | `config.py` + `prompts.py` | ✅ |
| 2 | `tools/search.py` | ✅ |
| 3 | MCP 客户端接载与工具发现（含 mock 回退） | ✅ |
| 4 | `agent.py` Agent 组装 + 记忆 + 流式 | ✅ |
| 5 | `cli.py` 终端界面 | ✅ |
| 6 | `feishu/feishu_bot.py` 飞书入口 | ✅ |
| 7 | 虚拟机开机后真机联调（含 MCP 工具调用加固） | ✅ |

## CLI 使用

```bash
python cli.py
```

进入后直接提问即可，三个内置命令：

| 命令 | 作用 |
|---|---|
| `quit` / `exit` / `q` | 退出（`Ctrl+C`、`Ctrl+D` 同效） |
| `clear` | 清空对话历史，只清消息，不重载工具 |
| `tools` | 表格列出全部工具及其来源（MCP / 本地搜索） |

一轮问答的终端输出顺序（对应 PDF §1.4）：

```
You: 查看集群有哪些 node 节点
  🔧 调用工具: resource list  {"kind": "Node", "namespace": ""}
     ✅ 工具返回完成  (0.8s)
╭──────────────── Agent ────────────────╮
│ （Markdown 渲染的回答，流式刷新）      │
╰───────────────────────────────────────╯
```

实现要点：

- 回答用 `rich.Live` + `Panel(Markdown(...))` 流式刷新。工具调用发生在回答生成之前，
  那时 `console.print` 会把内容推到 Live 面板**上方**，不会打断正在刷新的面板。
- 刷新节奏由 `_RENDER_INTERVAL`（120ms）自己控制，所以 `Live(auto_refresh=False)` 关掉了 rich
  的自动刷新线程——否则那个线程会按自己的帧率把**同一份 Markdown** 多解析几遍。之所以不用更密的
  80ms：`_answer_panel` 每帧都要把"目前为止的全部文本"重新解析一遍 Markdown，这是 O(n) 的，
  回答到三四千字时单帧渲染就逼近 80ms，间隔太密反而让终端看起来一卡一卡的。
- 非终端环境（`python cli.py > log.txt`、管道输入）自动关闭 Live，改为结束后一次性打印，
  否则 Live 的反复整屏重画会把日志刷爆。
- 启动时执行 `console_boot.bootstrap_console()`：Windows 控制台默认 GBK 代码页，`✅`/`🔧`/`🤖`
  会报 `UnicodeEncodeError` 或乱码，所以在**导入 rich 之前**把控制台代码页与
  Python 的 stdin/stdout/stderr 一起切成 UTF-8，并打开 ANSI 转义。

因此有个约定：**入口脚本必须自己调 `console_boot.bootstrap_console()`**，而 `python -m agent`、
`python -m tools.search` 这类自测代码只用 GBK 安全字符（中文 + ASCII），不打印 emoji。

## 飞书入口

```bash
python -m feishu.feishu_bot        # 在项目根目录启动
```

启动前提（`.env` + 飞书开放平台）：

```bash
# .env
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx
```

1. 开发者后台 -> 你的应用 -> **事件订阅** -> 订阅方式选 **「长连接」**
2. 订阅事件 **「接收消息 `im.message.receive_v1`」**
3. 开通权限 `im:message`（发消息）、`im:message:send_as_bot`
4. 开通权限 **`cardkit:card:write`**（创建卡片实体，流式更新的前提），改完要**发布新版本**才生效

用**长连接**而不是 Webhook 的原因：事件订阅默认要求一个公网可回调的地址，长连接由客户端主动连飞书，
本地开发不需要公网 IP 和内网穿透（见下方「与需求文档的差异」第 4 条）。

缺第 4 条权限时的症状：建卡片实体返回 `99991672`，机器人一条消息都不会回。

### 一条消息答一轮：飞书原生流式卡片

一轮问答只占用**一条**飞书消息，不会刷屏。走的是官方「流式更新卡片」通道：

```
用户发问
  └─ cardkit.v1.card.create        建卡片实体，拿 card_id
       └─ im.v1.message.reply      用 card_id 回复一张「正在分析…」卡片
            └─ 后台协程循环 cardkit.v1.card_element.content(card_id, element_id, 全量文本, sequence++)
                 └─ 收尾推最终文本 -> cardkit.v1.card.settings 关 streaming_mode
```

四条只能靠真机试出来的约定（探针实测，文档没写死或写得含糊）：

| 事项 | 实测结论 |
|---|---|
| `content` 传什么 | **累计全量文本**，不是增量；增量由飞书计算，我们只管把当前完整文本推上去 |
| `sequence` | 整张卡片生命周期内**严格递增**，且内容更新与收尾的 settings 更新**共用同一个计数器** |
| 怎么结束流式 | 必须再调一次 `card.settings`，载荷 `{"streaming_mode": false}`；不调的话打字光标一直转 |
| 用 card_id 发消息 | `msg_type="interactive"` + `content={"type":"card","data":{"card_id":...}}`（另两种写法返回失败） |

还有两条硬约束：能承载流式的组件只有 `plain_text` / `markdown`，且该组件**必须自带 `element_id`**。
所以整张卡只能有一个正文元素——工具轨迹不再用折叠面板，改成 markdown 列表项拼进同一段文本
（列表项是为了绕开另一个坑：单个 markdown 组件里裸 `\n` 属于软换行，渲染时可能被忽略）。
状态行也写进正文末尾（`> ✅ 完成 ｜ 2 步工具调用 ｜ 耗时 42.3s`），因为副标题属于卡片头，
改它得走整卡更新，不值当。

**推送为什么必须放在后台协程里，而不是写在 token 循环里。** 早先的写法是每来一个 token 就
`await maybe_flush()`，看起来只是"节流"，实际上一次更新是真实的 HTTPS 往返（整卡 PATCH 约
0.2~0.5s，流式更新实测约 0.5s），而这段 await 发生在 `async for event in bot.astream(...)` 里面
——**这段时间 token 循环是挂起的、消费不了模型输出**，只能堆在缓冲区等请求回来一次性吐出。
观感就是"冻结一秒多 → 蹦出一大块字"。

用假客户端（固定 0.35s 往返）灌 3 秒 token 流，三种写法的代价一目了然：

| 写法 | token 阶段耗时 | 帧数 / 帧间隔 |
|---|---|---|
| 更新写在循环里 await（最初的 `maybe_flush`） | **7.09s**（流被拉长 2.4 倍） | 不规则 |
| 后台协程 + 0.3s 时间闸 | 3.05s | 4 帧 / 0.65s |
| 后台协程 + **不设帧率闸**（现在的做法） | 3.00s | **10 帧 / 0.35s（=一次往返）** |

顺带两个细节：内容指纹没变就跳过这一帧（正在等工具返回时文字是静止的，别白烧更新额度）；
收尾要先 `stop()` 等后台协程真的退干净、再推最终内容，**不能 cancel** —— 取消只是让 `to_thread`
外面抛异常，那个已经在跑的请求线程并不会停，可能晚于最终内容落地、把回答盖回一份旧快照。

真机效果（同一句「查看集群有哪些节点和正在运行的 pods」）：一轮 46 秒推 **33 帧、0 帧被拒**，
而带 0.3s 闸的上一版是 23 帧；折算到单位内容，帧率提升约 **30%**。对比最初整卡 PATCH 方案
（1.2s 闸）的约 0.6 帧/秒，现在约 **1.8 帧/秒**。

**这已经是我们这侧的天花板。** 不设闸之后，帧间距就等于一次网络往返（实测约 0.5s），
再快只能靠并发（挂两个请求不等前一个返回）。但 `sequence` 要求严格递增，两个请求的**到达顺序**
不保证与发送顺序一致，可能让卡片短暂退回旧文本——为多几帧不值当，所以没做。
另外飞书客户端逐字动画的播放速度不在开发者可控范围内：接口只接受「全量文本 + 序号」，没有速度参数。

帧数与成败都进了收尾日志，所以"刷太快会不会反而被限流"这件事是可观测的，不是靠感觉：

```
[14:46:57] 已回复（4 条工具记录，3782 字） | 流式推送 23 帧成功，无丢帧
```

卡片仍是**卡片 JSON 2.0**结构，正文是 `markdown` 组件，支持标题、表格、代码块、引用、分割线，
所以 LLM 的回答**原样贴进去**就能正常渲染。改走流式之后卡片头不再变化（副标题、标题栏颜色都
属于卡片头，动它要走整卡更新），状态一律写在正文末尾的引用行里。

### 线程与事件循环

`lark-oapi` 的 `ws.Client.start()` 是「阻塞 + 自建事件循环」的，Agent 自己也需要一个事件循环，
两者不能共用。因此分两条线：

```
主线程                        lark-ws 线程（daemon）
agent_loop.run_forever()      ws_client.start()  —— 阻塞，负责协议与心跳
     ↑                                |
     |  run_coroutine_threadsafe       |  收到消息 -> 同步回调 on_message()
     +--------------------------------+
                 process_message(...)
```

事件回调**必须立刻返回**：它跑在 lark 自己的循环里，一旦在里面等 LLM（几十秒），心跳发不出去，
连接会被飞书断掉。所以回调只做一件事——把协程投递到 `agent_loop` 就返回。

会话记忆按 `thread_id = f"feishu:{chat_id}"` 隔离，每个群/私聊一份历史；发 `clear` / `重置` 清空当前会话。
每条消息先经 `message_id` 去重（飞书会重投事件），群聊里只有被 @ 到才回应。

## 搜索工具（DuckDuckGo + 代理）

搜索后端默认用 **DuckDuckGo**（免费、无需 Key）。它的底层后端包含 `search.yahoo.com`、
`www.mojeek.com` 等域名，**在本机网络下均不可直连**，必须经代理访问：

```bash
# .env
SEARCH_PROVIDER=duckduckgo
SEARCH_PROXY=http://127.0.0.1:7892     # 换成你本机代理的实际地址与端口
```

实现上**没有**用全局 `HTTPS_PROXY`：那样会把 DashScope 的 LLM 调用也一起代理掉。改为把
`SEARCH_PROXY` 显式传给 `ddgs.DDGS(proxy=...)`，作用域只限搜索。这也是没用
`langchain-community` 的 `DuckDuckGoSearchResults` 的原因——它的封装不允许单次搜索传代理。

备选方案是 Tavily（`SEARCH_PROVIDER=tavily` + `TAVILY_API_KEY`），它在本机网络可直连，无需代理。

## MCP 工具调用加固（真机联调后补的三道闸门）

真机联调第一次提问就翻车：一句"查看集群有哪些节点和正在运行的 pods"跑了 **431 秒**，
最后还得出"pods 为空"的错误结论。排查下来两个根因都在 Server 侧（不是模型的锅），对策分三层：

| 现象 | 根因 | 对策 |
|---|---|---|
| 结论错：报告集群里没有 pods | LLM 按 kubectl 的习惯传 `namespace="all"`，而 Server 把它当**字面命名空间名**去查，返回空列表；空列表又被 LLM 当成"确实没有" | `agent.py` 的 `_fix_namespace_args()` 把 `all`/`*`/`-a`/`all-namespaces` 一律改写成 `""`（Server 用空串表示"全部命名空间"），并打 `[WARN]` 留痕 |
| 挂死：整轮对话 431s 没有响应 | `resource list` 缺 `namespace` 时 Server 直接掐断连接，一个字节都不回，客户端只能干等 | ① 同样在 `_fix_namespace_args()` 里给 `resource_list` 补 `""`；② `_harden_tool()` 给每次工具调用套 `asyncio.wait_for(TOOL_CALL_TIMEOUT)` |
| 绕远路：`* analyze` 返回空后一路瞎猜 | 本机部署的 Server 上 `node analyze`/`pod analyze` 恒返回 `[]`，`deployment analyze`/`service analyze` 恒返回 `null` | `prompts.py` 新增「已知的工具坑」一节，明确告诉模型：列资源走 `resource list(kind, namespace)`，`* analyze` 返回空**不代表**资源不存在 |

包装点选在 `tool.coroutine`（`StructuredTool` 的异步实现）上，调用链是：

```
ToolNode._arun_one -> tool.ainvoke(...) -> StructuredTool._arun(...) -> self.coroutine(**args)
```

在这里既能拿到 LLM 传来的实参做兜底，又能把整个网络调用罩进超时；本地同步工具（`web_search`、
mock 工具）的 `coroutine is None`，会被自动跳过，不受影响。超时抛的是普通 `TimeoutError`，
LangGraph 的 `ToolNode` 会把错误文本当作**工具结果**回给 LLM，让它换工具或换参数重试，
而不是让整轮对话一直挂着。

同一个包装点还挂了结果瘦身：`resource list` 的返回会先投影成运维视图再交给模型（见下文
「对话记忆治理」里的**当轮结果瘦身**小节）。分工是——入参兜底管"查得到"，超时管"不挂死"，
瘦身管"别太胖"。

效果（同一句提问"查看集群有哪些节点和正在运行的 pods"）：

| | 加固前 | 加固后 |
|---|---|---|
| 总耗时 | 431.4s | 36.2s |
| 工具调用次数 | 7 次（前 4 次全空） | 2 次（各命中一次） |
| 实际入参 | `namespace: "all"` | `namespace: ""` |
| 结论 | pods 为空（错） | 3 节点 / 23 个 pods（对） |

## 对话记忆治理（轮数窗口 + 旧工具结果清理 + 当轮结果瘦身）

checkpointer 会把每轮 **ToolMessage 的原文**一起存档。`resource list` 拉一次全量 pods 就是
143KB YAML，从第二轮起每次模型调用都要把这段旧 YAML 重新预填一遍：同一句小问题实测
**空历史 12.9s、带历史 37.1s**（群里那轮更慢到 105.8s，因为它工具调用更多、回答更长，
而一轮里有几次工具调用就要做几次模型 pass，每 pass 都重读一遍历史）。

三道闸门（前两道在 `agent.py` 的 `MemoryWindowMiddleware`，第三道在工具包装层）：

| 闸门 | 作用 | 是否写存档 |
|---|---|---|
| `before_model` | 轮数窗口：历史超过 `MEMORY_WINDOW_ROUNDS` 轮（默认 10）时，用 `RemoveMessage` 整轮删除最老的轮次 | 是（历史不再无限增长） |
| `wrap_model_call` | 体积清理：把「已结束旧轮」的 ToolMessage 原文换成占位符，当前轮原样保留 | 否（只影响本次调用） |
| `_trim_list_result` | 当轮瘦身：`resource list` 的原始 YAML 投影成运维视图后才交给模型（详见下小节） | 是（进存档的就已经是精简版） |

两个设计细节：

- 删除永远对齐轮次边界（以 HumanMessage 为起点），不会只删 AIMessage 而留下它对应的
  ToolMessage，模型不会看到「有工具结果却没有工具调用」的残缺历史。
- 被清理的只是「原始证据」，上一轮的**结论**仍留在该轮最终回答（AIMessage）里，模型照常
  可以引用；真需要细节时占位符会提醒它重新调工具，而不是凭记忆猜。

### 第三道闸门：当轮结果瘦身（`TRIM_TOOL_PAYLOAD`）

上面两道闸门管的都是**旧轮**，而「当前轮」的工具结果必须留着——模型正要根据它作答，
清了就没法答题了。所以当前轮只能从**源头**减重：`resource list` 返回的原始 YAML 在交给
模型之前，先投影成运维视图（`agent.py` 的 `_trim_list_result`，挂在工具包装层里）。

投影保留了什么、丢了什么：

| 保留（答题要用） | 丢弃（运维问答用不上） |
|---|---|
| `name` / `namespace` / `labels` / `nodeName` / `phase` / `podIP` / `hostIP` | `spec.volumes` + `projected`（14.8KB）、`spec.tolerations`（4.9KB） |
| `containers[].image` 与 `resources.requests/limits` | `volumeMounts`（2.5KB）、`imageID`、`containerID`（各 70 字符 sha） |
| `containerStatuses` 的 `ready` / `restartCount` / `state` / `lastState`（含 `reason`、`exitCode`） | `metadata.annotations`、探针、环境变量、`serviceAccount` 等 |
| `abnormalConditions`：**只列非 `True` 的条件**（空列表即一切正常） | Node 的 `status.images` 清单压成 `imageCount` 一个数（12.9KB → 1 个数） |

真机效果（同一对提问，各自全新会话）：

| | 不瘦身 | 瘦身 | 变化 |
|---|---|---|---|
| turn1「集群有哪些节点和运行中 pods」 | 35.6s | **23.8s** | 快 11.8s（33%） |
| turn2「default 空间 pod 详细信息」 | 26.2s | **19.0s** | 快 7.3s |
| 列 Pod 的工具结果 | 135.2KB | **15.9KB** | 省 88% |
| 列 Node 的工具结果 | 20.3KB | **4.4KB** | 省 78% |

两点取舍要记下来：

- **回答会变短**（turn1 从 5734 字降到 2039 字）。原始 YAML 里那些探针、挂载、环境变量本来
  也答不出结论，模型很多时候是在把它们摊成篇幅。事实性内容（3 个节点、Ready 状态、IP、
  重启次数）两边都正确。
- **精简说明必须跟着结果一起给模型**（`_TRIM_NOTE`）：明确讲清「探针/挂载等字段已被裁掉、
  `abnormalConditions` 空表示正常、要完整定义请用 `resource describe`」。少了这句话，模型会把
  "看不到"当成"集群里没有"，然后开始编——这个坑在 namespace 那轮已经踩过一次。
- 只裁 `resource list`。`resource describe` / `resource get` 的语义就是「我要这个对象的完整
  定义」，裁掉等于把用户查的东西裁掉。想对照可把 `TRIM_TOOL_PAYLOAD=false` 退回原始 YAML。

## 回答篇幅控制（提示词侧）

`qwen3.8-flash` 答题全面但啰嗦：一句"集群有哪些节点和 pods"能写 3746 字。压缩靠的不是加一句
"请简洁回答"——那种笼统要求模型无法判定，实测无效果——而是把"啰嗦"拆成**六种可判定的形态**，
逐个下约束：

| 冗余形态 | 具体约束 |
|---|---|
| 过程旁白（"我来查询…""两个查询相互独立，我并行执行"） | 决定调工具时**直接发起调用**，不写过程；不复述问题 |
| 给正常项写解释（"⚠️ 有 NoSchedule 污点（正常）；`unschedulable: null` 即未 cordon"） | **只报异常，不解释正常**；无异常的对象备注列留空 |
| 表格无限加列 | 清单类**默认不超过 5 列**，只放用户问到的维度 + 状态，其余等追问 |
| 顺手做根因推测（"集群不久前发生过全局抖动"） | **不写没有依据的推测**；要么指出是哪个字段支撑，要么不提 |
| 结尾追问菜单（"需要我继续做下面哪一项？"＋3 条） | 结尾不给菜单；确有必要最多 1 条建议 |
| 命令写成一整块带注释脚本 | 下一步命令**最多 2 条**，一行一条，不加注释 |

长度预算要**分开算**，这是我第一版踩的坑：一开始写"清单类问题 600 字以内"，结果模型把表格也
压了（信息就丢了），而总量还是降不下来。改成「散文部分（不含表格）≤ 200 字，表格长度由对象
数量决定、不刻意压缩」之后才既短又不损信息。

真机对照（同一句提问、同一模型、各自全新会话）：

| | 改前 | 只压散文 | 加上列数/命令约束 |
|---|---|---|---|
| 总字数 | 3746 | 3412 | **1882（-50%）** |
| 其中表格 | 1852 | 2024 | 1404 |
| 其中命令块 | 167 | 397 | 167 |
| 其中散文与标题 | 1727 | 991 | **311（-82%）** |

信息没有损失：3 个节点、22 个 Pod 逐行重启次数、唯一异常（dashboard `exitCode=2`、终止与重启
只隔 20 秒）都还在，只是不再解释正常项、不再推测集群历史。**每格都是单次采样**，实际会浮动。

## 与需求文档的差异（实现时的依据）

1. **MCP 传输方式**：PDF §2.3 称使用 `streamable_http`，但 `mcp-k8s-eye` 实际以 **SSE** 提供服务（endpoint 以 `/sse` 结尾）。已按 SSE 实现，并把传输方式做成 `K8S_MCP_TRANSPORT` 配置项，切换时不改代码。
2. **参考代码缺失**：PDF §5「完整代码实现」只有一个 `k8s-ops-agent-cli.zip` 附件链接，PDF 内无可提取代码，全部代码从零实现。
3. **Python 版本**：PDF 用 3.11.3，本机 venv 为 3.12.8，上述依赖版本均兼容。
4. **飞书连接方式**：飞书事件订阅默认要求公网回调地址，本地开发不便。改用 `lark-oapi` 的 **WebSocket 长连接**模式，无需公网 IP 与内网穿透。
5. **`* analyze` 系列在本机 Server 上不可用**：PDF §1.4 把 19 个工具并列展示，示例回答也像是分析工具的产物。实测本机部署的 `mcp-k8s-eye` 上：`node analyze` / `pod analyze` 恒返回空数组 `[]`，`deployment analyze` / `service analyze` 恒返回 `null`。真正可用的是 `resource list` / `resource describe` / `resource get` / `pod logs`。因此提示词里把 `resource list` 定为主路径，并明确"`* analyze` 返回空 ≠ 资源不存在"。
6. **`resource list` 的入参语义与 kubectl 不同**：只接受 `kind` + `namespace` 两个参数（无 `apiVersion`）。`namespace=""` 表示全部命名空间；传 `"all"` 会被当成名为 "all" 的命名空间、返回空列表；**缺 `namespace` 时 Server 不返回任何响应**（连接被掐断，客户端一直挂着）。README 上面「MCP 工具调用加固」一节的三道闸门就是为这两条设计的。

## 安全提示

`mcp-k8s-eye` 暴露的 19 个工具中包含**写操作**：`resource delete`、`resource create or update`、`deployment scale`、`pod exec`。本项目按需求方决策**不做工具白名单、不做二次确认**，全部交给 LLM 自主调用。

因此：**不要将本 Agent 连接到生产集群。** 首次使用时先用只读类问题验证行为，再考虑放开写操作。

## 踩坑记录

- **`requirements.txt` 中文注释导致 pip 失败**：中文 Windows 下 pip 用系统编码（cp936/GBK）读取该文件，含中文注释会抛 `UnicodeDecodeError`。已约定此文件只用 ASCII 注释，中文说明放本文档。Python 源文件不受影响（默认 UTF-8）。
- **`mcp` 必须锁 1.x**：`langchain-mcp-adapters 0.2.1` 只声明 `mcp>=1.9.2` 没有上界，pip 会装 `mcp 2.x`，而 2.x 删掉了 `mcp.shared.context.RequestContext`，导致 `from langchain_mcp_adapters.client import MultiServerMCPClient` 直接 ImportError。已在 requirements 中加 `mcp<2`。
- **`langgraph-prebuilt` 必须锁 1.0.8**：`langchain 1.2.10` 限制 `langgraph<1.1.0`，而 `langgraph-prebuilt` 从 1.0.9 起调用 `langgraph.runtime.ExecutionInfo`（langgraph 1.1 才引入），两者范围重叠但 API 不兼容，pip 默认装 1.0.13 会让 `from langchain.agents import create_agent` 报错。已锁 1.0.8。
- **DuckDuckGo 在本机网络必须走代理**：实测其全部后端域名（`search.yahoo.com`、`www.mojeek.com`
  等）直连均超时或被拒；系统里虽配过代理 `127.0.0.1:7892` 但当前未启用（`ProxyEnable=0`，
  端口无监听）。因此要用 DuckDuckGo 必须先打开代理并填 `SEARCH_PROXY`。已验证 `ddgs` 底层
  遵守 `HTTP(S)_PROXY` 环境变量（用本地假代理抓到 CONNECT 请求），但为避免连带代理 DashScope，
  项目改用显式传参。
- **`duckduckgo-search` 包名已失效**：该包已改名为 `ddgs`，且 `langchain-community 0.4.2` 的
  DuckDuckGo 封装也只认 `ddgs`。装旧包名会抛 `Could not import ddgs python package`。
  依赖里装的是 `ddgs`。
- **PDF 自身的版本矛盾**：§2.1 技术栈表写 `langgraph 1.1.x`，§4.2 具体版本写 `1.0.9`。经实测，`langchain 1.2.10` 的上界是 `<1.1.0`，因此以 **1.0.9** 为准，§2.1 表格的写法有误。
- **`DASHSCOPE_API_KEY` 回退**：`.env` 里 `OPENAI_API_KEY` 为占位符时，`config.py` 自动回退读系统环境变量 `DASHSCOPE_API_KEY`，避免在文件里落盘明文密钥。
- **Windows 控制台编码**：中文 Windows 控制台代码页是 GBK，输出 `•`（U+2022）等字符直接
  `UnicodeEncodeError`。`cli.py` 在导入 rich 之前把控制台代码页与 Python 的 stdin/stdout/stderr
  统一切到 UTF-8 解决；自测脚本因不做引导，只用 GBK 安全字符。
- **`Live` 面板会重复打印**：`rich.Live.stop()` 在真机上会把最后一帧留在屏幕上，所以
  `stop()` 之后不能再 `console.print` 同一个面板，否则用户看到两遍。`cli.py` 用
  `if live is None` 把「流式渲染」与「一次性打印」两条路径分开，互不重叠。
- **`lark_oapi` 在导入时就抓走了事件循环**：启动飞书长连接时报
  `RuntimeError: This event loop is already running`。根因是 `lark_oapi/__init__.py` 第一行就是
  `from . import ws`，而 `ws/client.py` 在**导入那一刻**执行 `asyncio.get_event_loop()` 把当前循环
  存进模块级变量，`start()` 再用它 `run_until_complete(_connect())`。主线程先 `import lark_oapi`
  时抓到的是 `agent_loop`，于是 WS 线程去跑一个主线程正在 `run_forever` 的循环。
  解法：**lark 的一切导入都放进 WS 线程内**，并先给该线程 `asyncio.set_event_loop(new_event_loop())`
  再导入（见 `_run_ws_client`）。
- **飞书卡片渲染不出 Markdown**：卡片 JSON 1.0 的 `lark_md` 只支持行内语法，标题会显示成 `## xxx`、
  表格会显示成一堆竖线。改成卡片 JSON 2.0（`schema: "2.0"` + `markdown` 组件）后，
  除 HTMLBlock 外的标准 Markdown 都能渲染，原先那套「表格降级成列表」的转换代码整块删掉了。
  两个注意点：2.0 只支持共享卡片（`config.update_multi` 只能为 `true`），而只有共享卡片才允许被
  PATCH 更新，正好是我们要的；另外单个 `markdown` 组件里的换行是**软换行**，渲染时可能被忽略，
  要保证一行一条就得拆成多个组件。
- **卡片 JSON 无法离线校验**：想用 `cardkit.v1.card.create`（只建卡片实体、不发消息）校验卡片结构，
  但应用未开通 `cardkit:card:write` 权限，返回 `99991672`。目前只能靠真实消息验证，
  卡片结构出错时飞书 API 会在日志里回 `code=...`。
- **一次提问 431 秒，锅不在模型**：起初怀疑 qwen-plus 慢，逐次给工具调用计时后才发现时间全花在
  等 MCP Server 上——Server 掐断连接后不回任何数据，客户端也没有超时，就这么一直挂着。
  `K8S_MCP_TIMEOUT` 只管**工具发现**阶段，不管单次工具调用，这是两码事。已加
  `TOOL_CALL_TIMEOUT`（默认 30s）罩住每次调用。
- **"参数写错但服务端不报错、只返回空"是最危险的坑**：`namespace="all"` 不会被 Server 拒绝，
  它只是查不到东西、返回 `[]`。Agent 拿到空结果后不会说"我参数写错了"，而是开始编故事
  （怀疑权限、怀疑 etcd、怀疑静态 Pod），越查越偏。这类坑必须**提示词 + 客户端**双管：
  提示词告诉模型正确写法，客户端 `_fix_namespace_args()` 再兜一层，兜住时打 `[WARN]` 留痕。
- **`node analyze` 恒返回空**：PDF §1.4 的工具清单里它排在最前，直觉上该用它列节点，
  实测本机 Server 上 `node analyze`/`pod analyze` 返回 `[]`、`deployment analyze`/`service analyze`
  返回 `null`。别在它身上浪费时间，列资源统一用 `resource list`。
- **多轮变慢的真凶是历史里的工具原文**：第一轮不慢、第二轮突然慢一倍以上时，别先怀疑模型
  或服务端——先看历史里躺了多大的 ToolMessage。 LangChain 自带的 `ClearToolUsesEdit`
  本来是对症的工具，但它的 `keep` 语义是「保留最近 N 条工具结果」：第二轮时第一轮那份
  143KB 恰好是「最近的一条」，于是原封不动留下，等于没清。所以最终自己写了轮次感知的
  `MemoryWindowMiddleware`：旧轮全清、当前轮保留，正好绕开这个语义错位。
- **`resource list` 的体积不是被某一个"垃圾字段"吃掉的**：直觉上以为 `managedFields`、
  `annotations` 是大头，逐字段量化后（135.2KB 的列 Pod 结果）发现 `managedFields` 服务端早
  就剥掉了、`annotations` 只占 4.5KB，体积其实平摊在 `spec`(31KB)/`status`(31KB)/`metadata`(16KB)。
  所以"上黑名单删几个键"这种改法几乎不省。真正能删的是运维问答用不上的字段：
  `volumes`+`projected`(14.8KB)、`tolerations`(4.9KB)、`volumeMounts`(2.5KB)、`imageID`(2.4KB)，
  Node 侧则集中在一项——`status.images` 独占 12.9KB（Node 总共才 20.3KB）。
  **结论：改投影之前先把字段体积排个序，别照抄"K8s YAML 噪音字段"的经验清单。**
- **流式输出卡顿是渲染层的锅，但主犯不是"节流间隔"而是"在流里 await 网络"**：卡片 PATCH
  写在 `async for event in bot.astream(...)` 内部，每次往返 0.2~0.5s 就把 token 循环挂起这么久，
  流被打成"冻结 → 蹦一块"。改法是把刷新搬进后台协程（`CardReply._patcher`），token 侧只做内存累加。
  顺带一个反直觉的点：**单独把 `PATCH_INTERVAL` 从 1.2s 调小会更糟**——PATCH 次数 ≈ 流式时长 ÷ 间隔，
  调小等于把"挂起等网络"的总时长同比例放大。必须先把 await 挪出循环，再谈间隔。
- **"某个函数第一次调用特别慢"先怀疑冷导入，别急着改代码**：测刷新节奏时发现第一次
  `_patch_card` 花了 **8.17s**，而后续只要 0.25s。逐层计时后确认 8s 全在 `import lark_oapi`
  本身（它的 `__init__` 会拉整棵 API 树），里面的 model 子模块导入接近 0。真实机器人启动时
  WS 线程早就 import 过了，所以这笔开销线上根本不存在——是**测试没复现启动路径**造成的假象。
  另外这里不能"顺手把导入提到模块顶部"：见上文「`lark_oapi` 在导入时就抓走了事件循环」，
  提上去会让主线程抢到 `agent_loop`，WS 线程随后就报 `event loop is already running`。
- **飞书原生流式卡片有四条"看文档看不出来"的约定**，都是探针实测出来的：
  ① `content` 要传**累计全量文本**而不是增量（增量由飞书算）；② `sequence` 在整张卡生命周期内
  严格递增，且**内容更新与收尾的设置更新共用同一个计数器**（分开数会被判乱序）；
  ③ 结束必须再调 `card.settings` 传 `{"streaming_mode": false}`，不调的话打字光标一直转；
  ④ 用 `card_id` 发消息只有 `msg_type="interactive"` + `content={"type":"card","data":{"card_id":...}}`
  这一种写法通过，另外两种直接失败。还有权限门槛：建卡片实体要 **`cardkit:card:write`**，
  缺它报 `99991672`，而且开通后**要发布新版本**才生效。
- **外部 API 的调用形状有多种可能时，先写最小探针脚本，别直接改主逻辑**：上面那四条是一次探针
  跑出来的（`card.create` → `message.reply` → 循环 `card_element.content` → `card.settings`），
  10 分钟内就能把"猜错就整条链路挂"的部分全部排掉。反过来，如果直接上手改，故障会长成
  "机器人一条消息都不回"——那种排查面大得多。
- **包装层里"读工具返回值"要先搞清 `response_format`，否则会静默失效**：结果瘦身写完在真机
  一测，耗时完全没变、`[TRIM]` 日志一条都没打——`_result_text()` 每次都取到空串、每次都提前
  原样返回。根因是这套 MCP 工具是 `response_format="content_and_artifact"`，**我们的包装层
  拿到的是 `(内容块列表, artifact)` 二元组**，而 `ainvoke` 返回给外面的是已经转换好的内容块列表；
  只按"字符串或块列表"判断的取文本逻辑，正好在包装层这一侧永远不成立。
  定位手法值得记：分别 `await t.coroutine(...)` 与 `await t.ainvoke(...)`，把两者的
  `type` / 长度 / 键名打印出来对比——形状差异一眼就暴露了。
  教训：**改了返回值处理就必须验证"它真的跑了"**（打一条日志或断点），不能只看端到端耗时没变
  就当"效果不明显"；否则会把 bug 当成结论。
