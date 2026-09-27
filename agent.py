"""
Agent 核心模块：接载 MCP 工具、组装 Agent、提供带记忆的流式对话。

对应 PDF §3.2 中 agent.py 的职责：初始化 MCP 客户端、加载工具、创建 Agent。

对外主入口是 K8sAgent（CLI 与飞书机器人共用）：
    bot = await K8sAgent.create()
    async for event in bot.astream("查看集群有哪些 node 节点"):
        ...
记忆由 LangGraph 的 checkpointer 承担，同一 thread_id 的多轮对话自动带上下文。

工具来源有两处：
  1. K8s 工具 —— 由虚拟机上部署的 mcp-k8s-eye（MCP Server）动态提供，共 19 个；
  2. 搜索工具 —— 本地封装（tools/search.py），共 1 个。

两处工具名统一处理后再交给 LLM：
  - 真实 MCP 工具名含空格（"resource list"），而 LLM 的函数调用规范只接受
    字母/数字/下划线/连字符，所以暴露给 LLM 时换成下划线（resource_list）；
    调用 Server 时适配器内部仍用原始名，不受影响（见 langchain_mcp_adapters/tools.py）。
  - 原始名保存在 tool.metadata["mcp_name"]，供 CLI 按 PDF 的样式展示。

虚拟机没开机时，MCP Server 不可达。此时若 ALLOW_MCP_FALLBACK=true 就回退到
tools/mock_k8s.py 的离线工具，保证 Agent 主逻辑仍可开发和演示。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from config import settings
from prompts import SYSTEM_PROMPT
from tools.mock_k8s import load_mock_tools
from tools.search import load_search_tools

# MCP Server 在客户端里的标识名，与用户提供的 mcpServers 配置保持一致
MCP_SERVER_NAME = "k8s-eye"

# 默认会话 ID。多轮对话靠它区分上下文：同一个 thread_id 才能记住历史
DEFAULT_THREAD_ID = "cli"


def _sanitize_tool_name(raw_name: str) -> str:
    """把 MCP 工具名转成 LLM 能接受的标识符：空格与非法字符统一换成下划线。"""
    return re.sub(r"[^0-9a-zA-Z_-]", "_", raw_name)


def _prepare_tools(raw_tools: list[BaseTool]) -> list[BaseTool]:
    """净化工具名，并把原始名记进 metadata 供展示。"""
    for tool in raw_tools:
        original = tool.metadata.get("mcp_name") if tool.metadata else None
        if original is None:
            tool.metadata = {**(tool.metadata or {}), "mcp_name": tool.name}
        tool.name = _sanitize_tool_name(tool.name)
    return raw_tools


def _build_connection() -> dict:
    """按配置拼出 MultiServerMCPClient 需要的连接描述。"""
    connection: dict = {
        "url": settings.mcp_url,
        "transport": settings.mcp_transport,
    }
    if settings.mcp_transport == "sse":
        # SSEConnection.timeout 单位是秒
        connection["timeout"] = settings.mcp_timeout
    else:
        # StreamableHttpConnection.timeout 要求 timedelta
        from datetime import timedelta

        connection["timeout"] = timedelta(seconds=settings.mcp_timeout)
    return connection


async def load_mcp_tools() -> tuple[list[BaseTool], str]:
    """
    加载 K8s 工具。

    返回 (工具列表, 来源)。来源取值为 "mcp"（真实 Server）或 "mock"（离线回退）。
    连不上且 ALLOW_MCP_FALLBACK=false 时直接抛错，避免误以为在查真实集群。
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connection = _build_connection()
    client = MultiServerMCPClient({MCP_SERVER_NAME: connection})

    try:
        # 额外包一层超时：虚拟机没开机时 TCP 连接会挂很久，这里快速失败
        raw_tools = await asyncio.wait_for(
            client.get_tools(server_name=MCP_SERVER_NAME),
            timeout=settings.mcp_timeout + 5,
        )
        if not raw_tools:
            raise RuntimeError("MCP Server 连接成功但没有返回任何工具")
        return _prepare_tools(raw_tools), "mcp"

    except Exception as exc:  # noqa: BLE001 - 连接失败原因多样，统一走回退判断
        if not settings.allow_mcp_fallback:
            raise RuntimeError(
                f"连接 K8s MCP Server 失败（{settings.mcp_url}）：{type(exc).__name__}: {exc}。"
                "请确认虚拟机已开机、mcp-k8s-eye 在 8080 端口运行；"
                "或临时把 .env 里 ALLOW_MCP_FALLBACK 改为 true 使用离线 mock 工具。"
            ) from exc

        print(f"  [WARN] K8s MCP Server 不可达（{type(exc).__name__}），已回退到离线 mock 工具")
        # mock 工具同样过一遍净化，保证两条路径产出的工具名与元数据一致
        return _prepare_tools(load_mock_tools()), "mock"


