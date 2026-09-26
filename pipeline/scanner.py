"""输入文件扫描（需求 #24）。

规则：
- 按扩展名做粗筛，真实格式必须经 ffprobe 确认（不能只看扩展名）；
- 已入库的文件不重复建任务；
- input/ 源文件只读，绝不修改、绝不删除。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .database import Database
from .state_machine import Job

log = logging.getLogger("pipeline.scanner")


class Scanner:
    def __init__(self, input_dir: Path, extensions: list[str], db: Database,
                 include: list[str] | None = None) -> None:
        self.input_dir = Path(input_dir)
        self.extensions = {e.lower() for e in extensions}
        self.db = db
        #: 只处理这部分子目录（相对 input/ 的路径）；空 = 全部
        self.include = [str(i).replace("\\", "/").strip("/")
                        for i in (include or []) if str(i).strip("/")]

    def _included(self, path: Path) -> bool:
        """判断文件是否落在 include 指定的子目录内。"""
        if not self.include:
            return True
        try:
            rel = path.relative_to(self.input_dir).as_posix()
        except ValueError:
            return False
        return any(rel == inc or rel.startswith(inc + "/") for inc in self.include)

    def discover_files(self) -> list[Path]:
        """递归扫描 input 目录，按扩展名粗筛（可用 include 限定子目录）。"""
        if not self.input_dir.exists():
            log.warning("input 目录不存在: %s", self.input_dir)
            return []
        files = [p for p in sorted(self.input_dir.rglob("*"))
                 if p.is_file() and p.suffix.lower() in self.extensions
                 and self._included(p)]
        if self.include:
            log.info("扫描到 %d 个候选文件（限定子目录: %s）",
                     len(files), ", ".join(self.include))
        else:
            log.info("扫描到 %d 个候选文件", len(files))
        return files

    def scan(self) -> list[Job]:
        """扫描并建立任务记录。返回本次新建的任务列表。"""
        created: list[Job] = []
        for path in self.discover_files():
            try:
                size = path.stat().st_size
            except OSError as exc:
                log.error("无法读取文件 %s: %s", path, exc)
                continue
            if size == 0:
                log.warning("跳过空文件: %s", path)
                continue
            job = self.db.add_job(str(path.resolve()), size)
            if job is not None:
                created.append(job)
                log.info("新任务 job=%d: %s (%.2f GB)",
                         job.job_id, path.name, size / 1024 ** 3)
        return created
