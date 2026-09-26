"""磁盘空间管理 —— 本项目的第一优先级调度约束（需求 #12 / #27）。

阈值语义（可用空间 free）：
    free >= safe_start_gb          允许启动新任务
    pause_new_jobs_gb <= free < safe_start_gb   只继续当前任务
    cleanup_gb <= free < pause_new_jobs_gb      暂停新任务，预防性清理
    emergency_stop_gb <= free < cleanup_gb      EMERGENCY_CLEANUP
    free < emergency_stop_gb       紧急停止整条流水线
"""

from __future__ import annotations

import enum
import logging
import shutil
from pathlib import Path

from .errors import EmergencyStopError

log = logging.getLogger("pipeline.disk")


class DiskState(enum.Enum):
    NORMAL = "NORMAL"                   # 允许新任务
    CONTROLLED = "CONTROLLED"           # 只继续当前任务
    CONSERVATIVE = "CONSERVATIVE"       # 暂停新任务
    EMERGENCY_CLEANUP = "EMERGENCY_CLEANUP"
    HALT = "HALT"                       # 停止流水线


class DiskManager:
    def __init__(self, cfg: dict, watch_path: Path, work_dir: Path) -> None:
        self.safe_start_gb: float = cfg.get("safe_start_gb", 30)
        self.pause_new_jobs_gb: float = cfg.get("pause_new_jobs_gb", 20)
        self.cleanup_gb: float = cfg.get("cleanup_gb", 15)
        self.emergency_stop_gb: float = cfg.get("emergency_stop_gb", 10)
        self.safety_margin_gb: float = cfg.get("safety_margin_gb", 8)
        self.video_temp_multiplier: float = cfg.get("video_temp_multiplier", 1.5)
        self.audio_temp_multiplier: float = cfg.get("audio_temp_multiplier", 0.2)
        self.output_multiplier: float = cfg.get("output_multiplier", 1.2)
        self.watch_path = Path(watch_path)
        self.work_dir = Path(work_dir)

    # ------------------------------------------------------------------ #
    def free_gb(self) -> float:
        usage = shutil.disk_usage(self.watch_path)
        return usage.free / (1024 ** 3)

    def workspace_size_gb(self) -> float:
        total = 0
        if self.work_dir.exists():
            for p in self.work_dir.rglob("*"):
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        return total / (1024 ** 3)

    def state(self) -> DiskState:
        free = self.free_gb()
        if free < self.emergency_stop_gb:
            return DiskState.HALT
        if free < self.cleanup_gb:
            return DiskState.EMERGENCY_CLEANUP
        if free < self.pause_new_jobs_gb:
            return DiskState.CONSERVATIVE
        if free < self.safe_start_gb:
            return DiskState.CONTROLLED
        return DiskState.NORMAL

    def can_start_new_job(self) -> bool:
        return self.state() is DiskState.NORMAL

    def assert_not_halted(self) -> None:
        st = self.state()
        if st is DiskState.HALT:
            raise EmergencyStopError(
                f"磁盘可用空间低于紧急阈值 {self.emergency_stop_gb}GB，流水线停止")

    # ------------------------------------------------------------------ #
    # 空间预估（需求 #27：只是防止明显不足，运行时仍需实时监控）
    # ------------------------------------------------------------------ #
    def estimate_required_gb(self, source_size_bytes: int) -> float:
        src_gb = source_size_bytes / (1024 ** 3)
        required = (src_gb * self.video_temp_multiplier
                    + src_gb * self.audio_temp_multiplier
                    + src_gb * self.output_multiplier
                    + self.safety_margin_gb)
        return required

    def has_space_for(self, source_size_bytes: int) -> tuple[bool, float, float]:
        """返回 (是否足够, 需要GB, 实际可用GB)。"""
        required = self.estimate_required_gb(source_size_bytes)
        free = self.free_gb()
        return (free >= required and self.can_start_new_job(), required, free)
