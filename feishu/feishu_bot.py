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

## 卡片刷新：飞书原生流式更新

一轮问答只占用**一条**飞书消息，避免刷屏。走的是官方「流式更新卡片」通道：

```
建卡片实体(cardkit.v1.card.create) -> 拿 card_id
   └─ 用 card_id 回复消息(im.v1.message.reply)
        └─ 后台协程循环 cardkit.v1.card_element.content(card_id, element_id, 全量文本, sequence++)
             └─ 收尾推最终文本，再 cardkit.v1.card.settings 关 streaming_mode
```

三个必须照做的约定（真机探针实测）：
  - `content` 传**累计全量文本**，不是增量，增量由飞书计算；
  - `sequence` 在整张卡片生命周期内严格递增，内容更新与收尾的设置更新**共用同一个计数器**；
  - 收尾不调 `settings` 关掉 `streaming_mode`，客户端会一直挂着"生成中"的打字光标。

前置条件是应用要有 **`cardkit:card:write`** 权限（建卡片实体用的），缺它会在创建时报
`99991672`；另外能承载流式文本的组件只有 `plain_text` / `markdown`，且该组件必须自带
`element_id`，所以整张卡只剩一个正文元素——工具轨迹也拼进这段文本，原先的折叠面板取消了。

推送由 `CardReply` 的**后台协程**连着推（不设帧率闸，间隔等于一次网络往返），不在消费
token 的循环里 await。原因见 `CardReply` 的文档字符串：一次更新是实打实的 HTTPS 往返
（实测约 0.5s），写在 token 循环里会把流打断成"冻结 -> 蹦一大块"，这正是之前流式输出卡顿的来源。

卡片用 **卡片 JSON 2.0** 结构（见 build_card）：2.0 的 markdown 组件支持全部标准
Markdown，LLM 的回答可以原样贴进去。注意 2.0 的卡片必须是共享卡片
（config.update_multi 只能为 true），只有共享卡片才允许被 PATCH 更新。

启动前需要 .env 里填好 FEISHU_APP_ID / FEISHU_APP_SECRET，并在开发者后台：
    1. 开启「事件订阅」-> 订阅方式选「长连接」
    2. 订阅事件「接收消息 im.message.receive_v1」
    3. 开通权限 im:message（发消息）、im:message:send_as_bot
    4. 开通权限 cardkit:card:write（建卡片实体，流式更新靠它），改完要发布新版本才生效
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import threading
import time
from typing import Any

from console_boot import bootstrap_console

bootstrap_console()

from agent import K8sAgent  # noqa: E402
from config import settings  # noqa: E402

# 卡片里最多显示多少行工具轨迹，超出的折叠成一行汇总
MAX_TRACE_LINES = 12

# 承载流式正文的 markdown 组件 id。飞书要求流式组件自带自定义 element_id，
# 流式更新接口就是按 /cards/{card_id}/elements/{element_id}/content 定位的
STREAM_ELEMENT_ID = "agent_answer"

# 卡片没有新内容时的轮询间隔（秒）。**不是帧率闸**：有新增就立刻连着推，
# 帧率因此只由网络往返决定（实测约 0.55s/次 ≈ 1.8 帧/秒，离官方 10 次/秒很远）。
# 这个值只在"正在等工具返回、文本没变"时用来避免空转烧更新额度。
IDLE_POLL_INTERVAL = 0.05

