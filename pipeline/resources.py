"""资源监控：RAM / CPU / GPU VRAM / 磁盘（需求 #14、#15）。

GPU 信息通过 `nvidia-smi` 获取；Intel Arc 无通用 CLI，
用 QSV 可用性（ffmpeg -encoders 中的 hevc_qsv）作为其在线信号。
任何探测失败都不允许导致崩溃 —— 降级为"未知"，由调度器保守处理。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

import psutil

log = logging.getLogger("pipeline.resources")


@dataclass
class ResourceSnapshot:
    ram_percent: float          # 0-100
    ram_free_gb: float
    cpu_percent: float
    gpu_vram_used_mb: float | None   # None = 无法探测
    gpu_vram_total_mb: float | None
    gpu_util_percent: float | None
    disk_free_gb: float


class ResourceMonitor:
    def __init__(self, cfg: dict, disk_watch_path) -> None:
        self.ram_soft_limit = cfg.get("ram_soft_limit_percent", 80)
        self.ram_hard_limit = cfg.get("ram_hard_limit_percent", 92)
        self.disk_watch_path = disk_watch_path

    # ------------------------------------------------------------------ #
    def snapshot(self) -> ResourceSnapshot:
        vm = psutil.virtual_memory()
        disk = __import__("shutil").disk_usage(self.disk_watch_path)
        used_mb, total_mb, util = self._nvidia_gpu()
        return ResourceSnapshot(
            ram_percent=vm.percent,
            ram_free_gb=vm.available / (1024 ** 3),
            cpu_percent=psutil.cpu_percent(interval=0.1),
            gpu_vram_used_mb=used_mb,
            gpu_vram_total_mb=total_mb,
            gpu_util_percent=util,
            disk_free_gb=disk.free / (1024 ** 3),
        )

    @staticmethod
    def _nvidia_gpu() -> tuple[float | None, float | None, float | None]:
        """查询第一块 NVIDIA GPU。失败返回 (None, None, None)。"""
        if not shutil.which("nvidia-smi"):
            return None, None, None
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=False)
            if out.returncode != 0:
                return None, None, None
            first = out.stdout.strip().splitlines()[0]
            used, total, util = (float(x.strip()) for x in first.split(","))
            return used, total, util
        except Exception as exc:  # noqa: BLE001 —— 探测绝不允许崩溃
            log.debug("nvidia-smi 探测失败: %s", exc)
            return None, None, None

    # ------------------------------------------------------------------ #
    def ram_allows_new_stage(self) -> bool:
        """RAM > 软限制禁止启动新阶段。"""
        return psutil.virtual_memory().percent < self.ram_soft_limit

    def ram_critical(self) -> bool:
        """RAM > 硬限制（默认 92%）—— 需要暂停/降级。"""
        return psutil.virtual_memory().percent >= self.ram_hard_limit

    def vram_free_gb(self) -> float | None:
        used, total, _ = self._nvidia_gpu()
        if used is None or total is None:
            return None
        return (total - used) / 1024.0
