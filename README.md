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

用**长连接**而不是 Webhook 的原因：事件订阅默认要求一个公网可回调的地址，长连接由客户端主动连飞书，
本地开发不需要公网 IP 和内网穿透（见下方「与需求文档的差异」第 4 条）。

### 一条消息答一轮：卡片 PATCH 刷新

一轮问答只占用**一条**飞书消息，不会刷屏：

```
用户发问
  └─ reply 一张「正在分析…」卡片  ──> 拿到 message_id
         └─ 之后工具轨迹 / 流式回答都用 PATCH 刷新这条消息（节流 1.2s）
                └─ 结束后再 PATCH 一次最终卡片（副标题变成「完成 · N 步工具调用 · X.Xs」）
```

卡片用**卡片 JSON 2.0**结构，正文是 `markdown` 组件，支持标题、表格、代码块、引用、分割线，
所以 LLM 的回答**原样贴进去**就能正常渲染。工具轨迹放进 `collapsible_panel`，默认收起。
副标题显示状态（`正在分析…` / `完成 · 3 步工具调用 · 12.4s` / `出错了`），失败时标题栏转红。

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

效果（同一句提问"查看集群有哪些节点和正在运行的 pods"）：

| | 加固前 | 加固后 |
|---|---|---|
| 总耗时 | 431.4s | 36.2s |
| 工具调用次数 | 7 次（前 4 次全空） | 2 次（各命中一次） |
| 实际入参 | `namespace: "all"` | `namespace: ""` |
| 结论 | pods 为空（错） | 3 节点 / 23 个 pods（对） |

## 对话记忆治理（轮数窗口 + 旧工具结果清理）

checkpointer 会把每轮 **ToolMessage 的原文**一起存档。`resource list` 拉一次全量 pods 就是
143KB YAML，从第二轮起每次模型调用都要把这段旧 YAML 重新预填一遍：同一句小问题实测
**空历史 12.9s、带历史 37.1s**（群里那轮更慢到 105.8s，因为它工具调用更多、回答更长，
而一轮里有几次工具调用就要做几次模型 pass，每 pass 都重读一遍历史）。

`agent.py` 的 `MemoryWindowMiddleware` 两道闸门：

| 钩子 | 作用 | 是否写存档 |
|---|---|---|
| `before_model` | 轮数窗口：历史超过 `MEMORY_WINDOW_ROUNDS` 轮（默认 10）时，用 `RemoveMessage` 整轮删除最老的轮次 | 是（历史不再无限增长） |
| `wrap_model_call` | 体积清理：把「已结束旧轮」的 ToolMessage 原文换成占位符，当前轮原样保留 | 否（只影响本次调用） |

两个设计细节：

- 删除永远对齐轮次边界（以 HumanMessage 为起点），不会只删 AIMessage 而留下它对应的
  ToolMessage，模型不会看到「有工具结果却没有工具调用」的残缺历史。
- 被清理的只是「原始证据」，上一轮的**结论**仍留在该轮最终回答（AIMessage）里，模型照常
  可以引用；真需要细节时占位符会提醒它重新调工具，而不是凭记忆猜。

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
