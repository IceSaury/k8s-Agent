"""
CLI 交互入口（对应 PDF §4.2 cli.py：交互循环、流式输出、对话历史管理）。

一次会话的流程：
    引导控制台编码 -> 初始化 Agent（连 MCP、加载工具）-> 打印横幅
    -> 循环：读输入 -> 判断是否是内置命令 -> 交给 Agent 流式执行 -> 渲染回答

渲染分三层，都基于 agent.K8sAgent.astream 吐出的事件（见 agent.py 的 AgentEvent）：
    token      -> 累积进缓冲区，用 rich.Live 实时刷新 Markdown 面板（PDF 里的 Agent 框）
    tool_start -> 立即打印「🔧 调用工具: xxx」
    tool_end   -> 立即打印「✅ 工具返回完成」，并附带耗时

内置命令：
    quit / exit / q   退出
    clear             清空当前会话历史（只清对话，不重载工具）
    tools             列出全部可用工具

入口启动时做一次控制台 UTF-8 引导，原因见 console_boot.py。
"""

from __future__ import annotations

import asyncio
import json
import time

from console_boot import bootstrap_console

# 必须在导入 rich 之前执行：Console 构造时会探测终端编码，晚了就不生效
bootstrap_console()

from rich.console import Console  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from agent import DEFAULT_THREAD_ID, K8sAgent  # noqa: E402
from config import settings  # noqa: E402

# 流式渲染的最小刷新间隔（秒）。qwen-plus 出字很快，逐 token 重排 Markdown 会拖慢终端
_RENDER_INTERVAL = 0.08

# 工具参数显示的截断长度，太长会把终端刷满
_ARGS_LIMIT = 80

console = Console()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    """启动横幅，样式对齐 PDF §1.4 的终端截图。"""
    body = Text.assemble(
        ("K8s 运维助手 Agent\n", "bold"),
        ("基于 LangChain 1.2 + MCP\n\n", "dim"),
        ("命令: ", "dim"),
        ("quit", "cyan"),
        (" 退出 | ", "dim"),
        ("clear", "cyan"),
        (" 清空历史 | ", "dim"),
        ("tools", "cyan"),
        (" 查看可用工具", "dim"),
    )
    console.print(Panel(body, title="🤖", border_style="cyan", padding=(1, 2)))


def _print_loaded(label: str, names: list[str]) -> None:
    """打印「✅ 已加载 N 个 xxx: [...]」。用 Text 拼装，避免 list 里的符号被当成 rich 标记。"""
    # 中文标签紧跟"个"不留空格，英文/数字开头（K8s、MCP）留一个，读起来才自然
    gap = " " if label[:1].isascii() else ""
    line = Text("  ✅ 已加载 ")
    line.append(f"{len(names)} 个{gap}{label}: ")
    line.append(str(names), style="dim")
    console.print(line)


def _answer_panel(text: str) -> Panel:
    """回答面板：内容按 Markdown 渲染，表格/代码块/列表都能正常显示。"""
    return Panel(Markdown(text), title="Agent", border_style="green", padding=(0, 1))


def _format_args(args: dict) -> str:
    """把工具参数压成一行短字符串用于展示。"""
    try:
        raw = json.dumps(args, ensure_ascii=False)
    except TypeError:  # 参数里出现不可序列化对象时兜底
        raw = str(args)
    return raw if len(raw) <= _ARGS_LIMIT else raw[: _ARGS_LIMIT - 3] + "..."


