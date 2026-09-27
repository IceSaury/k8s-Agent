"""
控制台编码引导。

两个入口（cli.py、feishu/feishu_bot.py）启动时都要先调用 bootstrap_console()，
且必须在导入 rich 之前：rich 的 Console 在构造时就会探测终端编码，晚了不生效。

解决的问题（仅 Windows）：
  1. 中文 Windows 控制台代码页是 GBK，输出 ✅ / 🔧 / 🤖 会 UnicodeEncodeError 或乱码。
     解法是两步配套：SetConsoleOutputCP(65001) 让控制台按 UTF-8 解释字节，
     再把 Python 的 stdout/stderr 也改成 UTF-8。
  2. 代码页改成 65001 后键盘输入也是 UTF-8 字节，stdin 必须一起改，
     否则中文提问会被按 GBK 解码成乱码。
另外打开 ENABLE_VIRTUAL_TERMINAL_PROCESSING，rich 的配色才会生效。

本模块只依赖标准库，可以安全地在任何位置导入。
"""

from __future__ import annotations

import sys

# ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
_CONSOLE_MODE = 0x0001 | 0x0002 | 0x0004


def bootstrap_console() -> None:
    """把控制台切成 UTF-8 并打开 ANSI 转义。非 Windows 平台是空操作。"""
    if sys.platform != "win32":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        # -11 = STD_OUTPUT_HANDLE。无控制台（如双击运行、服务方式）时调用失败，忽略即可
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), _CONSOLE_MODE)
    except Exception:  # noqa: BLE001 - 尽力而为，失败也不该阻断启动
        pass

    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            # errors="replace"：万一还有编不出的字符，显示成占位符而不是让程序崩掉
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 部分流（如已包装的管道）不支持 reconfigure
            pass