# 进打字机的字数上限，超过就**停止喂帧**，剩下的正文留到收尾那一帧一次性出现。
#
# 为什么要有这个闸：客户端逐字速率约 **35 字/秒**且不可调（探针实测——一帧给 709 字，
# 它仍然爬了约 20 秒），而模型出字约 70 字/秒，是它的两倍。也就是说喂得越多积压越多，
# 卡片永远落在模型后面，最后关流式那一下再"啪"地补齐，观感就是"慢慢爬 + 突然跳完"。
# 关掉打字机（整卡 PATCH）最快但会丢掉逐字效果，所以取中间值：让开头那句结论逐字出现，
# 其余内容随收尾快照帧整块到位。200 字约等于"一句话总览 + 表格标题行"。
TYPEWRITER_HEAD_CHARS = 200

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
    body: str,
    *,
    subtitle: str = "",
    template: str = "blue",
    streaming: bool = True,
) -> dict:
    """
    拼一张飞书卡片（卡片 JSON 2.0 结构）。

    为什么必须是 2.0：2.0 的 markdown 组件支持除 HTMLBlock 外的全部标准 Markdown
    （标题、表格、代码块、引用、分割线都能渲染），所以 LLM 的回答可以原样贴进去。
    1.0 的 lark_md 只支持行内语法，标题会显示成 "## xxx"、表格会显示成一堆竖线，
    之前那套"表格转列表"的降级转换就是为了绕开它，换到 2.0 后整块删掉了。

    streaming=True 表示这张卡交给**流式更新接口**逐帧刷新，需满足官方列的两个前置条件：
      - config.streaming_mode = true，且 update_multi 不为 false（共享卡片）
      - 能流式的组件只有 plain_text / markdown，且该组件必须自带自定义 element_id
    于是整张卡只剩一个正文元素，工具轨迹也得拼进这段文本，原先的可折叠面板取消。
    两条快速回复（非文字输入提示、/clear 确认）不参与流式，用 streaming=False。
    subtitle 只能用在 streaming=False 的卡上：卡片头走不到流式通道，写进去就是终态。
    """
    answer: dict = {"tag": "markdown", "content": body or "🔍 正在分析，请稍候…"}
    if streaming:
        answer["element_id"] = STREAM_ELEMENT_ID

    config: dict = {
        # 2.0 只支持共享卡片（update_multi 只能为 true），共享卡片才允许被反复更新
        "update_multi": True,
        "width_mode": "fill",  # 撑满聊天窗口，宽表格才放得下
        "summary": {"content": "K8s 运维助手的回答"},
    }
    if streaming:
        config["streaming_mode"] = True

    card: dict = {
        "schema": "2.0",
        "config": config,
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "K8s 运维助手"},
        },
        "body": {"elements": [answer]},
    }
    if subtitle:
        card["header"]["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return card


# ---------------------------------------------------------------------------
# 卡片回复：一条消息 + 反复 PATCH 刷新它
# ---------------------------------------------------------------------------

class CardReply:
    """
    一条问答对应一张**流式卡片**的回复器。

    一轮的生命周期：建卡片实体拿 card_id -> 用 card_id 回复消息 -> 后台协程不停地往
    card_id 的那个 markdown 元素推**全量文本**（sequence 严格递增）
    -> 收尾推最终文本，再调设置接口关掉 streaming_mode。

    三个容易踩错的点：
      - content 是**累计全量**，不是增量；增量由飞书自己算，我们只管把当前完整文本推上去。
      - sequence 必须在整张卡片的生命周期内严格递增，内容更新和最后的设置更新**共用这一个
        计数器**，所以统一走 _next_seq()。
      - 结束时必须调 settings 关流式，否则客户端会一直挂着"生成中"的打字光标。

    为什么推送放在后台协程、而不是写在 token 循环里：一次更新是实打实的 HTTPS 往返
    （实测约 0.6s），在 `async for event in bot.astream(...)` 里 await 它，token 循环这段时间
    是挂起的、消费不了模型输出，token 堆在缓冲区等请求回来一次性吐出——观感就变成
    "冻结一秒多 -> 蹦出一大块字"。所以网络全部挪出主循环，token 侧只做内存累加。
    """

    def __init__(self, client: Any, card_id: str):
        self.client = client
        self.card_id = card_id
        self.trace: list[str] = []
        self.answer = ""
        self.failed = False
        self.started = time.monotonic()
        self._pending_tools: dict[str, list[float]] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._seq = 0
        # 推送帧的成败计数，收尾时打进日志（用来验证会不会撞上飞书的频率限制）
        self.frames_ok = 0
        self.frames_failed = 0
        # 每帧携带的字数增量 + 每帧推送时刻 + 模型最后一个 token 的时刻。
        # 加这三样是为了回答"卡顿/成块"这类观感问题时报得出数字：
        # 到底是帧太密每帧只带几个字，还是表格那段本来就是一次吐一大块。
        self.frame_deltas: list[int] = []
        self.frame_at: list[float] = []
        self.last_token_at = 0.0
        self._last_len = 0
        # 打字机闸门的触发点：停止喂帧时**实际已经推出去**的字数（0 = 没触发，全文都是逐字的）
        self.head_pushed = 0

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

    def on_token(self, text: str) -> None:
        """模型吐字。只做内存累加，绝不在这条路径上 await 网络。"""
        self.answer += text
        self.last_token_at = time.monotonic()

    def _trace_text(self) -> list[str]:
        """截断过长的轨迹：留前 MAX_TRACE_LINES-1 行，其余折叠成一行汇总。"""
        if len(self.trace) <= MAX_TRACE_LINES:
            return self.trace
        hidden = len(self.trace) - (MAX_TRACE_LINES - 1)
        return [*self.trace[: MAX_TRACE_LINES - 1], f"… 另有 {hidden} 条工具记录"]

    def _footer(self) -> str:
        """收尾状态行。放在正文里而不是卡片副标题：副标题属于卡片头，改它要整卡更新。"""
        secs = time.monotonic() - self.started
        if self.failed:
            return f"> ⏱ {secs:.1f}s ｜ 本轮出错了"
        steps = f" ｜ {len(self.trace)} 步工具调用" if self.trace else ""
        return f"> ✅ 完成{steps} ｜ 耗时 {secs:.1f}s"

    def render(self, final: bool = False) -> str:
        """
        把"工具轨迹 + 回答 (+ 状态行)"合成流式元素要的那段完整 Markdown。

        轨迹写成无序列表的一行一项：单个 markdown 组件里裸 \\n 属于软换行，渲染时
        可能被忽略，只有列表项或空行才保证分行——这是卡片 2.0 实测出来的行为。
        """
        blocks: list[str] = []
        if self.trace:
            blocks.append("\n".join(f"- {line}" for line in self._trace_text()))
        blocks.append(self.answer.strip() or "🔍 正在分析，请稍候…")
        if final:
            blocks.append(self._footer())
        return "\n\n".join(blocks)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _snapshot_sig(self) -> tuple[int, int, bool]:
        """内容指纹。用长度当指纹足够：回答只增不减，工具轨迹只追加。"""
        return len(self.answer), len(self.trace), self.failed

    async def _push(self, final: bool) -> None:
        """推一帧。任何异常都只算丢一帧——不能让后台协程因此死掉。"""
        text = self.render(final)
        try:
            ok = await asyncio.to_thread(
                _stream_element, self.client, self.card_id, text, self._next_seq()
            )
        except Exception as exc:  # noqa: BLE001 - 推送失败不该中断本轮回答
            _log(f"[WARN] 流式推送异常：{type(exc).__name__}: {exc}")
            ok = False
        if ok:
            self.frames_ok += 1
            # 只在成功时记账：丢帧会由下一帧一起补上，增量应当合并到那一帧头上
            self.frame_deltas.append(len(text) - self._last_len)
            self._last_len = len(text)
            self.frame_at.append(time.monotonic())
        else:
            self.frames_failed += 1

    async def _patcher(self) -> None:
        """
        后台推送循环：**推完一帧立刻回来看有没有新内容**，不设帧率闸。

        所以帧间距就等于一次更新的网络往返（约 0.55s），是这条通道的物理上限；
        再往下压只能靠并发，而并发会让 sequence 到达顺序失控，不值当。
        只有内容确实没长出来时（多半在等工具返回）才短睡一下，避免空转烧更新额度。

        超过 TYPEWRITER_HEAD_CHARS 就主动退出循环、不再喂帧：客户端逐字速率（约 35 字/秒）
        只有模型出字速率的一半，继续喂只会把积压越攒越多，最后关流式时"啪"地跳完。
        剩下的正文交给 finish() 那一帧一次性带上，观感就是"开头逐字 + 后文整块到位"。
        """
        seen: tuple[int, int, bool] | None = None
        while not self._stopping.is_set():
            if len(self.answer) > TYPEWRITER_HEAD_CHARS:
                self.head_pushed = self._last_len
                return
            sig = self._snapshot_sig()
            if sig == seen:
                with contextlib.suppress(asyncio.TimeoutError):
                    # 用"等停止事件"代替纯 sleep：收尾时能立刻醒来退出，不必白等满一轮
                    await asyncio.wait_for(self._stopping.wait(), timeout=IDLE_POLL_INTERVAL)
                continue
            seen = sig
            await self._push(final=False)

    def start(self) -> None:
        """启动后台推送协程。"""
        self._task = asyncio.create_task(self._patcher())

    async def stop(self) -> None:
        """
        停止后台推送。

        这里要 await 到协程真正退出，而不是 cancel：取消只是在 to_thread 外面抛
        CancelledError，那个已经在跑的请求线程并不会停，可能晚于最终内容才落地、
        把回答盖回一份旧快照。等它跑完这一帧再退就没这个问题。
        """
        if self._task is None:
            return
        self._stopping.set()
        with contextlib.suppress(Exception):
            await self._task
        self._task = None

    async def finish(self) -> None:
        """
        推最终文本，然后关掉流式模式。

        这一帧是**全文快照**：打字机闸门关掉之后，模型剩下的输出全靠它一次带出去。
        紧接着的 settings 调用会把 streaming_mode 关掉，客户端于是停止逐字动画、
        直接把没显示的部分补齐——这正是探针实测到"推完就关的那张卡看起来很快"的原因。
        """
        await self._push(final=True)
        try:
            await asyncio.to_thread(_close_streaming, self.client, self.card_id, self._next_seq())
        except Exception as exc:  # noqa: BLE001 - 关流式失败只是光标停不下来，别影响本轮回答
            _log(f"[WARN] 关闭流式异常：{type(exc).__name__}: {exc}")

    def stats_line(self) -> str:
        """
        收尾用的流式体检：帧数、每帧增量分布、帧间距、末帧滞后，以及打字机闸门有没有触发。

        为什么要这些数：观感上的"卡顿"和"表格一整块蹦出来"有好几种可能——帧太碎、
        客户端打字机比模型慢、或者关流式的时机不对。没有 deltas 分布就只能猜。
        """
        if not self.frame_deltas:
            return "流式推送 0 帧"
        deltas = sorted(self.frame_deltas)
        gaps = [b - a for a, b in zip(self.frame_at, self.frame_at[1:])]
        tail = (self.frame_at[-1] - self.last_token_at) if self.last_token_at else 0.0
        gap_txt = f"｜帧间距 中位 {statistics.median(gaps):.2f}s" if gaps else ""
        head = (
            f"｜逐字只喂了 {self.head_pushed} 字，其余 {deltas[-1]} 字随快照帧补齐"
            if self.head_pushed else "｜全文逐字（未触发打字机闸门）"
        )
        return (
            f"流式 {len(deltas)} 帧（{self.frames_failed} 帧失败）"
            f"｜每帧增量 中位 {deltas[len(deltas) // 2]} 字 / 最大 {deltas[-1]} 字"
            f"{gap_txt}"
            f"｜末帧滞后模型 {tail:.1f}s{head}"
        )


# ---------------------------------------------------------------------------
# 飞书 API 调用（同步 SDK，统一丢到线程里跑，别阻塞事件循环）
# ---------------------------------------------------------------------------

def _api_error(response: Any) -> str:
    """把 SDK 的响应整理成一行错误描述。"""
    return f"code={response.code} msg={response.msg} log_id={response.get_log_id()}"


def _reply_card(client: Any, message_id: str, card: dict) -> str | None:
    """回复一张**内联 JSON** 卡片（不参与流式的两条快速回复用这条路）。"""
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


def _create_card(client: Any, card: dict) -> str | None:
    """创建卡片实体，返回 card_id。流式更新是按实体定位的，所以必须先建它。"""
    from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

    request = (
        CreateCardRequest.builder()
        .request_body(
            CreateCardRequestBody.builder()
            .type("card_json")
            .data(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.cardkit.v1.card.create(request)
    if not response.success():
        _log(f"[ERROR] 创建卡片实体失败：{_api_error(response)}")
        return None
    return response.data.card_id


def _reply_card_entity(client: Any, message_id: str, card_id: str) -> str | None:
    """用 card_id 回复一条卡片消息（content 形状是探针实测出来的唯一可用写法）。"""
    from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
    from lark_oapi.api.im.v1.model.reply_message_request_body import ReplyMessageRequestBody

    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False))
            .msg_type("interactive")
            .build()
        )
        .build()
    )
    response = client.im.v1.message.reply(request)
    if not response.success():
        _log(f"[ERROR] 回复卡片消息失败：{_api_error(response)}")
        return None
    return response.data.message_id


