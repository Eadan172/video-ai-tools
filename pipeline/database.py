"""SQLite 持久化层。

需求要点：
- 任务状态必须存 SQLite，不能依赖内存变量；
- 程序重启后从 SQLite 恢复；
- stage 级进度存 job_stages 表，断点续跑时跳过已完成阶段；
- 断点续跑不能"只相信数据库"，调度器会再结合文件存在性判断。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .state_machine import (Job, JobStatus, Stage, StageResult,
                            can_transition, utcnow)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path     TEXT NOT NULL UNIQUE,
    source_size     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'NONE',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 0,
    profile         TEXT NOT NULL DEFAULT 'auto',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    last_error      TEXT NOT NULL DEFAULT '',
    gpu             TEXT NOT NULL DEFAULT '',
    output_path     TEXT NOT NULL DEFAULT '',
    workspace_path  TEXT NOT NULL DEFAULT '',
    metadata_json   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS job_stages (
    job_id      INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, stage),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
"""


class Database:
    """线程安全的 SQLite 封装（单写者多读者，check_same_thread=False + 锁）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #
    def add_job(self, source_path: str, source_size: int,
                priority: int = 0) -> Job | None:
        """插入新任务；已存在（按 source_path 去重）时返回 None。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO jobs "
                "(source_path, source_size, status, stage, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_path, source_size, JobStatus.DISCOVERED.value,
                 Stage.NONE.value, priority, utcnow()),
            )
            if cur.rowcount == 0:
                return None
            return self.get_job(cur.lastrowid)

    def get_job(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return Job.from_row(row) if row else None

    def get_job_by_path(self, source_path: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE source_path=?", (source_path,)).fetchone()
            return Job.from_row(row) if row else None

    def list_jobs(self, statuses: Iterable[JobStatus] | None = None) -> list[Job]:
        with self._lock:
            if statuses:
                marks = ",".join("?" * len(list(statuses)))
                rows = self._conn.execute(
                    f"SELECT * FROM jobs WHERE status IN ({marks}) "
                    "ORDER BY priority DESC, job_id ASC",
                    [s.value for s in statuses]).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY job_id ASC").fetchall()
            return [Job.from_row(r) for r in rows]

    def update_status(self, job_id: int, new: JobStatus,
                      error: str = "") -> None:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"job {job_id} 不存在")
        if not can_transition(job.status, new):
            raise ValueError(
                f"非法状态迁移: {job.status.value} -> {new.value} (job {job_id})")
        finished = utcnow() if new in (JobStatus.DONE,
                                       JobStatus.FAILED_FINAL) else None
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, last_error=?, "
                "finished_at=COALESCE(?, finished_at) WHERE job_id=?",
                (new.value, error or job.last_error, finished, job_id))

    def set_stage(self, job_id: int, stage: Stage) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET stage=? WHERE job_id=?",
                               (stage.value, job_id))

    def mark_started(self, job_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET started_at=COALESCE(started_at, ?) WHERE job_id=?",
                (utcnow(), job_id))

    def set_error(self, job_id: int, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET last_error=? WHERE job_id=?",
                               (error, job_id))

    def set_output(self, job_id: int, output_path: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET output_path=? WHERE job_id=?",
                               (output_path, job_id))

    def set_workspace(self, job_id: int, workspace_path: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET workspace_path=? WHERE job_id=?",
                               (workspace_path, job_id))

    def set_metadata(self, job_id: int, metadata_json: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET metadata_json=? WHERE job_id=?",
                               (metadata_json, job_id))

    def set_profile(self, job_id: int, profile: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET profile=? WHERE job_id=?",
                               (profile, job_id))

    def increment_retry(self, job_id: int) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET retry_count=retry_count+1 WHERE job_id=?",
                (job_id,))
            row = self._conn.execute(
                "SELECT retry_count FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return int(row["retry_count"])

    # ------------------------------------------------------------------ #
    # job_stages —— 断点续跑的核心依据之一
    # ------------------------------------------------------------------ #
    def set_stage_result(self, job_id: int, stage: Stage,
                         result: StageResult, error: str = "") -> None:
        now = utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job_stages "
                "(job_id, stage, result, attempts, started_at, error) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(job_id, stage) DO UPDATE SET "
                "result=excluded.result, error=excluded.error, "
                "attempts=job_stages.attempts+1, "
                "started_at=COALESCE(job_stages.started_at, excluded.started_at), "
                "finished_at=?",
                (job_id, stage.value, result.value, now, error,
                 now if result in (StageResult.DONE, StageResult.SKIPPED,
                                   StageResult.FAILED) else None))

    def get_stage_result(self, job_id: int, stage: Stage) -> StageResult:
        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM job_stages WHERE job_id=? AND stage=?",
                (job_id, stage.value)).fetchone()
            return StageResult(row["result"]) if row else StageResult.PENDING

    def get_completed_stages(self, job_id: int) -> dict[Stage, StageResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, result FROM job_stages WHERE job_id=?",
                (job_id,)).fetchall()
            return {Stage(r["stage"]): StageResult(r["result"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 崩溃恢复：进程重启时，把"RUNNING"但实际上已经死掉的任务拉回待跑
    # ------------------------------------------------------------------ #
    def recover_interrupted(self) -> int:
        """启动时调用：RUNNING/WAIT_RESOURCE → RETRY_PENDING（可断点续跑）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status=? WHERE status IN (?, ?)",
                (JobStatus.RETRY_PENDING.value, JobStatus.RUNNING.value,
                 JobStatus.WAIT_RESOURCE.value))
            # 被打断时正在执行的 stage 标记为 FAILED，由调度器决定重跑该阶段
            self._conn.execute(
                "UPDATE job_stages SET result=? WHERE result=?",
                (StageResult.FAILED.value, StageResult.RUNNING.value))
            return cur.rowcount

    # ------------------------------------------------------------------ #
    # 统计 / 事件
    # ------------------------------------------------------------------ #
    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}

    def log_event(self, job_id: int | None, level: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (job_id, ts, level, message) VALUES (?,?,?,?)",
                (job_id, utcnow(), level, message))
