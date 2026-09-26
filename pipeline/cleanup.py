"""临时文件清理（需求 #12 / #15）。

红线：
- 绝不删除 input 源文件；
- 绝不删除 output 已验证文件；
- 只清理 work/ 目录下确认无用的中间产物。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("pipeline.cleanup")

#: 可安全删除的临时文件后缀
_TEMP_SUFFIXES = {".tmp", ".partial", ".wav"}


def delete_if_exists(path: Path, logger: logging.Logger | None = None) -> bool:
    lg = logger or log
    p = Path(path)
    try:
        if p.exists() and p.is_file():
            size = p.stat().st_size
            p.unlink()
            lg.info("删除临时文件 %s (%.2f MB)", p.name, size / 1024 ** 2)
            return True
    except OSError as exc:
        lg.warning("删除失败 %s: %s", p, exc)
    return False


class Cleaner:
    """管理单个工作目录(work/current)的生命周期。"""

    def __init__(self, work_root: Path, input_dir: Path, output_dir: Path) -> None:
        self.work_root = Path(work_root)
        self.current = self.work_root / "current"
        self.input_dir = Path(input_dir).resolve()
        self.output_dir = Path(output_dir).resolve()

    # ------------------------------------------------------------------ #
    def prepare_current(self) -> Path:
        self.current.mkdir(parents=True, exist_ok=True)
        return self.current

    def is_safe_to_delete(self, path: Path) -> bool:
        """只有 work/current 内的文件才允许自动删除。"""
        try:
            p = Path(path).resolve()
            return self.current.resolve() in p.parents or p == self.current.resolve()
        except OSError:
            return False

    def cleanup_current(self, logger: logging.Logger | None = None) -> None:
        """任务完成后清空 work/current（需求 I：完成后必须接近空目录）。"""
        lg = logger or log
        if not self.current.exists():
            return
        for p in sorted(self.current.rglob("*"), reverse=True):
            if not self.is_safe_to_delete(p):
                lg.error("拒绝删除工作区外文件: %s", p)
                continue
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            except OSError as exc:
                lg.warning("清理 %s 失败: %s", p, exc)

    def emergency_cleanup(self, logger: logging.Logger | None = None) -> int:
        """紧急清理：删除 work 下所有 .tmp/.partial/.wav，返回释放字节数。

        只作用于 work/ 目录 —— input/ 与 output/ 绝不触碰。
        """
        lg = logger or log
        freed = 0
        if not self.work_root.exists():
            return 0
        for p in self.work_root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in _TEMP_SUFFIXES:
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    lg.info("紧急清理: %s", p)
                except OSError as exc:
                    lg.warning("紧急清理失败 %s: %s", p, exc)
        return freed