def _stream_element(client: Any, card_id: str, text: str, sequence: int) -> bool:
    """
    往流式元素推一次**全量文本**。

    content 是累计完整文本而不是增量，增量由飞书计算；sequence 必须严格递增，
    失败只当丢一帧（调用方计数），不中断本轮回答。
    """
    import uuid

    from lark_oapi.api.cardkit.v1 import ContentCardElementRequest, ContentCardElementRequestBody

    request = (
        ContentCardElementRequest.builder()
        .card_id(card_id)
        .element_id(STREAM_ELEMENT_ID)
        .request_body(
            ContentCardElementRequestBody.builder()
            .content(text)
            .sequence(sequence)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        .build()
    )
    return client.cardkit.v1.card_element.content(request).success()


def _close_streaming(client: Any, card_id: str, sequence: int) -> None:
    """关掉 streaming_mode。不调这一步，卡片会一直挂着"生成中"的打字光标。"""
    import uuid

    from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

    request = (
        SettingsCardRequest.builder()
        .card_id(card_id)
        .request_body(
            SettingsCardRequestBody.builder()
            .settings(json.dumps({"streaming_mode": False}))
            .sequence(sequence)
            .uuid(str(uuid.uuid4()))
            .build()
        )
        .build()
    )
    response = client.cardkit.v1.card.settings(request)
    if not response.success():
        _log(f"[WARN] 关闭流式模式失败：{_api_error(response)}")


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
            _reply_card, client, message.message_id,
            build_card(body, subtitle="暂不支持", streaming=False),
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
            build_card("已清空本会话的对话历史。", subtitle="已重置", streaming=False),
        )
        return

    # 流式卡片的两步前置：先建卡片实体拿 card_id，再用 card_id 回复消息。
    # 这里**不给卡片头加"正在分析…"副标题**：改副标题要走整卡更新，而流式方案不做整卡更新，
    # 写死它就会答完之后头部一直挂着"正在分析…"。状态统一由正文承载（开头是占位提示，
    # 结尾是 render(final=True) 生成的状态行）。
    card_id = await asyncio.to_thread(_create_card, client, build_card(""))
    if card_id is None:
        return
    if await asyncio.to_thread(_reply_card_entity, client, message.message_id, card_id) is None:
        return

    reply = CardReply(client, card_id)
    reply.start()

    try:
        # 这个循环里刻意不做任何网络 await：卡片交给 reply 的后台协程去推。
        # 只要在这里 await 一次更新，token 流就会被它的往返时间切断一次
        async for event in bot.astream(question, thread_id=thread_id):
            if event.kind == "tool_start":
                reply.on_tool_start(event.tool_name, event.tool_args)
            elif event.kind == "tool_end":
                reply.on_tool_end(event.tool_name, event.ok)
            elif event.kind == "token":
                reply.on_token(event.text)
    except Exception as exc:  # noqa: BLE001 - 单条消息失败不能让机器人退出
        _log(f"[ERROR] 处理提问失败：{type(exc).__name__}: {exc}")
        reply.failed = True
        reply.answer = reply.answer or f"处理这条消息出错了：`{type(exc).__name__}: {exc}`"

    # 先停后台推送，再发最终内容——顺序反了会有一帧旧快照抢在后面盖掉回答
    await reply.stop()
    if not reply.answer.strip():
        reply.answer = "_（本轮没有文字回答）_"
    await reply.finish()
    _log(
        f"已回复（{len(reply.trace)} 条工具记录，{len(reply.answer)} 字）"
        f" | {reply.stats_line()}"
    )


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
