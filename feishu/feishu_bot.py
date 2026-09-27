"""
飞书机器人入口（对应 PDF §4.2 feishu/feishu_bot.py：飞书机器人交互、卡片答复）。

与 CLI 入口共用 agent.K8sAgent，区别只在"怎么收问题、怎么显示过程"：
    CLI   —— input() 读输入，rich 控制台渲染
    飞书  —— 长连接收事件，卡片消息渲染（工具轨迹 + 回答都刷新在同一张卡片上）

## 为什么用 WebSocket 长连接

飞书事件订阅默认要求一个公网可回调的地址，本地开发要么内网穿透要么上服务器。
lark-oapi 提供的 WebSocket 长连接模式由客户端主动连飞书，不需要公网 IP。

## 线程与事件循环模型（本文件最容易看错的地方）

    lark-oapi 的 ws.Client.start() 是「阻塞 + 自建事件循环」的：
    它内部用 run_until_complete 跑自己的 loop，一直不返回。
    Agent 这边是 async 的，也需要一个事件循环。
    两者不能共用一个循环，否则一边跑一边要跑另一边就会报 "event loop is already running"。

所以分成两条线：

    主线程                         lark-ws 线程（daemon）
    agent_loop.run_forever()       ws_client.start()  —— 阻塞住，负责协议与心跳
         ↑                                  |
         |   run_coroutine_threadsafe       |  收到消息 -> 同步回调 on_message()
         +----------------------------------+
                    _process(...)

    事件回调必须**立刻返回**：它跑在 lark 自己的循环里，一旦在里面做 LLM 调用（几十秒），
    心跳发不出去，连接会被飞书断掉。所以回调只做一件事——把协程投递到 agent_loop 就返回。

## 卡片刷新

一轮问答只占用一条飞书消息：先 reply 一张「正在分析…」卡片拿到 message_id，
之后工具调用与流式回答都用 PATCH 刷新同一张卡片，避免刷屏。

卡片用 **卡片 JSON 2.0** 结构（见 build_card）：2.0 的 markdown 组件支持全部标准
Markdown，LLM 的回答可以原样贴进去。注意 2.0 的卡片必须是共享卡片
（config.update_multi 只能为 true），只有共享卡片才允许被 PATCH 更新。

启动前需要 .env 里填好 FEISHU_APP_ID / FEISHU_APP_SECRET，并在开发者后台：
    1. 开启「事件订阅」-> 订阅方式选「长连接」
    2. 订阅事件「接收消息 im.message.receive_v1」
    3. 开通权限 im:message（发消息）、im:message:send_as_bot
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from console_boot import bootstrap_console

bootstrap_console()

from agent import K8sAgent  # noqa: E402
from config import settings  # noqa: E402

# 卡片里最多显示多少行工具轨迹，超出的折叠成一行汇总
MAX_TRACE_LINES = 12

# 流式刷新卡片的最小间隔（秒）。飞书对消息更新有频率限制，刷太快会被限流
PATCH_INTERVAL = 1.2

# 单条提问最多取多少字符，防止有人粘贴一整篇日志把上下文撑爆
MAX_INPUT_CHARS = 1000

# 内置命令：只清当前会话的对话历史
_RESET_WORDS = {"clear", "reset", "清空", "重置"}

# 已处理过的 message_id。飞书可能重投事件，靠它避免同一条消息回两次
_seen_message_ids: set[str] = set()

# 工具名 -> 中文说明，用于卡片上的轨迹展示（没有的按原名显示）
_TOOL_LABELS = {
    "node analyze": "节点分析",
    "pod analyze": "Pod 分析",
    "pod logs": "Pod 日志",
    "deployment analyze": "Deployment 分析",
    "service analyze": "Service 分析",
    "ingress analyze": "Ingress 分析",
    "resource list": "资源列表",
    "resource get": "资源详情",
    "resource describe": "资源 describe",
    "workload resource usage": "资源用量",
    "web_search": "联网搜索",
}


# ---------------------------------------------------------------------------
# 纯函数：消息解析与卡片渲染（不依赖网络，可单独测试）
# ---------------------------------------------------------------------------

def _log(message: str) -> None:
    """带时间戳的日志。控制台不打印 emoji，避免 GBK 终端下出问题。"""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _is_placeholder(value: str) -> bool:
    """判断飞书凭证是否还是 .env 里的占位符。"""
    return not value or "REPLACE_ME" in value


def _mark_seen(message_id: str) -> bool:
    """记录已处理的 message_id。返回 True 表示这是新消息，False 表示重复投递。"""
    if message_id in _seen_message_ids:
        return False
    _seen_message_ids.add(message_id)
    # 只留最近的一批，避免长期运行后无限增长
    if len(_seen_message_ids) > 500:
        _seen_message_ids.clear()
    return True


def extract_text(message: Any) -> str:
    """从飞书消息体里取出纯文本，并去掉 @机器人 留下的占位符。"""
    try:
        content = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        return ""
    text = content.get("text", "")
    for mention in message.mentions or []:
        if mention.key:
            text = text.replace(mention.key, "")
    return text.strip()


def _tool_label(name: str) -> str:
    """工具名转展示文本：认识的给中文名，不认识的保留原名。"""
    return _TOOL_LABELS.get(name, name)


def build_card(
    trace: list[str],
    body: str,
    *,
    subtitle: str = "",
    template: str = "blue",
) -> dict:
    """
    拼一张飞书卡片（卡片 JSON 2.0 结构）。

    为什么必须是 2.0：2.0 的 markdown 组件支持除 HTMLBlock 外的全部标准 Markdown
    （标题、表格、代码块、引用、分割线都能渲染），所以 LLM 的回答可以原样贴进去。
    1.0 的 lark_md 只支持行内语法，标题会显示成 "## xxx"、表格会显示成一堆竖线，
    之前那套"表格转列表"的降级转换就是为了绕开它，换到 2.0 后整块删掉了。

    结构分三块：config（卡片行为）、header（标题栏）、body（正文元素）。
    执行过程放进折叠面板，默认收起，避免抢走回答的视觉重心。
    """
    elements: list[dict] = []

    if trace:
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,  # 默认收起，不抢回答的视觉重心
                "background_color": "grey",
                "header": {"title": {"tag": "markdown", "content": f"**执行过程（{len(trace)} 步）**"}},
                "border": {"color": "grey", "corner_radius": "6px"},
                # 一行一个 markdown 组件：单个组件里用 \n 换行属于软换行，
                # 渲染器可能忽略，拆成多个组件才能保证一行一条
                "elements": [{"tag": "markdown", "content": line} for line in trace],
            }
        )

    elements.append({"tag": "markdown", "content": body or "🔍 正在分析，请稍候…"})

    card: dict = {
        "schema": "2.0",
        "config": {
            # 2.0 只支持共享卡片（update_multi 只能为 true），共享卡片才允许被 PATCH 更新
            "update_multi": True,
            "width_mode": "fill",  # 撑满聊天窗口，宽表格才放得下
            "summary": {"content": "K8s 运维助手的回答"},
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "K8s 运维助手"},
        },
        "body": {"elements": elements},
    }
    if subtitle:
        card["header"]["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return card


# ---------------------------------------------------------------------------
# 卡片回复：一条消息 + 反复 PATCH 刷新它
# ---------------------------------------------------------------------------

class CardReply:
    """
    挂在一条飞书卡片消息上的回复器。

    工具调用与回答片段都先更新到内存状态，再由 flush() 用一次 PATCH 推到飞书。
    流式回答靠 maybe_flush() 节流，最终结果由 flush() 强制推送。
    """

    def __init__(self, message_id: str, chat_id: str):
        self.message_id = message_id
        self.chat_id = chat_id
        self.trace: list[str] = []
        self.answer = ""
        self.failed = False
        self.started = time.monotonic()
        self._last_patch = 0.0
        self._pending_tools: dict[str, list[float]] = {}

    def on_tool_start(self, name: str, args: dict) -> None:
        self._pending_tools.setdefault(name, []).append(time.monotonic())
        detail = ""
        if args:
            try:
                detail = json.dumps(args, ensure_ascii=False)
            except TypeError:
                detail = str(args)
            if len(detail) > 60:
                detail = detail[:57] + "..."
        label = _tool_label(name)
        self.trace.append(f"🔧 调用 **{label}**{'  ' + detail if detail else ''}")

    def on_tool_end(self, name: str, ok: bool) -> None:
        started = self._pending_tools.get(name) or []
        cost = f"  ({time.monotonic() - started.pop(0):.1f}s)" if started else ""
        mark = "✅" if ok else "❌"
        self.trace.append(f"{mark} {_tool_label(name)} 完成{cost}")

    def _trace_text(self) -> list[str]:
        """截断过长的轨迹：留前 MAX_TRACE_LINES-1 行，其余折叠成一行汇总。"""
        if len(self.trace) <= MAX_TRACE_LINES:
            return self.trace
        hidden = len(self.trace) - (MAX_TRACE_LINES - 1)
        return [*self.trace[: MAX_TRACE_LINES - 1], f"… 另有 {hidden} 条工具记录"]

    def _subtitle(self, final: bool) -> str:
        """卡片副标题：跑的时候给状态，结束时给个小结。"""
        if not final:
            return "正在分析…"
        if self.failed:
            return "出错了"
        steps = f" · {len(self.trace)} 步工具调用" if self.trace else ""
        return f"完成{steps} · {time.monotonic() - self.started:.1f}s"

    def _card(self, final: bool) -> dict:
        return build_card(
            self._trace_text(),
            self.answer,
            subtitle=self._subtitle(final),
            template="red" if self.failed else "blue",
        )

    async def maybe_flush(self, client: Any) -> None:
        """流式期间按 PATCH_INTERVAL 节流刷新。"""
        now = time.monotonic()
        if now - self._last_patch < PATCH_INTERVAL:
            return
        self._last_patch = now
        await _patch_card(client, self.message_id, self._card(final=False), quiet=True)

    async def flush(self, client: Any) -> None:
        """强制刷新（最终回答）。"""
        self._last_patch = time.monotonic()
        await _patch_card(client, self.message_id, self._card(final=True), quiet=False)


# ---------------------------------------------------------------------------
# 飞书 API 调用（同步 SDK，统一丢到线程里跑，别阻塞事件循环）
# ---------------------------------------------------------------------------

def _api_error(response: Any) -> str:
    """把 SDK 的响应整理成一行错误描述。"""
    return f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"


def _reply_card(client: Any, message_id: str, card: dict) -> str | None:
    """回复一张卡片，返回新消息的 message_id（后续 PATCH 要用）。"""
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .msg_type("interactive")
            .build()
        )
        .build()
    )
    response = client.im.v1.message.reply(request)
    if not response.success():
        _log(f"[ERROR] 回复卡片失败：{_api_error(response)}")
        return None
    return response.data.message_id


def _patch_card_sync(client: Any, message_id: str, card: dict, quiet: bool) -> None:
    from lark_oapi.api.im.v1.model.patch_message_request import PatchMessageRequest
    from lark_oapi.api.im.v1.model.patch_message_request_body import PatchMessageRequestBody

    request = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.patch(request)
    if not response.success() and not quiet:
        _log(f"[ERROR] 更新卡片失败：{_api_error(response)}")


async def _patch_card(client: Any, message_id: str, card: dict, quiet: bool) -> None:
    # 同步 SDK + 网络请求，必须丢线程，否则会卡住整个 Agent 事件循环
    try:
        await asyncio.to_thread(_patch_card_sync, client, message_id, card, quiet)
    except Exception as exc:  # noqa: BLE001 - 刷新卡片失败不该中断本轮回答
        if not quiet:
            _log(f"[WARN] 更新卡片异常：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------

async def process_message(bot: K8sAgent, client: Any, data: Any) -> None:
    """处理一条飞书消息：回卡片 -> 跑 Agent -> 刷新卡片。"""
    message = data.event.message
    chat_id = message.chat_id or ""
    thread_id = f"feishu:{chat_id}"

    if not _mark_seen(message.message_id):
        return  # 重复投递，忽略

    if message.message_type != "text":
        body = "目前只认文字提问，图片/文件/语音还不会看。"
        await asyncio.to_thread(
            _reply_card, client, message.message_id, build_card([], body, subtitle="暂不支持")
        )
        return

    # 群聊里只有被 @ 到才回应；私聊直接回应
    if message.chat_type != "p2p" and not message.mentions:
        return

    question = extract_text(message)
    if not question:
        return
    if len(question) > MAX_INPUT_CHARS:
        question = question[:MAX_INPUT_CHARS]

    _log(f"收到提问（{message.chat_type} {chat_id}）：{question}")

    if question.lower() in _RESET_WORDS:
        bot.reset_memory(thread_id)
        await asyncio.to_thread(
            _reply_card, client, message.message_id,
            build_card([], "已清空本会话的对话历史。", subtitle="已重置"),
        )
        return

    reply_message_id = await asyncio.to_thread(
        _reply_card, client, message.message_id, build_card([], "", subtitle="正在分析…")
    )
    if reply_message_id is None:
        return

    reply = CardReply(reply_message_id, chat_id)

    try:
        async for event in bot.astream(question, thread_id=thread_id):
            if event.kind == "tool_start":
                reply.on_tool_start(event.tool_name, event.tool_args)
                await reply.maybe_flush(client)
            elif event.kind == "tool_end":
                reply.on_tool_end(event.tool_name, event.ok)
                await reply.maybe_flush(client)
            elif event.kind == "token":
                reply.answer += event.text
                await reply.maybe_flush(client)
    except Exception as exc:  # noqa: BLE001 - 单条消息失败不能让机器人退出
        _log(f"[ERROR] 处理提问失败：{type(exc).__name__}: {exc}")
        reply.failed = True
        reply.answer = reply.answer or f"处理这条消息出错了：`{type(exc).__name__}: {exc}`"

    if not reply.answer.strip():
        reply.answer = "_（本轮没有文字回答）_"
    await reply.flush(client)
    _log(f"已回复（{len(reply.trace)} 条工具记录，{len(reply.answer)} 字）")


def make_handler(bot: K8sAgent, client: Any, agent_loop: asyncio.AbstractEventLoop):
    """
    造事件回调。

    回调本身跑在 lark 的 WS 线程里，所以这里**只做投递**：把协程丢到 agent_loop
    就立刻返回，真正的 LLM 调用与卡片刷新都在 agent_loop 上执行。
    """

    def on_message(data: Any) -> None:
        asyncio.run_coroutine_threadsafe(process_message(bot, client, data), agent_loop)

    return on_message


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _build_http_client() -> Any:
    """建飞书 HTTP 客户端（发消息/刷新卡片用），与长连接客户端是两码事。

    只能在 WS 线程里调用，原因见 _run_ws_client 的注释。
    """
    import lark_oapi as lark

    return (
        lark.Client.builder()
        .app_id(settings.feishu_app_id)
        .app_secret(settings.feishu_app_secret)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _build_event_handler(bot: K8sAgent, client: Any, agent_loop: asyncio.AbstractEventLoop):
    """只能在 WS 线程里调用，原因见 _run_ws_client 的注释。"""
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    # 长连接模式不经过 HTTP 回调，没有签名校验，encrypt_key / verification_token 传空即可
    return (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(make_handler(bot, client, agent_loop))
        .build()
    )


def _run_ws_client(bot: K8sAgent, agent_loop: asyncio.AbstractEventLoop) -> None:
    """
    WS 线程的入口：建长连接并一直阻塞。

    lark 的所有导入都必须放在本线程内，这是踩过的坑：
      lark_oapi 顶层 __init__ 第一行就是 `from . import ws`，而 ws/client.py 在
      **导入那一刻**会执行 asyncio.get_event_loop() 把当前循环存进模块级变量 loop，
      start() 里再用这个 loop 去 run_until_complete(_connect())。
      如果换成主线程先导入（比如为了建 HTTP 客户端），它抓到的就是 agent_loop，
      于是 WS 线程去跑一个主线程正在 run_forever 的循环 ->
      "RuntimeError: This event loop is already running"。
    解法：先给本线程建一个独立循环并设为当前循环，再导入 lark。
    """
    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)

    from lark_oapi.core.enum import LogLevel
    from lark_oapi.ws.client import Client as WsClient

    http_client = _build_http_client()
    event_handler = _build_event_handler(bot, http_client, agent_loop)

    ws_client = WsClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=LogLevel.WARNING,
    )

    try:
        ws_client.start()  # 阻塞：内部事件循环一直跑，直到进程退出
    except Exception as exc:  # noqa: BLE001 - 线程里抛异常会静默，必须自己兜住
        _log(f"[ERROR] 飞书长连接失败：{type(exc).__name__}: {exc}")
        _log("        请检查 FEISHU_APP_ID / FEISHU_APP_SECRET，")
        _log("        以及开发者后台是否已开启「长连接」订阅方式并订阅 im.message.receive_v1 事件")
        agent_loop.call_soon_threadsafe(agent_loop.stop)


def main() -> None:
    if _is_placeholder(settings.feishu_app_id) or _is_placeholder(settings.feishu_app_secret):
        print("[ERROR] 飞书凭证未配置：请在 .env 里填 FEISHU_APP_ID / FEISHU_APP_SECRET")
        print("        取值路径：飞书开放平台 -> 开发者后台 -> 你的应用 -> 凭证与基础信息")
        return

    _log("正在初始化 Agent...")
    # Agent 需要的事件循环。主线程拥有它，WS 线程通过 run_coroutine_threadsafe 往里投任务
    agent_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(agent_loop)

    try:
        bot = agent_loop.run_until_complete(K8sAgent.create())
    except Exception as exc:  # noqa: BLE001 - 初始化失败要给出可读提示
        _log(f"[ERROR] Agent 初始化失败：{type(exc).__name__}: {exc}")
        agent_loop.close()
        return

    k8s_names, search_names = bot.tool_groups()
    _log(f"[OK] 已加载 {len(k8s_names)} 个 K8s 工具（来源: {bot.k8s_source}）")
    _log(f"[OK] 已加载 {len(search_names)} 个搜索工具")
    _log(f"[OK] Agent 初始化完成 (LLM: {settings.llm_model})")

    # 注意：飞书相关的一切（含 HTTP 客户端与事件分发器）都在这个线程里创建，
    # 别在主线程提前 import lark_oapi，原因见 _run_ws_client 的注释
    threading.Thread(
        target=_run_ws_client,
        args=(bot, agent_loop),
        name="lark-ws",
        daemon=True,
    ).start()
    _log("[OK] 飞书长连接线程已启动，等待消息…（Ctrl+C 退出）")

    try:
        agent_loop.run_forever()
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    finally:
        agent_loop.close()


if __name__ == "__main__":
    # 启动方式（项目根目录）：python -m feishu.feishu_bot
    main()
