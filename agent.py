"""
Agent 核心模块：接载 MCP 工具、组装 Agent。

对应 PDF §3.2 中 agent.py 的职责：初始化 MCP 客户端、加载工具、创建 Agent。

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

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from config import settings
from prompts import SYSTEM_PROMPT
from tools.mock_k8s import load_mock_tools
from tools.search import load_search_tools

# MCP Server 在客户端里的标识名，与用户提供的 mcpServers 配置保持一致
MCP_SERVER_NAME = "k8s-eye"


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


def build_agent(model: BaseChatModel | None = None, tools: list[BaseTool] | None = None):
    """组装 ReAct Agent：LangChain 的 create_agent 底层就是 LangGraph。"""
    return create_agent(
        model=model or build_llm(),
        tools=tools if tools is not None else [],
        system_prompt=SYSTEM_PROMPT,
    )


if __name__ == "__main__":
    # 自测：python -m agent —— 只验证工具接载，不调用 LLM（阶段4 再加 Agent 对话）
    from rich.console import Console

    console = Console()

    async def _main() -> None:
        tools, source = await load_all_tools()
        k8s_names = [t.name for t in tools if t.name != "web_search"]

        console.print(f"[bold]K8s 工具来源:[/bold] {source}")
        console.print(f"[bold]K8s 工具数:[/bold] {len(k8s_names)}")
        console.print(f"[bold]K8s 工具:[/bold] {sorted(k8s_names)}")
        console.print(f"[bold]搜索工具:[/bold] {[t.name for t in tools if t.name == 'web_search']}")
        console.print(f"[bold]合计:[/bold] {len(tools)} 个工具")

    asyncio.run(_main())