async def load_all_tools() -> tuple[list[BaseTool], str]:
    """合并 K8s 工具与搜索工具，返回 (全部工具, K8s 工具来源)。

    搜索工具构造失败（如没填 TAVILY_API_KEY）不算致命：给出提醒后继续，
    这样 K8s 查询功能仍可使用，不会被一个外部服务卡住整个 Agent。
    """
    k8s_tools, source = await load_mcp_tools()

    try:
        search_tools = load_search_tools()
    except Exception as exc:  # noqa: BLE001 - 搜索服务不可用时降级而非中断
        print(f"  [WARN] 搜索工具加载失败：{exc}")
        search_tools = []

    return [*k8s_tools, *search_tools], source


def build_llm() -> BaseChatModel:
    """按配置初始化 LLM（qwen-plus 走 DashScope 的 OpenAI 兼容接口）。"""
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        model=settings.llm_model,
        model_provider=settings.llm_provider,
        api_key=settings.api_key,
        base_url=settings.api_base,
    )


def build_agent(
    model: BaseChatModel | None = None,
    tools: list[BaseTool] | None = None,
    checkpointer: InMemorySaver | None = None,
):
    """组装 ReAct Agent：LangChain 的 create_agent 底层就是 LangGraph。

    传入 checkpointer 后 Agent 就带记忆能力：同一 thread_id 的多轮对话会自动
    带上历史消息，无需手动拼接 messages。
    """
    return create_agent(
        model=model or build_llm(),
        tools=tools if tools is not None else [],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer or InMemorySaver(),
    )


# ---------------------------------------------------------------------------
# 流式事件：把 LangGraph 的原始流翻译成入口层（CLI / 飞书）好渲染的事件
# ---------------------------------------------------------------------------

EventKind = Literal["token", "tool_start", "tool_end", "done"]


@dataclass
class AgentEvent:
    """一次流式输出中的单个事件。"""

    kind: EventKind
    text: str = ""                      # token 事件：本次增量文本
    tool_name: str = ""                 # 工具事件：展示用工具名（含空格的原名）
    tool_args: dict = field(default_factory=dict)
    ok: bool = True                     # tool_end 事件：工具是否正常返回


