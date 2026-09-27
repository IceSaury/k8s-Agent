"""
搜索工具封装。

职责：把互联网搜索包装成 Agent 能调用的单一工具 web_search，屏蔽搜索服务商差异。
切换只改 .env 里的 SEARCH_PROVIDER，业务代码不用动。

两个后端：
  - duckduckgo（默认，免费无需 Key）—— 直接调用 ddgs 库。
    这里没有用 langchain-community 的 DuckDuckGoSearchResults，原因是它的封装
    不允许为单次搜索传代理；而 DuckDuckGo 在本机网络必须走代理，且我们不能用
    全局 HTTPS_PROXY（那会把 DashScope 的 LLM 调用也一起代理掉）。
    ddgs.DDGS(proxy=...) 支持显式传代理，作用域干净。
  - tavily（备选，需 TAVILY_API_KEY）—— 复用 langchain-tavily 的 TavilySearch。

依赖说明：PDF 写的包名 duckduckgo-search 已改名为 ddgs，langchain-community
0.4.2 也只认 ddgs，所以 requirements 里装的是 ddgs。
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool

from config import settings

# 工具名。系统提示词 prompts.py 里引用的是这个名字，改名要同步改
TOOL_NAME = "web_search"

# 缓存的 Tavily 工具实例，避免每次搜索都重建
_tavily_tool: BaseTool | None = None


def _search_duckduckgo(query: str) -> str:
    """用 ddgs 搜索，代理来自 SEARCH_PROXY（留空则用系统环境变量或直连）。"""
    from ddgs import DDGS

    results = DDGS(
        proxy=settings.search_proxy or None,
        timeout=settings.search_timeout,
    ).text(query, max_results=settings.search_max_results)

    if not results:
        return f"未搜索到与「{query}」相关的结果。"

    lines = [f"搜索「{query}」得到 {len(results)} 条结果："]
    for index, item in enumerate(results, 1):
        lines.append(
            f"\n[{index}] {item.get('title', '')}\n"
            f"    URL: {item.get('href', '')}\n"
            f"    {item.get('body', '')}"
        )
    return "\n".join(lines)


def _get_tavily_tool() -> BaseTool:
    """构造（并缓存）Tavily 搜索工具。"""
    global _tavily_tool
    if _tavily_tool is None:
        from langchain_tavily import TavilySearch

        _tavily_tool = TavilySearch(
            name=TOOL_NAME,
            max_results=settings.search_max_results,
            tavily_api_key=settings.tavily_api_key,
        )
    return _tavily_tool


@tool(TOOL_NAME, parse_docstring=True)
def web_search(query: str) -> str:
    """搜索互联网。用于检索 Kubernetes 官方文档、排障案例与最佳实践。

    Args:
        query: 搜索关键词，建议包含具体报错信息或 K8s 资源类型。
    """
    provider = settings.search_provider

    if provider == "duckduckgo":
        return _search_duckduckgo(query)

    if provider == "tavily":
        result = _get_tavily_tool().invoke({"query": query})
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, indent=2)

    raise ValueError(f"不支持的 SEARCH_PROVIDER: {provider}（可选 duckduckgo / tavily）")


def load_search_tools() -> list[BaseTool]:
    """返回搜索工具列表，供 agent.py 与 MCP 工具合并。

    这里会做一次配置校验：配置有问题就抛错，让启动阶段尽早暴露，
    而不是等用户提问时才发现搜不了（agent.py 会把它降级成警告）。
    """
    provider = settings.search_provider

    if provider not in ("duckduckgo", "tavily"):
        raise ValueError(f"不支持的 SEARCH_PROVIDER: {provider}（可选 duckduckgo / tavily）")

    if provider == "tavily":
        if not settings.tavily_api_key or "REPLACE_ME" in settings.tavily_api_key:
            raise RuntimeError(
                "SEARCH_PROVIDER=tavily 但 TAVILY_API_KEY 未配置。"
                "请到 tavily.com 注册获取 Key 并填入 .env；"
                "或把 SEARCH_PROVIDER 改为 duckduckgo（需要代理，见 README）。"
            )
        _get_tavily_tool()

    return [web_search]


if __name__ == "__main__":
    # 自测：发起一次真实搜索。必须在项目根目录用模块方式运行，否则 import config 会失败：
    #     python -m tools.search
    from rich.console import Console

    console = Console()
    console.print(f"[bold]搜索后端:[/bold] {settings.search_provider} | "
                  f"[bold]代理:[/bold] {settings.search_proxy or '(未配置)'}")
    console.print("[bold cyan]执行搜索:[/bold cyan] kubernetes pod CrashLoopBackOff 原因")
    try:
        console.print(web_search.invoke({"query": "kubernetes pod CrashLoopBackOff 原因"})[:1200])
    except Exception as exc:  # noqa: BLE001 - 自测脚本，原样打印便于排查
        console.print(f"[red]搜索失败:[/red] {type(exc).__name__}: {exc}")
