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
