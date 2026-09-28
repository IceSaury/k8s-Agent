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

工具返回值还要再过一道"瘦身"（_trim_list_result）：resource list 一次列全量 Pod 的原始
YAML 有 135KB，每次模型调用都得重读一遍，是回答越来越慢的主因，所以投影成运维视图再给模型。

虚拟机没开机时，MCP Server 不可达。此时若 ALLOW_MCP_FALLBACK=true 就回退到
tools/mock_k8s.py 的离线工具，保证 Agent 主逻辑仍可开发和演示。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import RemoveMessage

from config import settings
from prompts import SYSTEM_PROMPT
from tools.mock_k8s import load_mock_tools
from tools.search import TOOL_NAME as SEARCH_TOOL_NAME
from tools.search import load_search_tools

# MCP Server 在客户端里的标识名，与用户提供的 mcpServers 配置保持一致
MCP_SERVER_NAME = "k8s-eye"

# 默认会话 ID。多轮对话靠它区分上下文：同一个 thread_id 才能记住历史
DEFAULT_THREAD_ID = "cli"


def _sanitize_tool_name(raw_name: str) -> str:
    """把 MCP 工具名转成 LLM 能接受的标识符：空格与非法字符统一换成下划线。"""
    return re.sub(r"[^0-9a-zA-Z_-]", "_", raw_name)


# "全部命名空间"的常见写法。本项目的 MCP Server 只认空字符串：这些写法会被当成
# 真实的命名空间名去查、返回空列表（LLM 爱写 "all"，因为它习惯 kubectl 的 -A）
_ALL_NAMESPACES = {"all", "*", "-a", "all-namespaces", "all_namespaces", "--all-namespaces"}

# 这些工具缺少 namespace 时，服务端会掐掉连接、一个字节都不回（客户端只能干等到超时）。
# 补空字符串是安全的：它们都是只读工具，且空串表示"全部命名空间"
_NAMESPACE_FALLBACK_TOOLS = {"resource_list"}


def _fix_namespace_args(tool_name: str, kwargs: dict) -> dict:
    """
    入参兜底，两件事：

    1. `namespace="all"` 这类写法改成 `""`——服务端不认 "all"，会返回空列表，
       LLM 拿到空列表容易误判成"集群里没有资源"，然后一路瞎猜（真的踩过）。
    2. `resource list` 缺 namespace 时补 `""`——该 Server 缺参数就不响应，
       补一个安全的默认值比白白等一次超时强。
    """
    if "namespace" not in kwargs:
        if tool_name in _NAMESPACE_FALLBACK_TOOLS:
            print(f"  [WARN] {tool_name} 缺少 namespace，已补 '' （表示全部命名空间）")
            return {**kwargs, "namespace": ""}
        return kwargs

    value = kwargs["namespace"]
    if isinstance(value, str) and value.strip().lower() in _ALL_NAMESPACES:
        print(f"  [WARN] {tool_name} 的 namespace={value!r} 已改为 ''（该 Server 用空字符串表示全部命名空间）")
        return {**kwargs, "namespace": ""}
    return kwargs


# ---------------------------------------------------------------------------
# 工具结果瘦身：resource list 的原始 YAML 太大
#
# 真机实测（2026-09-28，22 个 Pod）：一次 `resource list` 列 Pod 返回 135.2KB YAML。
# 体积不是被某个"垃圾字段"吃掉的——managedFields 服务端已经剥掉了，annotations 只占
# 4.5KB——而是平摊在 spec(31KB) / status(31KB) / metadata(16KB) 上。真正对运维问答没用
# 的噪音是 spec.volumes+projected(14.8KB)、spec.tolerations(4.9KB)、volumeMounts(2.5KB)、
# imageID(2.4KB)；Node 那边 status.images 一项就占 12.9KB（Node 总共才 20.3KB）。
#
# 按下表投影后：Pod 135.2KB -> 18.3KB，Node 20.3KB -> 4.4KB，而 restartCount、
# lastState.reason、conditions、nodeName、image、requests/limits 这些真正用来答题的
# 字段一个没丢。
#
# 只裁 resource list —— 它是唯一会整片返回几十个对象的工具。resource describe / get
# 的语义就是"我要这个对象的完整定义"，裁掉等于把用户要查的东西裁掉，不动。
# ---------------------------------------------------------------------------

