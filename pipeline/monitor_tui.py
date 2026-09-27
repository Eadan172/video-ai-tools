"""终端实时监控（TUI）。

在 PowerShell / cmd 里原地重绘一张多文件进度表，无需浏览器。

要点
----
* 重绘用 ``ESC[H ESC[J``（光标归位 + 清到屏尾），不用 ``cls``——后者会闪屏、
  清掉 scrollback，且每次都要 fork 一个进程。
* 中文本身 GBK 可表示，真正会乱码的是 NODE_MAP 里的 ``✓ ✕ ▶ ⏸ ↻``，
  统一经 ``progress.safe_icon()`` 降级；若控制台编码仍非 UTF-8，
  再降一级到纯 ASCII（进度条 ``#``/``-``、节点 ``[x]/[>]/[ ]``）。
* ``--once`` 打一帧即退，且**不带任何转义序列**，便于重定向留档与脚本断言。
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
import unicodedata

from . import progress as P
from .config import load_config

log = logging.getLogger("pipeline.monitor")

_HOME_CLEAR = "\x1b[H\x1b[J"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"

#: 状态 → ANSI 颜色（16 色，兼容性优先）
_COLORS = {
    "waiting": "90",    # 灰
    "resource": "35",   # 品红
    "running": "96",    # 亮青
    "retry": "33",      # 黄
    "done": "92",       # 绿
    "failed": "91",     # 亮红
}

_DEFAULT_WIDTH = 104


# --------------------------------------------------------------------------- #
# 终端适配
# --------------------------------------------------------------------------- #
def _try_enable_utf8() -> None:
    """尽量把控制台切到 UTF-8；失败就静默回退。

    刻意不用 ``os.system("chcp 65001")``——那会闪一个窗口并且改掉全局代码页。
    """
    if not sys.stdout.isatty():
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001 —— 非 Windows 或缺 API 都无所谓
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _detect_ascii_mode(forced: bool) -> bool:
    if forced:
        return True
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" not in enc


def _use_color(no_color: bool) -> bool:
    if no_color or not sys.stdout.isatty():
        return False
    try:
        import colorama
        colorama.just_fix_windows_console()   # 只开 VT，不包裹 stdout
    except Exception:  # noqa: BLE001 —— 没有 colorama 也能用裸 ANSI
        pass
    return True


def _dw(text: str) -> int:
    """终端显示宽度（CJK 记 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度截断 / 补空格。"""
    if _dw(text) > width:
        out, w = "", 0
        for ch in text:
            cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if w + cw > width - 3:
                break
            out += ch
            w += cw
        text = out + "..."
    gap = " " * max(width - _dw(text), 0)
    return gap + text if align == "right" else text + gap


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _glyphs(ascii_mode: bool) -> dict[str, str]:
    if ascii_mode:
        return {"fill": "#", "empty": "-", "line": "-", "cursor": ">"}
    return {"fill": "█", "empty": "░", "line": "─", "cursor": ">"}


def _colorize(text: str, key: str, color: bool) -> str:
    if not color:
        return text
    code = _COLORS.get(key)
    return f"\x1b[{code}m{text}\x1b[0m" if code else text


def build_frame(s: P.Snapshot, limit: int = 20, color: bool = True,
                ascii_mode: bool = False, width: int = _DEFAULT_WIDTH) -> str:
    """把快照渲染成一帧文本（不含转义序列之外的重绘控制码）。"""
    g = _glyphs(ascii_mode)
    line = g["line"] * width
    out: list[str] = []

    # ---------- 顶部汇总 ----------
    out.append(_pad("视频处理实时监控", width - 12)
               + _pad(f"刷新 {int(_interval_hint)}s", 12, "right"))
    out.append(line)
    head = (f"总数 {s.total}   完成 {s.done}   运行 {s.running}   "
            f"等待 {s.waiting}   失败 {s.failed}")
    eta = P.human_eta(s.queue_eta_s)
    out.append(_pad(head, width - 26) + _pad(f"队列剩余 {eta}", 26, "right"))
    res = []
    if s.disk_free_gb is not None:
        res.append(f"磁盘剩余 {s.disk_free_gb:.1f} GB")
    if s.ram_percent is not None:
        res.append(f"内存 {s.ram_percent:.0f}%")
    res.append(f"阶段模型 {P.human_duration(s.model_age_s)}前更新")
    out.append(_pad("   ".join(res), width))
    if s.db_missing:
        out.append(_colorize("未找到数据库（尚未扫描或调度器未启动）",
                             "failed", color))
    out.append(line)

    # ---------- 队列表格 ----------
    w_name, w_node, w_pct, w_eta, w_info = 24, 18, 8, 10, 16
    w_bar = max(width - (4 + w_name + w_node + w_pct + w_eta + w_info), 10)
    out.append(_pad("#", 4) + _pad("文件名", w_name) + _pad("当前节点", w_node)
               + _pad("进度条", w_bar) + _pad("百分比", w_pct, "right")
               + _pad("剩余", w_eta, "right") + "  " + _pad("信息", w_info))

    shown = 0
    for v in s.jobs:
        if shown >= limit:
            break
        shown += 1
        if v.percent is None:
            bar = g["empty"] * w_bar
            pct = "-"
        else:
            bar = P.format_bar(v.percent, w_bar, g["fill"], g["empty"])
            pct = f"{v.percent:.0f}%"
        icon = P.safe_icon(v.node_icon, ascii_mode)
        node = f"{icon} {v.node_label}"
        info = ""
        if v.retry_count:
            info = f"重试 {v.retry_count} 次"
        elif v.speed:
            info = f"{v.speed:.2f}x 实时"
        elif v.queue_pos:
            info = f"队列第 {v.queue_pos} 位"
        eta_txt = "" if v.percent is None else P.human_eta(v.eta_s)
        row = (_pad(str(v.job_id), 4) + _pad(v.name, w_name)
               + _pad(node, w_node) + _pad(bar, w_bar)
               + _pad(pct, w_pct, "right") + _pad(eta_txt, w_eta, "right")
               + "  " + _pad(info, w_info))
        out.append(_colorize(row, v.style_key, color))
    if len(s.jobs) > shown:
        out.append(f"  ... 其余 {len(s.jobs) - shown} 个已省略（--limit 调整）")
    out.append(line)

    # ---------- 当前作业的阶段时间线 ----------
    cur = next((v for v in s.jobs if v.style_key == "running"), None)
    if cur is None:
        # 没有在跑的作业时，退而展示最近一个失败/重试的作业，便于定位问题
        cur = next((v for v in s.jobs
                    if v.style_key in ("retry", "failed")), None)
    if cur is not None:
        detail = f"当前作业 job{cur.job_id} {cur.name}  [{cur.node_label}]"
        if cur.duration_s:
            detail += f"   片源 {P.human_duration(cur.duration_s)}"
        if cur.elapsed_s:
            detail += f"   已用 {P.human_duration(cur.elapsed_s)}"
        if cur.speed:
            detail += f"   速度 {cur.speed:.2f}x"
        out.append(_pad(detail, width))
        for n in cur.timeline:
            mark = {"done": "[x]", "current": "[>]", "failed": "[!]",
                    "skipped": "[-]", "pending": "[ ]"}.get(n.state, "[ ]")
            text = f"   {mark} {_pad(n.label, 18)} {n.note}"
            out.append(_colorize(text, "running" if n.state == "current"
                                 else ("done" if n.state == "done" else "waiting"),
                                 color))
        # 错误摘要只对「重试等待 / 失败」有意义；运行中的作业带的是
        # 上一次尝试的旧错误，显示出来只会误导。
        if cur.error and cur.style_key in ("retry", "failed"):
            out.append(_colorize(f"   最近错误: {cur.error[:80]}", "failed",
                                 color))
    out.append(line)

    # ---------- 页脚 ----------
    out.append(_pad(
        f"Ctrl+C 退出   --once 打印一帧   --json 输出 JSON   "
        f"数据时刻 {time.strftime('%H:%M:%S')}", width))
    return "\n".join(out)


#: build_frame 里页脚要显示刷新间隔，由 run_tui 注入（避免多传一个参数）
_interval_hint: float = 5.0


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #
def _load(cfg_path: str):
    return load_config(cfg_path)


def run_tui(cfg_path: str = "config.yaml", interval: float | None = None,
            limit: int = 20, once: bool = False, as_json: bool = False,
            ascii_mode: bool = False, no_color: bool = False) -> int:
    """TUI 主循环。返回进程退出码。"""
    global _interval_hint
    cfg = _load(cfg_path)
    dash = (cfg.raw or {}).get("dashboard", {}) or {}
    if interval is None:
        interval = float(dash.get("tui_interval_seconds", 5))
    interval = max(interval, 0.5)
    _interval_hint = interval

    if once:
        s = P.build_snapshot(cfg)
        if as_json:
            print(json.dumps(P.snapshot_to_dict(s), ensure_ascii=False,
                             indent=2))
        else:
            # 单帧输出保持纯净：不打转义序列、不自动上色
            print(build_frame(s, limit=limit, color=False,
                              ascii_mode=_detect_ascii_mode(ascii_mode)))
        return 0

    _try_enable_utf8()
    ascii_mode = _detect_ascii_mode(ascii_mode)   # 可能因 reconfigure 变化
    color = _use_color(no_color)
    width = max(shutil.get_terminal_size((_DEFAULT_WIDTH, 30)).columns, 80)

    sys.stdout.write(_HIDE_CURSOR)
    try:
        while True:
            s = P.build_snapshot(cfg)
            frame = build_frame(s, limit=limit, color=color,
                                ascii_mode=ascii_mode, width=width)
            sys.stdout.write(_HOME_CLEAR + frame)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(_SHOW_CURSOR + "\n" + _HOME_CLEAR)
        sys.stdout.flush()


def add_cli(sub) -> None:
    """在 main.py 的 subparsers 上注册 monitor 子命令。"""
    m = sub.add_parser("monitor", help="终端实时监控（原地重绘）")
    m.add_argument("--interval", type=float, default=None,
                   help="刷新间隔秒数（默认取 config 的 dashboard.tui_interval_seconds）")
    m.add_argument("--limit", type=int, default=20,
                   help="最多显示多少个作业（默认 20）")
    m.add_argument("--once", action="store_true",
                   help="只打印一帧就退出（输出不含控制字符，便于重定向）")
    m.add_argument("--json", action="store_true",
                   help="配合 --once，输出 JSON 而非表格")
    m.add_argument("--ascii", action="store_true",
                   help="强制 ASCII 字形（图标与进度条降级）")
    m.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
    m.set_defaults(_handler=_cmd)


def _cmd(args) -> int:
    return run_tui(args.config, interval=args.interval, limit=args.limit,
                   once=args.once, as_json=args.json,
                   ascii_mode=args.ascii, no_color=args.no_color)