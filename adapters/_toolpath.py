"""工具可执行文件定位辅助。

AI 工具（REAL-Video-Enhancer / DeepFilterNet）安装在各自独立的虚拟环境中，
因此配置里的 `executable` 往往是**绝对路径**而非 PATH 上的命令名。
`shutil.which` 对「绝对路径 + Windows 无扩展名」等场景行为不一致，
这里统一封装，避免各适配器重复实现。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def which_tool(executable: str) -> str | None:
    """定位工具。支持 PATH 命令名与绝对/相对路径两种写法。

    返回可直接用于 subprocess 的路径；找不到返回 None。
    """
    if not executable:
        return None
    has_sep = (os.sep in executable) or bool(os.altsep and os.altsep in executable)
    if has_sep:
        p = Path(executable)
        if p.is_file():
            return str(p)
        # Windows 上省略了 .exe 后缀的绝对路径也做一次兜底
        if os.name == "nt" and not p.suffix:
            for ext in (".exe", ".cmd", ".bat"):
                cand = p.with_suffix(ext)
                if cand.is_file():
                    return str(cand)
        return None
    return shutil.which(executable)


#: 仓库自带的静态 FFmpeg（由 scripts/bootstrap.ps1 第 3 步下载到此目录）
_BUNDLED_FFMPEG_DIR = Path(__file__).resolve().parent.parent / ".tools" / "ffmpeg"


def resolve_bundled(name: str) -> str | None:
    """在仓库自带 .tools/ffmpeg 下找 ffmpeg / ffprobe，找不到返回 None。"""
    exts = ("", ".exe") if os.name == "nt" else ("",)
    for ext in exts:
        cand = _BUNDLED_FFMPEG_DIR / f"{name}{ext}"
        if cand.is_file():
            return str(cand)
    return None


def resolve_ffmpeg(name: str) -> str:
    """解析 ffmpeg / ffprobe：PATH 优先，其次仓库自带的 .tools/ffmpeg。

    为什么需要这层兜底：FFmpegAdapter / FFprobeAdapter 默认用裸命令名
    "ffmpeg"，只有 PATH 上有才找得到。而 PATH 是由启动方式决定的 —— 双击
    run.bat（走 scripts/bootstrap.ps1）会把它加进去，直接 `python main.py run`
    则不会，于是跑到 REPAIR_AUDIO 的第一步就报「找不到可执行文件: ffmpeg」
    （实测一次报废 4 个视频）。这里统一兜底后，任何启动方式都能用到仓库自带
    的 FFmpeg。都找不到时原样返回 name，由调用方给出可读的依赖错误。
    """
    if (os.sep in name) or (os.altsep and os.altsep in name):
        return name  # 显式路径：交给 which_tool / subprocess 自己报错
    return shutil.which(name) or resolve_bundled(name) or name