def _content_to_text(content: object) -> str:
    """把消息内容统一成文本。内容可能是 str，也可能是文本块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


class K8sAgent:
    """
    Agent 运行时封装。

    CLI 与飞书入口都通过它对话，好处是两处共用同一份工具、记忆与会话历史逻辑。
    生命周期：先 await K8sAgent.create() 初始化（会连 MCP Server），再反复
    await astream(...) 多轮对话。
    """

    def __init__(self, graph, tools: list[BaseTool], k8s_source: str, checkpointer: InMemorySaver):
        self.graph = graph
        self.tools = tools
        self.k8s_source = k8s_source
        self._checkpointer = checkpointer
        # 净化名 -> 原始展示名，CLI 展示工具调用时用原名（如 resource_list -> resource list）
        self.display_names = {
            t.name: (t.metadata or {}).get("mcp_name", t.name) for t in tools
        }

    @classmethod
    async def create(cls, model: BaseChatModel | None = None) -> K8sAgent:
        """初始化 Agent：加载工具 -> 建 LLM -> 组装带记忆的 Agent。"""
        tools, k8s_source = await load_all_tools()
        checkpointer = InMemorySaver()
        graph = build_agent(model=model, tools=tools, checkpointer=checkpointer)
        return cls(graph=graph, tools=tools, k8s_source=k8s_source, checkpointer=checkpointer)

    def tool_names(self) -> list[str]:
        """按原始名返回工具清单，供 CLI 的 tools 命令展示。"""
        k8s = [self.display_names[t.name] for t in self.tools if t.name != "web_search"]
        names = sorted(k8s)
        if any(t.name == "web_search" for t in self.tools):
            names.append("web_search")
        return names

    def reset_memory(self, thread_id: str = DEFAULT_THREAD_ID) -> None:
        """清空某个会话的历史（CLI 的 clear 命令）。"""
        self._checkpointer.delete_thread(thread_id)

    async def astream(
        self,
        user_input: str,
        thread_id: str = DEFAULT_THREAD_ID,
    ) -> AsyncIterator[AgentEvent]:
        """
        流式执行一轮对话，逐个吐出事件。

        同时订阅两种流：
          - messages：逐 token 的增量文本，用于实时打印回答；
          - updates：节点级结果，用于捕获完整的工具调用（名称、参数）与工具返回。
        """
        config = {"configurable": {"thread_id": thread_id}}

        async for mode, chunk in self.graph.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                message, meta = chunk
                if meta.get("langgraph_node") != "model" or not isinstance(message, AIMessageChunk):
                    continue
                text = _content_to_text(message.content)
                if text:
                    yield AgentEvent(kind="token", text=text)
                continue

            # mode == "updates"：chunk 形如 {"model": {"messages": [...]}}
            for node, update in (chunk or {}).items():
                messages = (update or {}).get("messages") if isinstance(update, dict) else None
                if not messages:
                    continue

                if node == "model":
                    for message in messages:
                        for call in getattr(message, "tool_calls", None) or []:
                            raw_name = call.get("name", "")
                            yield AgentEvent(
                                kind="tool_start",
                                tool_name=self.display_names.get(raw_name, raw_name),
                                tool_args=call.get("args") or {},
                            )
                elif node == "tools":
                    for message in messages:
                        raw_name = getattr(message, "name", "") or ""
                        status = getattr(message, "status", "success")
                        yield AgentEvent(
                            kind="tool_end",
                            tool_name=self.display_names.get(raw_name, raw_name),
                            ok=status != "error",
                        )

        yield AgentEvent(kind="done")


if __name__ == "__main__":
    # 自测：python -m agent —— 跑两轮真实对话，验证工具调用、流式输出与多轮记忆
    from rich.console import Console

    console = Console()

    async def _main() -> None:
        console.print("[bold]正在初始化 Agent...[/bold]")
        bot = await K8sAgent.create()
        console.print(f"[bold]K8s 工具来源:[/bold] {bot.k8s_source} | "
                      f"[bold]工具总数:[/bold] {len(bot.tools)}")

        for question in ("查看集群有哪些 node 节点", "刚才那个集群只有几个节点？"):
            console.print(f"\n[bold cyan]You:[/bold cyan] {question}")
            buffer = ""
            async for event in bot.astream(question):
                if event.kind == "token":
                    buffer += event.text
                elif event.kind == "tool_start":
                    console.print(f"  [yellow]调用工具:[/yellow] {event.tool_name} {event.tool_args}")
                elif event.kind == "tool_end":
                    console.print(f"  [green]工具返回完成:[/green] {event.tool_name}")
            console.print(f"[bold]Agent:[/bold] {buffer[:300]}{'...' if len(buffer) > 300 else ''}")

    asyncio.run(_main())
