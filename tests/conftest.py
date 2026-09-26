"""pytest 共享 fixtures。"""

from __future__ import annotations

import collections
import copy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import Config, DEFAULT_CONFIG  # noqa: E402
from pipeline.database import Database  # noqa: E402
from pipeline.disk_manager import DiskManager  # noqa: E402

#: DiskManager 测试阈值（与需求 #12 一致）
DISK_CFG = {
    "safe_start_gb": 30, "pause_new_jobs_gb": 20, "cleanup_gb": 15,
    "emergency_stop_gb": 10, "safety_margin_gb": 8,
    "video_temp_multiplier": 1.5, "audio_temp_multiplier": 0.2,
    "output_multiplier": 1.2,
}


def make_dm(monkeypatch: pytest.MonkeyPatch, watch: Path,
            free_gb: float) -> DiskManager:
    """构造一个磁盘可用空间被 mock 成 free_gb 的 DiskManager。"""
    Usage = collections.namedtuple("Usage", "total used free")
    dm = DiskManager(DISK_CFG, watch_path=watch, work_dir=watch / "work")
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda _p: Usage(50 * 1024 ** 3, 0,
                                         int(free_gb * 1024 ** 3)))
    return dm


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """创建完整的临时项目目录结构。"""
    for d in ("input", "work", "output", "failed", "logs"):
        (tmp_path / d).mkdir()
    return tmp_path


@pytest.fixture()
def cfg(project_dir: Path) -> Config:
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["paths"] = {k: str(project_dir / v.replace("./", ""))
                    for k, v in DEFAULT_CONFIG["paths"].items()}
    # 测试环境通常没有 AI 工具 —— 默认禁用，单测需要时再开
    raw["video_repair"]["enabled"] = False
    raw["audio_repair"]["enabled"] = False
    # 测试磁盘阈值调低，避免被宿主机真实可用空间影响
    raw["disk"] = {"safe_start_gb": 1, "pause_new_jobs_gb": 0.7,
                   "cleanup_gb": 0.4, "emergency_stop_gb": 0.2,
                   "safety_margin_gb": 0.5, "video_temp_multiplier": 1.5,
                   "audio_temp_multiplier": 0.2, "output_multiplier": 1.2}
    return Config(raw=raw, root=project_dir)


@pytest.fixture()
def db(project_dir: Path):
    database = Database(project_dir / "pipeline.db")
    yield database
    database.close()


HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


def make_test_video(path: Path, duration: float = 2.0,
                    size: str = "320x240", fps: int = 24) -> Path:
    """用 FFmpeg 生成一个带音频的测试视频（需要本机有 ffmpeg）。"""
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate={fps}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True, timeout=120)
    return path
