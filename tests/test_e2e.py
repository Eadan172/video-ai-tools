"""端到端测试：真实 FFmpeg 处理一个生成的测试视频（AI 功能禁用）。

验证完整链路：scan → run → DONE → output 校验通过 → work/current 清空。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import HAS_FFMPEG, HAS_FFPROBE, make_test_video
from pipeline.database import Database
from pipeline.scanner import Scanner
from pipeline.scheduler import Scheduler
from pipeline.state_machine import JobStatus, Stage

pytestmark = pytest.mark.skipif(not (HAS_FFMPEG and HAS_FFPROBE),
                                reason="需要 ffmpeg/ffprobe")


class TestEndToEnd:
    def test_full_pipeline(self, cfg, db, project_dir) -> None:
        src = make_test_video(project_dir / "input" / "第01讲：测试.mp4",
                              duration=1.0)
        scanner = Scanner(cfg.paths["input"],
                          cfg.raw["input"]["extensions"], db)
        created = scanner.scan()
        assert len(created) == 1

        scheduler = Scheduler(cfg, db)
        scheduler.poll_interval = 0.1
        scheduler.run()

        job = db.get_job(created[0].job_id)
        assert job.status is JobStatus.DONE
        out = Path(job.output_path)
        assert out.exists()
        # 中文文件名安全清洗
        assert ":" not in out.name
        # 需求 I：work/current 必须接近空目录
        current = cfg.paths["work"] / "current"
        leftovers = list(current.rglob("*")) if current.exists() else []
        assert leftovers == []
        # input 源文件未被修改
        assert src.exists()

    def test_resume_after_interruption(self, cfg, db, project_dir) -> None:
        """模拟中断：RUNNING 状态重启后应恢复为可续跑，而不是重跑全部。"""
        make_test_video(project_dir / "input" / "lesson.mp4", duration=1.0)
        Scanner(cfg.paths["input"], cfg.raw["input"]["extensions"], db).scan()
        job = db.list_jobs()[0]
        db.update_status(job.job_id, JobStatus.WAITING)
        db.update_status(job.job_id, JobStatus.RUNNING)

        scheduler = Scheduler(cfg, db)
        scheduler.poll_interval = 0.1
        scheduler.run()  # recover_interrupted 在 run() 开头执行

        job = db.get_job(job.job_id)
        assert job.status is JobStatus.DONE

    def test_bad_file_isolated(self, cfg, db, project_dir) -> None:
        """坏视频不能阻塞队列（需求 J）。"""
        (project_dir / "input" / "corrupt.mp4").write_bytes(b"not a video")
        make_test_video(project_dir / "input" / "good.mp4", duration=1.0)
        Scanner(cfg.paths["input"], cfg.raw["input"]["extensions"], db).scan()

        scheduler = Scheduler(cfg, db)
        scheduler.poll_interval = 0.1
        scheduler.run()

        statuses = {Path(j.source_path).name: j.status
                    for j in db.list_jobs()}
        assert statuses["good.mp4"] is JobStatus.DONE
        assert statuses["corrupt.mp4"] is JobStatus.FAILED_FINAL
