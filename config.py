"""
配置管理模块。

职责：把散落在 .env 里的配置读成强类型的 Settings 对象，其他模块只依赖这里，
不再自己读环境变量。全项目所有"魔法值"只在本文件出现一次。

用法：
    from config import settings
    print(settings.llm_model)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（本文件所在目录），用于定位 .env
PROJECT_ROOT = Path(__file__).resolve().parent

# 显式指定 utf-8：.env 里有中文注释，Windows 默认用 GBK 会读错
load_dotenv(PROJECT_ROOT / ".env", encoding="utf-8")


def _get(name: str, default: str = "") -> str:
    """读环境变量并去掉首尾空格。"""
    return (os.getenv(name) or default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    """读布尔型环境变量，接受 true/1/yes（大小写不敏感）。"""
    raw = _get(name)
    if not raw:
        return default
    return raw.lower() in ("true", "1", "yes", "on")


def _get_int(name: str, default: int) -> int:
    """读整数型环境变量，非法值回退默认值。"""
    raw = _get(name)
    try:
        return int(raw)
    except ValueError:
        return default


def _is_placeholder(value: str) -> bool:
    """判断是否仍是 .env 里没填的占位符。"""
    if not value:
        return True
    return "REPLACE_ME" in value or "xxxx" in value.lower()


@dataclass
class Settings:
    """项目全部配置项。字段名与 .env 变量名对应，详见 .env.example。"""

    # --- LLM ---
    llm_provider: str = "openai"
    llm_model: str = "qwen-plus"
    api_key: str = ""
    api_base: str = ""

    # --- K8s MCP Server ---
    mcp_url: str = ""
    mcp_transport: str = "sse"
    mcp_timeout: int = 10
    allow_mcp_fallback: bool = True
    tool_call_timeout: int = 30
    trim_tool_payload: bool = True

    # --- 搜索工具 ---
    search_provider: str = "duckduckgo"
    search_max_results: int = 5
    search_proxy: str = ""
    search_timeout: int = 10
    tavily_api_key: str = ""

    # --- 对话记忆 ---
    memory_window_rounds: int = 10

    # --- 飞书机器人 ---
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_connection_mode: str = "ws"

    # 记录配置加载过程中的提醒，供启动时展示（如"API Key 还没填"）
    warnings: list[str] = field(default_factory=list)


def _resolve_api_key(warnings: list[str]) -> str:
    """
    解析 LLM 的 API Key。

    优先用 .env 里的 OPENAI_API_KEY；如果它还是占位符，就回退读系统环境变量
    DASHSCOPE_API_KEY。这样密钥不必落盘到 .env，改系统变量即可。
    """
    key = _get("OPENAI_API_KEY")
    if not _is_placeholder(key):
        return key

    fallback = _get("DASHSCOPE_API_KEY")
    if fallback:
        return fallback

    warnings.append("未找到可用的 LLM API Key：请在 .env 填 OPENAI_API_KEY，或设置系统环境变量 DASHSCOPE_API_KEY")
    return ""


def load_settings() -> Settings:
    """从环境变量组装 Settings，并收集需要提醒用户的问题。"""
    warnings: list[str] = []

    mcp_transport = _get("K8S_MCP_TRANSPORT", "sse").lower()
    if mcp_transport not in ("sse", "streamable_http"):
        warnings.append(f"K8S_MCP_TRANSPORT 取值非法（{mcp_transport}），已回退为 sse")
        mcp_transport = "sse"

    feishu_app_id = _get("FEISHU_APP_ID")
    if _is_placeholder(feishu_app_id):
        warnings.append("飞书凭证未配置（FEISHU_APP_ID / FEISHU_APP_SECRET 仍是占位符），飞书入口暂不可用")

    search_provider = _get("SEARCH_PROVIDER", "duckduckgo").lower()
    tavily_api_key = _get("TAVILY_API_KEY")
    if search_provider == "tavily" and _is_placeholder(tavily_api_key):
        warnings.append("SEARCH_PROVIDER=tavily 但 TAVILY_API_KEY 未填，web_search 工具将不可用（去 tavily.com 取 Key）")

    return Settings(
        llm_provider=_get("LLM_PROVIDER", "openai"),
        llm_model=_get("LLM_MODEL", "qwen-plus"),
        api_key=_resolve_api_key(warnings),
        api_base=_get("OPENAI_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        mcp_url=_get("K8S_MCP_URL", "http://192.168.150.101:8080/sse"),
        mcp_transport=mcp_transport,
        mcp_timeout=_get_int("K8S_MCP_TIMEOUT", 10),
        allow_mcp_fallback=_get_bool("ALLOW_MCP_FALLBACK", True),
        tool_call_timeout=_get_int("TOOL_CALL_TIMEOUT", 30),
        trim_tool_payload=_get_bool("TRIM_TOOL_PAYLOAD", True),
        search_provider=search_provider,
        search_max_results=_get_int("SEARCH_MAX_RESULTS", 5),
        search_proxy=_get("SEARCH_PROXY"),
        search_timeout=_get_int("SEARCH_TIMEOUT", 10),
        tavily_api_key=tavily_api_key,
        memory_window_rounds=_get_int("MEMORY_WINDOW_ROUNDS", 10),
        feishu_app_id=feishu_app_id,
        feishu_app_secret=_get("FEISHU_APP_SECRET"),
        feishu_connection_mode=_get("FEISHU_CONNECTION_MODE", "ws").lower(),
        warnings=warnings,
    )


# 全项目共用的配置单例
settings = load_settings()


if __name__ == "__main__":
    # 自测：直接运行本文件可打印当前配置（API Key 打码）
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="当前配置")
    table.add_column("配置项")
    table.add_column("值")

    masked_key = f"{settings.api_key[:6]}...{settings.api_key[-4:]}" if len(settings.api_key) > 10 else "(未配置)"
    masked_tavily = (
        f"{settings.tavily_api_key[:8]}...{settings.tavily_api_key[-4:]}"
        if len(settings.tavily_api_key) > 12
        else "(未配置)"
    )
    for name, value in [
        ("LLM_PROVIDER", settings.llm_provider),
        ("LLM_MODEL", settings.llm_model),
        ("OPENAI_API_KEY", masked_key),
        ("OPENAI_API_BASE", settings.api_base),
        ("K8S_MCP_URL", settings.mcp_url),
        ("K8S_MCP_TRANSPORT", settings.mcp_transport),
        ("K8S_MCP_TIMEOUT", str(settings.mcp_timeout)),
        ("ALLOW_MCP_FALLBACK", str(settings.allow_mcp_fallback)),
        ("TOOL_CALL_TIMEOUT", str(settings.tool_call_timeout)),
        ("TRIM_TOOL_PAYLOAD", str(settings.trim_tool_payload)),
        ("MEMORY_WINDOW_ROUNDS", str(settings.memory_window_rounds)),
        ("SEARCH_PROVIDER", settings.search_provider),
        ("SEARCH_MAX_RESULTS", str(settings.search_max_results)),
        ("SEARCH_PROXY", settings.search_proxy or "(未配置)"),
        ("SEARCH_TIMEOUT", str(settings.search_timeout)),
        ("TAVILY_API_KEY", masked_tavily),
        ("FEISHU_APP_ID", settings.feishu_app_id),
        ("FEISHU_CONNECTION_MODE", settings.feishu_connection_mode),
    ]:
        table.add_row(name, value)
    console.print(table)

    if settings.warnings:
        console.print("\n[bold yellow]提醒：[/bold yellow]")
        for msg in settings.warnings:
            console.print(f"  - {msg}")
