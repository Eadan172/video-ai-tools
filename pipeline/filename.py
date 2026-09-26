"""Windows 安全文件名处理（需求 #21）。

- 保留中文字符；
- 剔除 Windows 非法字符 < > : " / \\ | ? *；
- 保证同名文件不互相覆盖：name.mp4 / name_1.mp4 / name_2.mp4 ...
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")
# Windows 保留设备名
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """把任意文件名清洗成 Windows 安全文件名（不含扩展名部分也可传入）。"""
    name = unicodedata.normalize("NFC", name)
    name = _INVALID.sub("_", name)
    name = _SPACES.sub(" ", name).strip(" .")  # 结尾空格/点在 Windows 非法
    if not name:
        name = "unnamed"
    if name.upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name


def unique_output_path(directory: Path, stem: str, suffix: str) -> Path:
    """生成不与现有文件冲突的输出路径。"""
    directory = Path(directory)
    stem = sanitize_filename(stem)
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    n = 0
    while candidate.exists():
        n += 1
        candidate = directory / f"{stem}_{n}{suffix}"
    return candidate
