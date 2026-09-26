"""分层日志（需求 #22）。

logs/
├── pipeline.log      全局
├── scheduler.log     调度器
└── jobs/<job_id>.log 单任务详细日志（含第三方命令 stdout/stderr）
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _file_handler(path: Path, level: int = logging.DEBUG) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=20 * 1024 * 1024, backupCount=3,
                            encoding="utf-8")
    h.setFormatter(logging.Formatter(_FMT))
    h.setLevel(level)
    return h


def setup_logging(log_dir: Path, console_level: int = logging.INFO) -> None:
    """初始化全局日志。幂等：重复调用不会叠加 handler。"""
    log_dir = Path(log_dir)
    root = logging.getLogger()
    if getattr(root, "_pipeline_logging_ready", False):
        return
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    console.setLevel(console_level)
    root.addHandler(console)
    root.addHandler(_file_handler(log_dir / "pipeline.log"))

    sched = logging.getLogger("pipeline.scheduler")
    sched.addHandler(_file_handler(log_dir / "scheduler.log"))

    root._pipeline_logging_ready = True  # type: ignore[attr-defined]


def get_job_logger(log_dir: Path, job_id: int) -> logging.Logger:
    """每个任务独立日志文件：logs/jobs/<job_id>.log"""
    logger = logging.getLogger(f"pipeline.job.{job_id}")
    if not logger.handlers:
        logger.addHandler(_file_handler(Path(log_dir) / "jobs" / f"{job_id:04d}.log"))
        logger.setLevel(logging.DEBUG)
    return logger


def job_log_path(log_dir: Path, job_id: int) -> Path:
    return Path(log_dir) / "jobs" / f"{job_id:04d}.log"
