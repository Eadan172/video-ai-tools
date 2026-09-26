"""调度器测试：选任务优先级、磁盘约束、失败隔离、断点续跑判定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_dm
from pipeline.database import Database
from pipeline.scheduler import Scheduler
from pipeline.state_machine import JobStatus, Stage, StageResult


@pytest.fixture()
def scheduler(cfg, db, project_dir) -> Scheduler:
    return Scheduler(cfg, db)


def _add(db: Database, name: str, size: int = 1024):
    return db.add_job(f"/input/{name}", size)


class TestPickNextJob:
    def test_retry_has_priority_over_waiting(self, scheduler, db,
                                             monkeypatch, tmp_path) -> None:
        scheduler.disk = make_dm(monkeypatch, tmp_path, free_gb=45)
        j1 = _add(db, "a.mp4")
        j2 = _add(db, "b.mp4")
        db.update_status(j1.job_id, JobStatus.WAITING)
        db.update_status(j2.job_id, JobStatus.WAITING)
        db.update_status(j2.job_id, JobStatus.RUNNING)
        db.update_status(j2.job_id, JobStatus.RETRY_PENDING)
        picked = scheduler._pick_next_job()
        assert picked.job_id == j2.job_id

    def test_low_disk_blocks_new_jobs(self, scheduler, db, monkeypatch) -> None:
        from conftest import make_dm
        _add(db, "a.mp4")
        scheduler.disk = make_dm(monkeypatch, Path("/tmp"), free_gb=25)
        # 25GB = CONTROLLED：没有进行中的任务 → 不启动新任务
        assert scheduler._pick_next_job() is None

    def test_low_disk_allows_continuing_job(self, scheduler, db,
                                            monkeypatch) -> None:
        from conftest import make_dm
        job = _add(db, "a.mp4")
        db.update_status(job.job_id, JobStatus.WAITING)
        db.update_status(job.job_id, JobStatus.RUNNING)
        db.set_stage(job.job_id, Stage.EXPORT)
        db.update_status(job.job_id, JobStatus.RETRY_PENDING)
        scheduler.disk = make_dm(monkeypatch, Path("/tmp"), free_gb=25)
        picked = scheduler._pick_next_job()
        assert picked is not None and picked.job_id == job.job_id

    def test_halt_blocks_everything(self, scheduler, db, monkeypatch) -> None:
        from conftest import make_dm
        _add(db, "a.mp4")
        scheduler.disk = make_dm(monkeypatch, Path("/tmp"), free_gb=5)
        assert scheduler._pick_next_job() is None

    def test_insufficient_space_marks_wait_disk(self, scheduler, db,
                                                monkeypatch) -> None:
        from conftest import make_dm
        job = _add(db, "huge.mp4", size=100 * 1024 ** 3)
        # 磁盘 40GB 但源文件 100GB：预估需求远超可用 → WAIT_DISK
        scheduler.disk = make_dm(monkeypatch, Path("/tmp"), free_gb=40)
        picked = scheduler._pick_next_job()
        assert picked is None
        assert db.get_job(job.job_id).status is JobStatus.WAIT_DISK


class TestFailureIsolation:
    def test_failure_goes_retry_then_final(self, scheduler, db) -> None:
        from pipeline.errors import PipelineError
        job = _add(db, "bad.mp4")
        scheduler.max_attempts = 2
        scheduler._handle_failure(job, PipelineError("第一次失败"))
        assert db.get_job(job.job_id).status is JobStatus.RETRY_PENDING
        scheduler._handle_failure(db.get_job(job.job_id),
                                  PipelineError("第二次失败"))
        assert db.get_job(job.job_id).status is JobStatus.FAILED_FINAL

    def test_probe_error_not_retryable(self, scheduler, db) -> None:
        from pipeline.errors import ProbeError
        job = _add(db, "corrupt.mp4")
        scheduler._handle_failure(job, ProbeError("文件损坏"))
        assert db.get_job(job.job_id).status is JobStatus.FAILED_FINAL


class TestResumeLogic:
    def test_stage_done_requires_artifact(self, scheduler, db,
                                          project_dir) -> None:
        """断点续跑不能只看数据库：产物文件缺失时必须重跑该阶段。"""
        job = _add(db, "a.mp4")
        artifact = project_dir / "work" / "current" / "video_ai.mp4"
        db.set_stage_result(job.job_id, Stage.REPAIR_VIDEO, StageResult.DONE)
        # DB 说完成了，但文件不存在 → 不算完成
        assert not scheduler._stage_done(db.get_job(job.job_id),
                                         Stage.REPAIR_VIDEO, artifact)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.touch()
        assert scheduler._stage_done(db.get_job(job.job_id),
                                     Stage.REPAIR_VIDEO, artifact)

    def test_skipped_stage_counts_as_done(self, scheduler, db) -> None:
        job = _add(db, "a.mp4")
        db.set_stage_result(job.job_id, Stage.TRANSCODE, StageResult.SKIPPED)
        assert scheduler._stage_done(db.get_job(job.job_id), Stage.TRANSCODE,
                                     Path("/nonexistent"))
