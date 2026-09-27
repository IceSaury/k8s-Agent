"""
搜索工具封装。

职责：把互联网搜索包装成 Agent 能调用的工具，屏蔽不同搜索服务商的差异。
PDF §2.1 要求 DuckDuckGo（免费）与 Tavily 两条路可切换，切换只改 .env 里的
SEARCH_PROVIDER，具体构造逻辑收在本模块，agent.py 不关心用的是哪家。

注意：本机网络实测 DuckDuckGo 不通（后端 search.yahoo.com 连接被拒绝），
所以 .env 默认用 tavily，需要 TAVILY_API_KEY。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from config import settings

# 对外统一的工具名。系统提示词里引用的是这个名字，改名要同步改 prompts.py
SEARCH_TOOL_NAME = "web_search"


def _build_tavily_tool() -> BaseTool:
    """构造 Tavily 搜索工具（付费服务，有免费额度，需 API Key）。"""
    if not settings.tavily_api_key or "REPLACE_ME" in settings.tavily_api_key:
        raise RuntimeError(
            "SEARCH_PROVIDER=tavily 但 TAVILY_API_KEY 未配置。"
            "请到 tavily.com 注册获取 Key 并填入 .env；"
            "或把 SEARCH_PROVIDER 改为 duckduckgo（注意本机网络可能不通）。"
        )

    from langchain_tavily import TavilySearch

    return TavilySearch(
        name=SEARCH_TOOL_NAME,
        max_results=settings.search_max_results,
        tavily_api_key=settings.tavily_api_key,
    )


def _build_duckduckgo_tool() -> BaseTool:
    """构造 DuckDuckGo 搜索工具（免费无需 Key）。"""
    from langchain_community.tools import DuckDuckGoSearchResults

    return DuckDuckGoSearchResults(
        name=SEARCH_TOOL_NAME,
        max_results=settings.search_max_results,
    )


def load_search_tools() -> list[BaseTool]:
    """按配置构造搜索工具列表，供 agent.py 与 MCP 工具合并。"""
    provider = settings.search_provider

    if provider == "tavily":
        return [_build_tavily_tool()]
    if provider == "duckduckgo":
        return [_build_duckduckgo_tool()]

    raise ValueError(f"不支持的 SEARCH_PROVIDER: {provider}（可选 tavily / duckduckgo）")


if __name__ == "__main__":
    # 自测：发起一次真实搜索。必须在项目根目录用模块方式运行，否则 import config 会失败：
    #     python -m tools.search
    from rich.console import Console

    console = Console()
    tools = load_search_tools()
    for tool in tools:
        console.print(f"[bold]工具名:[/bold] {tool.name}")
        console.print(f"[bold]描述:[/bold] {tool.description[:120]}...")
        console.print(f"\n[bold cyan]执行搜索:[/bold cyan] 'kubernetes pod CrashLoopBackOff 原因'")
        try:
            result = tool.invoke({"query": "kubernetes pod CrashLoopBackOff 原因"})
            console.print(f"[green]返回类型:[/green] {type(result).__name__}")
            console.print(str(result)[:800])
        except Exception as exc:  # noqa: BLE001 - 自测脚本，原样打印异常便于排查
            console.print(f"[red]搜索失败:[/red] {type(exc).__name__}: {exc}")