# 容器状态里这串 70 字符的 docker sha 对答题没有任何用处
_STATE_NOISE_KEYS = {"containerID"}


def _strip_state(block: Any) -> dict:
    """
    清理 state / lastState。

    它们的形状是 {"terminated": {...}} / {"running": {...}}，要丢的 containerID 藏在
    里层那一块，所以外层内层都过滤一遍。其余（reason、exitCode、时间）原样保留。
    """
    if not isinstance(block, dict):
        return {}
    cleaned: dict = {}
    for outer_key, outer_value in block.items():
        if outer_key in _STATE_NOISE_KEYS:
            continue
        if isinstance(outer_value, dict):
            outer_value = {
                k: v for k, v in outer_value.items() if k not in _STATE_NOISE_KEYS
            }
        cleaned[outer_key] = outer_value
    return cleaned


def _project_pod(item: dict) -> dict:
    """Pod 的运维视图：在哪台节点、什么状态、容器在不在跑、重启过几次、为什么重启。"""
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels"),
        "nodeName": spec.get("nodeName"),
        "phase": status.get("phase"),
        "podIP": status.get("podIP"),
        "hostIP": status.get("hostIP"),
        "containers": [
            {"name": c.get("name"), "image": c.get("image"), "resources": c.get("resources")}
            for c in (spec.get("containers") or [])
        ],
        # conditions 全是 True 是常态，逐条列出来纯属占体积；只保留非 True 的异常项。
        # 空列表就是"各项条件都正常"，这一点在 _TRIM_NOTE 里跟模型讲清楚
        "abnormalConditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": c.get("message"),
            }
            for c in (status.get("conditions") or [])
            if c.get("status") != "True"
        ],
        "containerStatuses": [
            {
                "name": c.get("name"),
                "ready": c.get("ready"),
                "restartCount": c.get("restartCount"),
                "state": _strip_state(c.get("state")),
                "lastState": _strip_state(c.get("lastState")),
            }
            for c in (status.get("containerStatuses") or [])
        ],
    }


def _project_node(item: dict) -> dict:
    """Node 的运维视图：地址、容量、可分配、状态条件、版本信息。"""
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    return {
        "name": meta.get("name"),
        "labels": meta.get("labels"),
        "unschedulable": spec.get("unschedulable"),
        "taints": spec.get("taints"),
        "addresses": status.get("addresses"),
        "capacity": status.get("capacity"),
        "allocatable": status.get("allocatable"),
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in (status.get("conditions") or [])
        ],
        "nodeInfo": status.get("nodeInfo"),
        # 镜像清单是 Node 结果的体积大头，压成一个数量。真需要看镜像列表时模型会去
        # 调别的工具或 describe 单个节点，而不是每次列节点都拖着 13KB 镜像名
        "imageCount": len(status.get("images") or []),
    }


_PROJECTORS = {"Pod": _project_pod, "Node": _project_node}

# 精简之后必须告诉模型"哪些字段没了"，否则它会以为集群里真的没有探针/挂载配置，
# 或者干脆凭猜测补细节（这类坑前面踩过）
_TRIM_NOTE = (
    "\n[说明：以上是 resource list 的运维视图，已精简掉 volumes/tolerations/探针/环境变量/"
    "serviceAccount 等字段；abnormalConditions 只列出非 True 的条件，空列表表示各项条件均正常；"
    "Node 的镜像列表只保留了数量。需要某个对象的完整定义时，对它单独调用 "
    "resource describe 或 resource get，不要凭猜测补细节。]"
)


def _unwrap(result: Any) -> Any:
    """
    剥掉外层容器，拿到真正的内容块。

    坑：我们的包装层罩在 MCP 适配器的 coroutine 外面，而这套工具是
    `response_format="content_and_artifact"`，所以 coroutine 返回的是
    `(内容块列表, artifact)` **二元组**，不是内容块本身；要等 `StructuredTool._arun`
    在我们返回之后才把它转成 ToolMessage 的 content。只按 list/dict 判断会什么都
    取不到，于是瘦身每次提前返回、静默失效（真机踩过：ainvoke 拿到 138KB 原文）。
    """
    if isinstance(result, tuple) and len(result) == 2:
        result = result[0]
    # CallToolResult 之类的容器：内容块挂在 .content 上
    return getattr(result, "content", result)


