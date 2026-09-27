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
```

配置要点：LLM 用 `qwen-plus`（走 DashScope 兼容接口），搜索用 `tavily`（需 `TAVILY_API_KEY`，
本机网络下 DuckDuckGo 不可达，见下方踩坑记录）。

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
| 1 | `config.py` + `prompts.py` | 待开始 |
| 2 | `tools/search.py` | 待开始 |
| 3 | MCP 客户端接载与工具发现（含 mock 回退） | 待开始 |
| 4 | `agent.py` Agent 组装 + 记忆 + 流式 | 待开始 |
| 5 | `cli.py` 终端界面 | 待开始 |
| 6 | `feishu/feishu_bot.py` 飞书入口 | 待开始 |
| 7 | 虚拟机开机后真机联调 | 待开始 |

## 与需求文档的差异（实现时的依据）

1. **MCP 传输方式**：PDF §2.3 称使用 `streamable_http`，但 `mcp-k8s-eye` 实际以 **SSE** 提供服务（endpoint 以 `/sse` 结尾）。已按 SSE 实现，并把传输方式做成 `K8S_MCP_TRANSPORT` 配置项，切换时不改代码。
2. **参考代码缺失**：PDF §5「完整代码实现」只有一个 `k8s-ops-agent-cli.zip` 附件链接，PDF 内无可提取代码，全部代码从零实现。
3. **Python 版本**：PDF 用 3.11.3，本机 venv 为 3.12.8，上述依赖版本均兼容。
4. **飞书连接方式**：飞书事件订阅默认要求公网回调地址，本地开发不便。改用 `lark-oapi` 的 **WebSocket 长连接**模式，无需公网 IP 与内网穿透。

## 安全提示

`mcp-k8s-eye` 暴露的 19 个工具中包含**写操作**：`resource delete`、`resource create or update`、`deployment scale`、`pod exec`。本项目按需求方决策**不做工具白名单、不做二次确认**，全部交给 LLM 自主调用。

因此：**不要将本 Agent 连接到生产集群。** 首次使用时先用只读类问题验证行为，再考虑放开写操作。

## 踩坑记录

- **`requirements.txt` 中文注释导致 pip 失败**：中文 Windows 下 pip 用系统编码（cp936/GBK）读取该文件，含中文注释会抛 `UnicodeDecodeError`。已约定此文件只用 ASCII 注释，中文说明放本文档。Python 源文件不受影响（默认 UTF-8）。
- **`mcp` 必须锁 1.x**：`langchain-mcp-adapters 0.2.1` 只声明 `mcp>=1.9.2` 没有上界，pip 会装 `mcp 2.x`，而 2.x 删掉了 `mcp.shared.context.RequestContext`，导致 `from langchain_mcp_adapters.client import MultiServerMCPClient` 直接 ImportError。已在 requirements 中加 `mcp<2`。
- **`langgraph-prebuilt` 必须锁 1.0.8**：`langchain 1.2.10` 限制 `langgraph<1.1.0`，而 `langgraph-prebuilt` 从 1.0.9 起调用 `langgraph.runtime.ExecutionInfo`（langgraph 1.1 才引入），两者范围重叠但 API 不兼容，pip 默认装 1.0.13 会让 `from langchain.agents import create_agent` 报错。已锁 1.0.8。
- **DuckDuckGo 在本机网络完全不可达**：实测其后端 `search.yahoo.com` 连接被拒绝（`os error 10061`），
  而 `api.tavily.com` 返回 200。因此 `SEARCH_PROVIDER` 默认改为 **tavily**，需要去 tavily.com 申请
  API Key（有免费额度）。`duckduckgo` 分支代码保留且可正常构造工具，换到可访问的网络环境即可用。
- **`duckduckgo-search` 包名已失效**：`langchain-community 0.4.2` 的 DuckDuckGo 封装改成了
  `import ddgs`，装旧的 `duckduckgo-search` 会抛 `Could not import ddgs python package`。
  依赖里已换成 `ddgs`。
- **PDF 自身的版本矛盾**：§2.1 技术栈表写 `langgraph 1.1.x`，§4.2 具体版本写 `1.0.9`。经实测，`langchain 1.2.10` 的上界是 `<1.1.0`，因此以 **1.0.9** 为准，§2.1 表格的写法有误。
- **`DASHSCOPE_API_KEY` 回退**：`.env` 里 `OPENAI_API_KEY` 为占位符时，`config.py` 自动回退读系统环境变量 `DASHSCOPE_API_KEY`，避免在文件里落盘明文密钥。
