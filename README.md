# K8s 运维助手 Agent

![CI](https://github.com/IceSaury/k8s-Agent/actions/workflows/ci.yml/badge.svg)

面向 SRE 日常运维的 AI Agent：用自然语言问集群状态，模型自己决定调哪个工具、调几次，最后给出结论。
K8s 侧的能力不是我自己写的，而是通过 **MCP 协议**接一个现成的 K8s MCP Server（19 个工具），官方文档
与排障资料由互联网搜索工具补。交付两个入口：终端 CLI 与飞书机器人。

**技术栈**：Python 3.12 · LangChain 1.2 · LangGraph 1.0 · MCP（Model Context Protocol） · Qwen（DashScope 兼容接口） · 飞书开放平台

## 特性

- **多轮对话**：checkpointer 按会话保存历史，不同群、不同用户之间互不干扰
- **工具动态发现**：启动时从 MCP Server 拉取全部工具，Server 侧加工具不需要改这里的代码
- **过程可见**：每次工具调用的名称、参数、耗时、成败都渲染进回复，结论能对回具体证据
- **流式输出**：CLI 实时刷新 Markdown 面板；飞书一轮问答只占一条卡片消息
- **上下文压缩**：轮次感知的中间件 + 工具结果投影，多轮对话不再随历史增长而变慢
- **入口与核心解耦**：CLI 与飞书共用同一套 Agent，新增入口不影响主流程
- **离线可开发**：MCP Server 不可达时回退到内置 mock 工具，主逻辑照样能跑通

## 效果展示

```
$ python cli.py

You: 查看集群有哪些 node 节点
  🔧 调用工具: resource list  {"kind": "Node", "namespace": ""}
     ✅ 工具返回完成  (0.8s)
╭──────────────── Agent ────────────────╮
│ 集群共 3 个节点，全部 Ready            │
│  NAME       ROLE         VERSION      │
│  master01   control-…    v1.29.2      │
│  node01     worker       v1.29.2      │
│  node02     worker       v1.29.2      │
╰───────────────────────────────────────╯
```

一句"查看集群有哪些节点和正在运行的 pods"的真实数据：2 次工具调用、约 24 秒返回、结论为 3 个节点
与 23 个运行中 Pod（详见「设计与实现要点」的实测表）。

## 架构

四层，自上而下：

```
交互入口层     cli.py（终端）        feishu/feishu_bot.py（飞书）
                    \                    /
Agent 核心层         agent.py
                 意图识别 · 工具路由 · 对话记忆
                 LangChain create_agent（底层 LangGraph）
                    /                    \
工具层        MCP 工具（K8s）    tools/search.py（互联网搜索）
                    \                    /
外部依赖层     mcp-k8s-eye (SSE)   DashScope Qwen   飞书开放平台
```

层间单向依赖：`agent.py` 不引用任何入口，只对外暴露 `astream()`，吐统一的事件
（`token` / `tool_start` / `tool_end` / `done`）；入口只负责"怎么收问题、怎么渲染事件"。

| 模块 | 职责 |
|---|---|
| `config.py` | 读取 `.env`，集中管理 LLM / MCP / 搜索 / 飞书配置，所有魔法值只在这里出现一次 |
| `prompts.py` | 系统提示词，只放**环境无关**的方法论与表达规范 |
| `agent.py` | MCP 客户端接载、工具净化与加固、上下文压缩中间件、入参兜底、流式事件翻译 |
| `cli.py` | 终端交互循环、内置命令、工具过程可视化 |
| `feishu/feishu_bot.py` | 飞书长连接、事件去重、流式卡片回复 |
| `tools/search.py` | 搜索工具封装，`duckduckgo` 与 `tavily` 可切换 |
| `tools/mock_k8s.py` | 离线 mock 工具，Server 不可达时保证主逻辑可调试 |
| `console_boot.py` | Windows 控制台 UTF-8 引导，两个入口共用 |
| `requirements-dev.txt` | CI 用的开发工具（只有 ruff），运行 Agent 不需要它 |
| `.github/workflows/ci.yml` | GitHub Actions 流水线，见「质量检查」 |
| `docs/` | 实现细节与实测记录 |

## 快速开始

前置条件：Python 3.12，一个可访问的 K8s MCP Server（本项目用 `mcp-k8s-eye`，SSE 传输）。

```bash
# 1. 建虚拟环境并安装依赖
python -m venv .venv
source .venv/Scripts/activate        # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt

# 2. 准备配置
cp .env.example .env                 # 然后填写 LLM Key 与 MCP Server 地址

# 3. 启动
python cli.py                        # 终端入口
python -m feishu.feishu_bot          # 飞书机器人（需先配好应用凭证，见「飞书入口」）
```

`config.py` 优先读 `.env` 里的 `OPENAI_API_KEY`；如果它仍是占位符，则回退读系统环境变量
`DASHSCOPE_API_KEY`，密钥因此不必落盘到文件。

## 配置项

| 变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_MODEL` | `qwen-plus` | 模型名，走 DashScope 兼容接口 |
| `K8S_MCP_URL` | — | MCP Server 地址，SSE 传输必须以 `/sse` 结尾 |
| `K8S_MCP_TRANSPORT` | `sse` | `sse` \| `streamable_http`，切换不需要改代码 |
| `K8S_MCP_TIMEOUT` | `10` | **工具发现**阶段超时（秒），Server 没开机时快速失败 |
| `TOOL_CALL_TIMEOUT` | `30` | **单次工具调用**超时（秒），防止个别入参把整轮对话挂死 |
| `TRIM_TOOL_PAYLOAD` | `true` | 是否把 `resource list` 的返回投影成运维视图 |
| `MEMORY_WINDOW_ROUNDS` | `10` | 记忆窗口（轮），超出的最老轮次整轮删除 |
| `SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`（免 Key，需代理）\| `tavily`（需 Key，可直连） |
| `SEARCH_PROXY` | 空 | 搜索专用代理地址，只作用于搜索，不影响 LLM 调用 |
| `ALLOW_MCP_FALLBACK` | `true` | Server 不可达时是否回退 mock 工具；真机联调建议设 `false` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 占位符 | 飞书自建应用凭证 |
| `FEISHU_CONNECTION_MODE` | `ws` | `ws` 长连接（本地开发无需公网）\| `webhook` |

## 使用

### CLI

启动后直接提问，三个内置命令：

| 命令 | 作用 |
|---|---|
| `quit` / `exit` / `q` | 退出（`Ctrl+C`、`Ctrl+D` 同效） |
| `clear` | 清空当前会话历史，只清消息，不重载工具 |
| `tools` | 列出全部工具及其来源（MCP / 本地搜索） |

回答用 `rich.Live` + `Panel(Markdown(...))` 流式刷新，刷新节奏由 `_RENDER_INTERVAL`（120ms）控制；
非终端环境（重定向到文件、管道输入）自动关掉 Live，改为结束后一次性打印，避免整屏重画把日志刷爆。
Windows 控制台默认 GBK，所以入口脚本必须先调 `console_boot.bootstrap_console()` 再导入 rich。

### 飞书入口

`.env` 里填好 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，并在开发者后台完成四步：

1. 事件订阅 → 订阅方式选 **「长连接」**
2. 订阅事件 **「接收消息 `im.message.receive_v1`」**
3. 开通权限 `im:message`、`im:message:send_as_bot`
4. 开通权限 **`cardkit:card:write`**（创建卡片实体，流式更新的前提），改完要**发布新版本**才生效

群聊里只有被 @ 到才回应，私聊直接回应；发 `clear` / `重置` 清空当前群的历史。
缺第 4 条权限的症状是建卡片实体返回 `99991672`，机器人一条消息都不回。

## 设计与实现要点

### 工具链路加固

`mcp-k8s-eye` 有两处接口与 kubectl 直觉不符，都会让 Agent 得出错误结论，因此在客户端做了三道闸门：

| 现象 | 根因 | 对策 |
|---|---|---|
| 报告"集群里没有 Pod" | 模型按习惯传 `namespace="all"`，Server 把它当**字面命名空间名**，返回空列表 | `_fix_namespace_args()` 把 `all` / `*` / `-a` 一律改写成 `""`（Server 用空串表示全部），并打 `[WARN]` 留痕 |
| 整轮对话挂死几十秒 | `resource list` 缺 `namespace` 时 Server 直接掐断连接、不回任何字节 | 同样在兜底里补 `""`，并用 `TOOL_CALL_TIMEOUT` 罩住每次调用 |
| 拿到空结果后一路瞎猜 | 本机 Server 上 `* analyze` 系列恒返回 `[]` 或 `null` | 在该类工具的 `description` 里写明"返回空 ≠ 资源不存在" |

包装点选在 `StructuredTool.coroutine` 上，本地同步工具的 `coroutine is None`，自动跳过。超时抛出的错误
文本会被 `ToolNode` 当作**工具结果**回给模型，让它换工具或换参数重试，而不是让整轮对话一直挂着。

同一句提问的前后对照（真机）：

| | 加固前 | 加固后 |
|---|---|---|
| 总耗时 | 431.4s | **36.2s** |
| 工具调用次数 | 7 次（前 4 次全空） | **2 次** |
| 实际入参 | `namespace: "all"` | `namespace: ""` |
| 结论 | Pod 为空（错） | 3 节点 / 23 Pod（对） |

### 上下文压缩

checkpointer 会把每轮 **ToolMessage 的原文**一起存档。列一次全量 Pod 是 143KB YAML，从第二轮起每次模型
调用都要把它重新预填一遍——同一句小问题实测**空历史 12.9s、带历史 37.1s**。分三道闸门处理：

| 闸门 | 位置 | 作用 | 是否写回存档 |
|---|---|---|---|
| 轮次窗口 | `before_model` | 超过 `MEMORY_WINDOW_ROUNDS` 轮时，用 `RemoveMessage` 整轮删除最老的 | 是 |
| 旧结果降级 | `wrap_model_call` | 把已结束旧轮的 ToolMessage 换成占位说明，当前轮原样保留 | 否（只影响本次请求） |
| 当轮投影 | 工具包装层 | `resource list` 的原始 YAML 投影成运维视图后再交给模型 | 是（进存档的就是精简版） |

两个设计细节：删除永远对齐**轮次边界**（以 HumanMessage 为起点），不会留下"有工具结果却没有工具调用"
的残缺历史；被清理的只是原始证据，上一轮的**结论**仍留在该轮的 AIMessage 里，模型照常可以引用。

投影保留答题要用的字段（`phase` / `nodeName` / `restartCount` / 镜像 / 资源限制 / 异常 conditions 等），
丢掉探针、挂载、环境变量、`status.images` 这类推不出结论的内容，并在结果末尾附一句裁剪说明——少了这句
话，模型会把"看不到"当成"集群里没有"。

实测（同一对提问、各自全新会话，只切 `TRIM_TOOL_PAYLOAD`）：

| | 不瘦身 | 瘦身 |
|---|---|---|
| 列 Pod 的工具结果 | 135.2KB | **15.9KB（−88%）** |
| 列 Node 的工具结果 | 20.3KB | **4.4KB（−78%）** |
| turn1 端到端 | 35.6s | **23.8s** |
| turn2 端到端 | 26.2s | **19.0s** |

副作用是回答会变短（同一轮 5734 字 → 2039 字），因为原始 YAML 里那些字段本来就答不出结论，模型常把
它们摊成篇幅；节点数、Ready 状态、IP、重启次数这些事实两边都正确。

### 提示词与环境事实分层

系统提示词只留换个集群、换个模型仍然成立的方法论：先取证再结论、空结果先怀疑参数再怀疑集群、写操作
交代后果、回答篇幅规范。某个 Server 的接口事实（参数语义、已知故障）追加进对应工具的 `description`，
理由有三条：模型是在决定调用这个工具的那一刻才读 `description`；工具没加载时这些说明不该留在上下文里；
换 Server 后提示词里不会残留过期指令。

篇幅控制走的是"把啰嗦拆成可判定的形态"，而不是加一句"请简洁回答"：不写过程旁白、只报异常不解释正常、
清单表格不超过 5 列、不做无依据推测、下一步命令最多 2 条。同一句提问从 3746 字降到 1882 字（其中散文
部分 1727 → 311 字），表格里的信息一条没少。

### 双入口与并发模型

`lark-oapi` 的 `ws.Client.start()` 是「阻塞 + 自建事件循环」的，而 Agent 自己也需要一个事件循环，两者
不能共用，因此分两条线：

```
主线程                        lark-ws 线程（daemon）
agent_loop.run_forever()      ws_client.start()  —— 阻塞，负责协议与心跳
     ↑                                |
     |  run_coroutine_threadsafe       |  收到消息 -> 同步回调 on_message()
     +--------------------------------+
                 process_message(...)
```

事件回调**必须立刻返回**：它跑在 lark 自己的循环里，一旦在里面等 LLM（几十秒），心跳发不出去，连接会被
飞书断掉，所以回调只负责投递协程。会话按 `thread_id = f"feishu:{chat_id}"` 隔离，每条消息先经
`message_id` 去重（飞书会重投事件）。CLI 与飞书是两个独立进程，各自持有 Agent 与内存 checkpointer，互不
影响；真正共享的只有外部资源（LLM 并发配额、同一个 MCP Server 与同一个集群）。

### 流式卡片

一轮问答只占**一条**飞书消息，走官方「流式更新卡片」通道：建卡片实体拿 `card_id` → 用它回复消息 →
后台协程持续推**累计全量文本**（`sequence` 严格递增）→ 收尾推最终文本并关掉 `streaming_mode`。

推送刻意放在后台协程而不是 token 循环里：一次更新是实打实的 HTTPS 往返（约 0.5s），写在 token 循环里
会把流打断成"冻结一秒 → 蹦出一大块字"。真机一轮 46 秒推 33 帧、0 帧被拒，约 1.8 帧/秒。

这条通道的天花板不在推送侧：客户端打字机速率约 35 字/秒，而模型出字约 70 字/秒，卡片因此永远落在模型
后面。接口约定、探针数据和一次已回退的优化尝试，记录在
[docs/feishu-streaming-card.md](docs/feishu-streaming-card.md)。

## 质量检查

`.github/workflows/ci.yml` 在每次 push、PR 和手动触发时跑四步，全部**不需要任何密钥**，fork 之后照样能跑通：

| 步骤 | 命令 | 查什么 |
|---|---|---|
| 语法编译 | `python -m compileall -q` | 每个模块能解析 |
| 导入冒烟 | `python -c "import agent, cli, ..."` | 依赖版本没被 pip 解成不兼容组合（历史上 `mcp 2.x`、`langgraph-prebuilt 1.0.13` 都是导入即炸） |
| 静态检查 | `ruff check --select E9,F` | 语法错误、未定义名字这类错误级问题，不卡代码风格 |
| 依赖安装 | `pip install -r requirements.txt` | 锁版本清单在干净环境里装得上 |

两点说明：

- **这是 CI，不是 CD**。本项目没有部署目标（本地跑的 CLI 与飞书机器人，也不发布包），所以流水线里没有
  构建产物和发布步骤。
- **端到端不在 CI 里**。真机链路要连虚拟机上的 MCP Server、大模型 API 和飞书开放平台，公共 runner 上
  没有这些，相关验证记录在上面的实测表里。CI 只保证"改完还能装、能导入"。
- Actions 只在 GitHub 侧执行；仓库同时镜像到 Gitee 时，Gitee 不会跑这份 workflow。

## 开发过程

按阶段推进，每个阶段一次提交，git 历史即学习路径：仓库初始化与依赖 → 配置与提示词 → 搜索工具 →
MCP 接载与工具发现 → Agent 组装与流式输出 → CLI → 飞书入口 → 真机联调与加固。

## 安全提示

`mcp-k8s-eye` 暴露的 19 个工具里包含**写操作**：`resource delete`、`resource create or update`、
`deployment scale`、`pod exec`。本项目在实训范围内**不做工具白名单、不做二次确认**，全部交给模型自主
调用，仅靠提示词要求写操作前说明对象与后果。

因此：**不要将本 Agent 连接到生产集群。** 首次使用先问只读类问题验证行为，再考虑放开写操作。

## License

MIT，见 [LICENSE](LICENSE)。