def _print_tools(bot: K8sAgent) -> None:
    """tools 命令：表格列出全部工具，标注来源。"""
    k8s_names, search_names = bot.tool_groups()

    table = Table(title="可用工具", title_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("工具名", style="cyan")
    table.add_column("来源")

    for index, name in enumerate(k8s_names, 1):
        table.add_row(str(index), name, f"MCP · {bot.k8s_source}")
    for index, name in enumerate(search_names, len(k8s_names) + 1):
        table.add_row(str(index), name, f"本地 · {settings.search_provider}")

    console.print(table)


def _print_warnings() -> None:
    """展示配置阶段的提醒。飞书凭证与 CLI 无关，这里跳过。"""
    for message in settings.warnings:
        if "飞书" in message:
            continue
        line = Text("  ⚠️  ", style="yellow")
        line.append(message, style="yellow")
        console.print(line)


# ---------------------------------------------------------------------------
# 一轮对话
# ---------------------------------------------------------------------------

async def run_turn(bot: K8sAgent, user_input: str) -> None:
    """
    执行一轮对话并渲染过程。

    工具调用在回答生成之前发生，所以先出现的 🔧/✅ 行会落在 Live 面板上方：
    rich 的 Live 会把同时期的 console.print 输出推到面板之上，不会打断正在刷新的面板。
    """
    buffer = ""
    live: Live | None = None
    last_render = 0.0
    # 工具名 -> 各次调用的开始时间。同一工具可能被并行调用多次，用列表按先入先出配对
    pending: dict[str, list[float]] = {}

    try:
        async for event in bot.astream(user_input, thread_id=DEFAULT_THREAD_ID):
            if event.kind == "tool_start":
                pending.setdefault(event.tool_name, []).append(time.monotonic())
                line = Text("  🔧 调用工具: ")
                line.append(event.tool_name, style="yellow")
                if event.tool_args:
                    line.append(f"  {_format_args(event.tool_args)}", style="dim")
                console.print(line)

            elif event.kind == "tool_end":
                started = pending.get(event.tool_name) or []
                line = Text("     ")
                if event.ok:
                    line.append("✅ 工具返回完成", style="green")
                else:
                    line.append("❌ 工具返回失败", style="red")
                if started:
                    line.append(f"  ({time.monotonic() - started.pop(0):.1f}s)", style="dim")
                console.print(line)

            elif event.kind == "token":
                buffer += event.text
                now = time.monotonic()
                # 非终端（重定向到文件）时不开 Live：Live 会反复整体重画，日志会被刷爆
                if live is None and console.is_terminal:
                    live = Live(
                        _answer_panel(buffer),
                        console=console,
                        refresh_per_second=12,
                        vertical_overflow="visible",
                    )
                    live.start()
                    last_render = now
                elif live is not None and now - last_render >= _RENDER_INTERVAL:
                    live.update(_answer_panel(buffer))
                    last_render = now

            # kind == "done" 无需处理，循环结束统一收尾

    finally:
        if live is not None:
            # 补最后一帧（中断时也能看到已生成的部分），stop() 会把这一帧留在终端上
            live.update(_answer_panel(buffer))
            live.stop()

    # 没开 Live 的场合（重定向到文件、非常规终端）在这里一次性打印，开了的不能再打一遍
    if live is None:
        if buffer.strip():
            console.print(_answer_panel(buffer))
        else:
            console.print("  [dim](本轮没有文字回答)[/dim]")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def main() -> None:
    console.print("正在初始化 Agent...")
    try:
        bot = await K8sAgent.create()
    except Exception as exc:  # noqa: BLE001 - 初始化失败要给出可读提示而不是堆栈
        console.print(f"[bold red]Agent 初始化失败：[/bold red]{type(exc).__name__}: {exc}")
        return

    k8s_names, search_names = bot.tool_groups()
    _print_loaded("K8s 工具", k8s_names)
    _print_loaded("搜索工具", search_names)
    console.print(Text(f"  ✅ Agent 初始化完成  (LLM: {settings.llm_model})", style="green"))
    _print_warnings()
    _print_banner()

    while True:
        try:
            user_input = console.input("\n[bold cyan]You:[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):  # Ctrl+C / Ctrl+D 都视作退出
            console.print()
            break

        if not user_input:
            continue

        # 终端里用户敲回车会回显换行；管道/重定向输入没有回显，补一个免得输出挤在同一行
        if not console.is_terminal:
            console.print()

        command = user_input.lower()
        if command in ("quit", "exit", "q"):
            break

        if command == "clear":
            bot.reset_memory(DEFAULT_THREAD_ID)
            console.print(Text("  ✅ 已清空对话历史", style="green"))
            continue

        if command == "tools":
            _print_tools(bot)
            continue

        try:
            await run_turn(bot, user_input)
        except KeyboardInterrupt:
            console.print(Text("\n  ⚠️  已中断本轮回答", style="yellow"))
        except Exception as exc:  # noqa: BLE001 - 单轮失败不应终止整个会话
            console.print(Text(f"  ❌ 本轮执行出错: {type(exc).__name__}: {exc}", style="red"))

    console.print("👋 再见")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n👋 再见")
