"""磁盘管理器测试（需求 #12 / #27）。用 mock 磁盘用量，不依赖真实磁盘。"""

from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from pipeline.disk_manager import DiskManager, DiskState
from pipeline.errors import EmergencyStopError

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3

CFG = {
    "safe_start_gb": 30, "pause_new_jobs_gb": 20, "cleanup_gb": 15,
    "emergency_stop_gb": 10, "safety_margin_gb": 8,
    "video_temp_multiplier": 1.5, "audio_temp_multiplier": 0.2,
    "output_multiplier": 1.2,
}


def make_dm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
            free_gb: float) -> DiskManager:
    dm = DiskManager(CFG, watch_path=tmp_path, work_dir=tmp_path / "work")
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda _p: Usage(50 * GB, 0, int(free_gb * GB)))
    return dm


class TestDiskStates:
    @pytest.mark.parametrize("free_gb,expected", [
        (45, DiskState.NORMAL),
        (30, DiskState.NORMAL),
        (25, DiskState.CONTROLLED),
        (18, DiskState.CONSERVATIVE),
        (12, DiskState.EMERGENCY_CLEANUP),
        (5, DiskState.HALT),
    ])
    def test_thresholds(self, monkeypatch, tmp_path, free_gb, expected) -> None:
        assert make_dm(monkeypatch, tmp_path, free_gb).state() is expected

    def test_can_start_new_job(self, monkeypatch, tmp_path) -> None:
        assert make_dm(monkeypatch, tmp_path, 40).can_start_new_job()
        assert not make_dm(monkeypatch, tmp_path, 25).can_start_new_job()

    def test_halt_raises(self, monkeypatch, tmp_path) -> None:
        with pytest.raises(EmergencyStopError):
            make_dm(monkeypatch, tmp_path, 5).assert_not_halted()


class TestSpaceEstimation:
    def test_estimate(self, monkeypatch, tmp_path) -> None:
        dm = make_dm(monkeypatch, tmp_path, 50)
        # 源 8GB：8*1.5 + 8*0.2 + 8*1.2 + 8 = 31.2 GB
        est = dm.estimate_required_gb(8 * GB)
        assert est == pytest.approx(31.2)

    def test_has_space_for(self, monkeypatch, tmp_path) -> None:
        dm = make_dm(monkeypatch, tmp_path, 40)
        ok, required, free = dm.has_space_for(2 * GB)
        assert ok and free == pytest.approx(40)
        dm_low = make_dm(monkeypatch, tmp_path, 10.5)
        ok, _, _ = dm_low.has_space_for(8 * GB)
        assert not ok
