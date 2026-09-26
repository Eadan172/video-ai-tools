"""数据库与状态机测试。"""

from __future__ import annotations

import pytest

from pipeline.database import Database
from pipeline.state_machine import (JobStatus, Stage, StageResult,
                                    can_transition)


class TestJobCRUD:
    def test_add_and_get(self, db: Database) -> None:
        job = db.add_job("/input/lesson_001.wmv", 1024)
        assert job is not None
        assert job.status is JobStatus.DISCOVERED
        fetched = db.get_job(job.job_id)
        assert fetched.source_path == "/input/lesson_001.wmv"
        assert fetched.source_size == 1024

    def test_deduplicate_by_path(self, db: Database) -> None:
        db.add_job("/input/a.mp4", 1)
        assert db.add_job("/input/a.mp4", 1) is None  # 重复路径不建任务

    def test_status_counts(self, db: Database) -> None:
        db.add_job("/input/a.mp4", 1)
        db.add_job("/input/b.mp4", 1)
        counts = db.status_counts()
        assert counts["DISCOVERED"] == 2


class TestStateMachine:
    def test_valid_transition(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        db.update_status(job.job_id, JobStatus.WAITING)
        db.update_status(job.job_id, JobStatus.RUNNING)
        db.update_status(job.job_id, JobStatus.DONE)
        assert db.get_job(job.job_id).status is JobStatus.DONE

    def test_invalid_transition_rejected(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        with pytest.raises(ValueError):
            db.update_status(job.job_id, JobStatus.DONE)  # DISCOVERED → DONE 非法

    def test_done_is_terminal(self) -> None:
        assert not can_transition(JobStatus.DONE, JobStatus.RUNNING)

    def test_failed_final_can_be_manually_retried(self) -> None:
        assert can_transition(JobStatus.FAILED_FINAL, JobStatus.RETRY_PENDING)


class TestStageResults:
    def test_stage_lifecycle(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        assert db.get_stage_result(job.job_id, Stage.EXPORT) is StageResult.PENDING
        db.set_stage_result(job.job_id, Stage.EXPORT, StageResult.RUNNING)
        db.set_stage_result(job.job_id, Stage.EXPORT, StageResult.DONE)
        assert db.get_stage_result(job.job_id, Stage.EXPORT) is StageResult.DONE

    def test_attempts_increment(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        db.set_stage_result(job.job_id, Stage.TRANSCODE, StageResult.FAILED,
                            error="boom")
        db.set_stage_result(job.job_id, Stage.TRANSCODE, StageResult.RUNNING)
        row = db._conn.execute(
            "SELECT attempts FROM job_stages WHERE job_id=? AND stage=?",
            (job.job_id, Stage.TRANSCODE.value)).fetchone()
        assert row["attempts"] == 2


class TestCrashRecovery:
    def test_running_job_recovered_as_retry_pending(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        db.update_status(job.job_id, JobStatus.WAITING)
        db.update_status(job.job_id, JobStatus.RUNNING)
        db.set_stage_result(job.job_id, Stage.REPAIR_VIDEO, StageResult.RUNNING)

        recovered = db.recover_interrupted()
        assert recovered == 1
        assert db.get_job(job.job_id).status is JobStatus.RETRY_PENDING
        assert db.get_stage_result(job.job_id, Stage.REPAIR_VIDEO) \
            is StageResult.FAILED

    def test_done_job_not_touched(self, db: Database) -> None:
        job = db.add_job("/input/a.mp4", 1)
        db.update_status(job.job_id, JobStatus.WAITING)
        db.update_status(job.job_id, JobStatus.RUNNING)
        db.update_status(job.job_id, JobStatus.DONE)
        db.recover_interrupted()
        assert db.get_job(job.job_id).status is JobStatus.DONE