def _result_text(result: Any) -> str:
    """从工具返回值里取出纯文本。认三种形状：裸字符串、字典块、带 .text 的内容对象。"""
    blocks = _unwrap(result)
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, (list, tuple)):
        return ""

    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text" and getattr(block, "text", None) is not None:
            parts.append(str(block.text))
    return "".join(parts)


def _rebuild_text(result: Any, new_text: str) -> Any:
    """把精简后的文本塞回原来的返回形状，不破坏 content_and_artifact 期望的结构。"""
    blocks = _unwrap(result)
    content: Any = [{"type": "text", "text": new_text}] if isinstance(blocks, list) else new_text
    if isinstance(result, tuple) and len(result) == 2:
        return (content, result[1])
    return content


def _trim_list_result(result: Any) -> Any:
    """
    把 resource list 的 YAML 投影成运维视图。

    任何"形状不对"的情况都原样返回：不是文本、解析不出 YAML、不是对象列表、kind 没有
    对应投影器、序列化失败。瘦身是优化，绝不能把工具调用本身搞坏。
    """
    if not settings.trim_tool_payload:
        return result
    text = _result_text(result)
    if not text:
        return result
    try:
        doc = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - 多半是错误信息而非 YAML，退回原文
        return result
    if not isinstance(doc, list) or not doc or not all(isinstance(i, dict) for i in doc):
        return result

    projector = _PROJECTORS.get(str(doc[0].get("kind") or ""))
    if projector is None:
        return result
    try:
        body = yaml.safe_dump(
            [projector(item) for item in doc],
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    except Exception:  # noqa: BLE001 - 投影本身出错时同样退回原文
        return result

    saved = (len(text) - len(body)) / len(text) * 100
    print(f"  [TRIM] resource list 结果 {len(text)/1024:.1f}KB -> {len(body)/1024:.1f}KB（省 {saved:.0f}%）")
    return _rebuild_text(result, body + _TRIM_NOTE)


def _harden_tool(tool: BaseTool) -> None:
    """
    给工具包三层："入参兜底 + 调用超时 + 结果瘦身"。

    包装点在 tool.coroutine（StructuredTool 的异步实现），调用链是：
        ToolNode -> tool.ainvoke -> StructuredTool._arun -> self.coroutine(**args)
    在这里既能拿到 LLM 传进来的参数，又能把整个网络调用罩住，还能改它返回给模型的内容。

    只有异步工具（MCP 工具）才有 coroutine；本地的 web_search 与 mock 工具是同步函数，
    这里会直接跳过——它们本来就有自己的超时，也不会挂死。
    """
    original = tool.coroutine
    if original is None:
        return

    async def hardened(*args: Any, **kwargs: Any) -> Any:
        args_fixed = _fix_namespace_args(tool.name, kwargs)
        try:
            result = await asyncio.wait_for(
                original(*args, **args_fixed), timeout=settings.tool_call_timeout
            )
        except asyncio.TimeoutError as exc:
            # 抛普通异常即可：LangGraph 的 ToolNode 会把错误文本作为工具结果回给 LLM，
            # 让它自己换个工具或换组参数重试，而不是让整轮对话一直挂着
            raise TimeoutError(
                f"调用工具 {tool.name} 超过 {settings.tool_call_timeout} 秒没有任何返回，"
                "已中止本次调用。可能是该工具对当前入参不响应，请换用其他工具或换个参数重试。"
            ) from exc

        if tool.name == "resource_list":
            return _trim_list_result(result)
        return result

    tool.coroutine = hardened


def _prepare_tools(raw_tools: list[BaseTool]) -> list[BaseTool]:
    """工具统一后处理：净化工具名、把原始名记进 metadata、并做加固（兜底 + 超时 + 瘦身）。"""
    for tool in raw_tools:
        original = tool.metadata.get("mcp_name") if tool.metadata else None
        if original is None:
            tool.metadata = {**(tool.metadata or {}), "mcp_name": tool.name}
        tool.name = _sanitize_tool_name(tool.name)
        _harden_tool(tool)
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


# ---------------------------------------------------------------------------
# 记忆治理：轮数窗口 + 旧工具结果清理
#
# 背景：checkpointer 会把每轮的 ToolMessage 原文永久存档。resource list 拉一次全量
# pods 就是 143KB YAML，之后每轮、每次模型调用都要把这段旧 YAML 重新预填一遍，
# 第二轮开始就明显变慢（实测 12.9s -> 37s+）。这里两道闸门：
#   1. 轮数窗口：历史超过 MEMORY_WINDOW_ROUNDS 轮时，把最老的整轮从存档里删掉；
#   2. 体积清理：发给模型时，把"已结束旧轮"的工具结果换成占位符（不改存档）。
# 上一轮的结论仍留在该轮最终回答（AIMessage）里，模型照常可以引用。
# ---------------------------------------------------------------------------

# 被清理的工具结果的占位符。要明确告诉模型"内容没了、需要就重查"，
# 避免它把占位符当成真实查询结果继续推理
_CLEARED_TOOL_PLACEHOLDER = (
    "[该工具结果属于较早的对话轮次，为控制上下文体积已清理。"
    "如仍需这些细节，请重新调用对应工具，不要凭记忆猜测。]"
)


def _round_starts(messages: Sequence[BaseMessage]) -> list[int]:
    """返回每轮对话的起始下标（即每条 HumanMessage 的位置）。"""
    return [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]


def _window_removals(messages: Sequence[BaseMessage], window: int) -> list[RemoveMessage]:
    """超过 window 轮时，把最老的若干轮整轮删除（含其中的工具调用与结果）。

    删除永远对齐轮次边界，不会只删 AIMessage 而留下它对应的 ToolMessage（反之亦然），
    避免模型看到"有工具结果却没有工具调用"的残缺历史。
    """
    starts = _round_starts(messages)
    if len(starts) <= window:
        return []
    keep_from = starts[-window]
    return [RemoveMessage(id=m.id) for m in messages[:keep_from] if getattr(m, "id", None)]


def _clear_old_tool_results(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """把"已结束旧轮"的工具结果换成占位符，当前轮的原样保留。

    只改本次发给模型的副本（返回新列表），不动 state 与存档。当前轮以最后一条
    HumanMessage 为起点：它之前的 ToolMessage 都属于已结束的轮次。
    """
    starts = _round_starts(messages)
    if not starts:
        return list(messages)
    current_start = starts[-1]

    edited: list[BaseMessage] = []
    for idx, message in enumerate(messages):
        if (
            idx < current_start
            and isinstance(message, ToolMessage)
            and message.content != _CLEARED_TOOL_PLACEHOLDER
        ):
            message = message.model_copy(update={"content": _CLEARED_TOOL_PLACEHOLDER})
        edited.append(message)
    return edited


class MemoryWindowMiddleware(AgentMiddleware):
    """记忆治理中间件，两个钩子各管一件事。

    - before_model：轮数窗口。返回 RemoveMessage 列表，LangGraph 的消息 reducer 会
      把它们从 state（也就是存档）里真正删掉，历史因此不会无限增长。
    - wrap_model_call：体积清理。在请求副本上替换旧工具结果，只影响这一次模型调用。
    """

    def before_model(self, state, runtime):  # noqa: ARG002 - 中间件签名固定
        removals = _window_removals(state["messages"], settings.memory_window_rounds)
        return {"messages": removals} if removals else None

    async def abefore_model(self, state, runtime):  # noqa: ARG002
        return self.before_model(state, runtime)

    def wrap_model_call(self, request, handler):
        return handler(request.override(messages=_clear_old_tool_results(request.messages)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(messages=_clear_old_tool_results(request.messages)))


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
        middleware=[MemoryWindowMiddleware()],
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

    def tool_groups(self) -> tuple[list[str], list[str]]:
        """按 (K8s 工具, 搜索工具) 分组返回展示名，供 CLI 启动信息与 tools 命令使用。

        注意返回的是展示名（含空格，如 "resource list"），与 PDF §1.4 的输出一致。
        """
        k8s, search = [], []
        for tool in self.tools:
            display = self.display_names.get(tool.name, tool.name)
            (search if tool.name == SEARCH_TOOL_NAME else k8s).append(display)
        return sorted(k8s), sorted(search)

    def tool_names(self) -> list[str]:
        """全部工具的展示名，K8s 工具在前、搜索工具在后。"""
        k8s, search = self.tool_groups()
        return [*k8s, *search]

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
