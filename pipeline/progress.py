"""进度与 ETA 计算（只读旁观者）。

Web 看板（``dashboard.py``）与终端监控（``monitor_tui.py``）共用本模块：
它只做「读库 + 算数」，不含任何渲染代码。

设计要点
--------
1. **只读连接**（``database.open_readonly``）：不写 WAL、不建表，
   与正在运行的调度器互不干扰。
2. **进度与 ETA 用历史实测阶段耗时推算**，不改数据库表结构。
3. 统计量取**截尾中位数**而非均值。实测 REPAIR_AUDIO 的样本是
   ``[364.5, 3.9, 3.3, 2.4]`` 分钟——均值 93.5 被 job 1 的长音频失败重试
   彻底带偏（``started_at`` 用 COALESCE 保留首次时间，重试不重置），
   中位数 3.6 才反映真实成本。用均值会把音频权重放大 25 倍。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import open_readonly
from .state_machine import (PIPELINE_STAGES, Job, JobStatus, Stage,
                            StageResult)

log = logging.getLogger("pipeline.progress")

# --------------------------------------------------------------------------- #
# 阶段 / 状态 → 展示用的语义节点（Web 与 TUI 共用同一套文案）
# --------------------------------------------------------------------------- #
NODE_MAP: dict[str, tuple[str, str]] = {
    JobStatus.DISCOVERED.value: ("等待队列", "○"),
    JobStatus.WAITING.value: ("等待队列", "○"),
    JobStatus.WAIT_RESOURCE.value: ("等待资源", "⏸"),
    JobStatus.WAIT_DISK.value: ("等待磁盘", "⏸"),
    JobStatus.RETRY_PENDING.value: ("重试等待", "↻"),
    JobStatus.RUNNING.value: ("处理中", "▶"),
    JobStatus.DONE.value: ("完成", "✓"),
    JobStatus.FAILED_FINAL.value: ("失败", "✕"),
    Stage.VALIDATING.value: ("片源校验", "○"),
    Stage.REPAIR_VIDEO.value: ("质量优化·画质", "▶"),
    Stage.REPAIR_AUDIO.value: ("质量优化·音质", "▶"),
    Stage.PREPARE_TRANSCODE.value: ("准备转码", "▶"),
    Stage.TRANSCODE.value: ("格式转换", "▶"),
    Stage.EXPORT.value: ("导出封装", "▶"),
    Stage.VERIFY.value: ("输出校验", "▶"),
}

#: NODE_MAP 的图标 → GBK 安全字形。
#: cmd.exe 默认 CP936 下 ✓ ✕ ▶ ⏸ ↻ ⊘ 会显示成乱码，必须降级。
_GBK_ICONS = {"✓": "√", "✕": "×", "▶": ">", "⏸": "=", "↻": "~", "⊘": "-"}

#: 再降一级：纯 ASCII（stdout 编码连 UTF-8 都不是时）
_ASCII_ICONS = {"○": "[ ]", "●": "[*]", "√": "[x]", "×": "[!]",
                ">": "[>]", "=": "[-]", "~": "[~]", "-": "[-]"}

# --------------------------------------------------------------------------- #
# 无历史样本时的降级模型
#
# 系数 = 阶段耗时 ÷ 片源时长（秒/秒），由「课程」批次 3 个成功作业实测反推：
#   录像01(3174s): 画质 1776s→0.56  音质 196s→0.062  转码 132s→0.042  导出 158s→0.050
#   录像02(2796s): 画质 1506s→0.54  音质 173s→0.062  转码 120s→0.043  导出 139s→0.050
#   录像03(2484s): 画质 1338s→0.54  音质 153s→0.062  转码 105s→0.042  导出 127s→0.051
# 三者高度一致，故取整。仅在某阶段完全没有历史样本时才会用到。
# --------------------------------------------------------------------------- #
_FACTOR_PER_SOURCE_SECOND: dict[Stage, float] = {
    Stage.REPAIR_VIDEO: 0.55,
    Stage.REPAIR_AUDIO: 0.062,
    Stage.PREPARE_TRANSCODE: 0.0001,
    Stage.TRANSCODE: 0.042,
    Stage.EXPORT: 0.050,
    Stage.VERIFY: 0.0001,
}

#: 片源时长也读不到时（该作业从未走到 VALIDATING）的兜底常量（秒）
_CONST_SECONDS: dict[Stage, float] = {
    Stage.REPAIR_VIDEO: 1400.0,
    Stage.REPAIR_AUDIO: 155.0,
    Stage.PREPARE_TRANSCODE: 0.3,
    Stage.TRANSCODE: 105.0,
    Stage.EXPORT: 125.0,
    Stage.VERIFY: 0.3,
}

#: 未开始的状态 —— 不显示百分比，改显示队列位次
_NOT_STARTED = frozenset({
    JobStatus.DISCOVERED, JobStatus.WAITING,
    JobStatus.WAIT_RESOURCE, JobStatus.WAIT_DISK,
})


def stage_fraction(ratio: float) -> float:
    """把「当前阶段已跑多久 / 预估耗时」换算成阶段进度的占比（0→1，永不到 1）。

    这是修「看板长时间数值不变」的关键。原实现直接 ``min(ratio, 0.95)``
    **硬封顶**：长阶段（REPAIR_VIDEO 约 24 分钟）里百分比与 ETA 都会退化成
    常量——页面每 3 秒确实取到了新数据，但新旧数据一模一样，看上去就像
    没有自动刷新（实测卡在 73.30% / 剩余 8 分）。

    改为两段：``ratio <= 1`` 时线性推进到 0.90；``ratio > 1``（实际比预估慢）
    转为渐近爬升，每个刷新周期仍在增长，只是越来越慢。
    """
    if ratio <= 0:
        return 0.0
    if ratio <= 1.0:
        return 0.90 * ratio
    return 0.90 + 0.095 * (1.0 - math.exp(-(ratio - 1.0)))


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StageStat:
    """某阶段的历史实测统计。"""

    stage: Stage
    median_s: float | None          # 截尾中位数；None = 无可用样本
    n: int                          # 参与统计的样本数


@dataclass(frozen=True)
class StageRow:
    """job_stages 的一行（只需展示与算进度用得到的字段）。"""

    stage: Stage
    result: StageResult
    started_at: str = ""
    finished_at: str = ""
    attempts: int = 0
    error: str = ""


@dataclass(frozen=True)
class ProgressModel:
    stats: dict[Stage, StageStat]
    job_total_median_s: float       # 单文件全流程历史中位数（吞吐外推用）
    generated_at: float


@dataclass(frozen=True)
class NodeView:
    """阶段时间线上的一个节点。"""

    key: str
    label: str
    icon: str
    state: str                      # done | current | pending | skipped | failed
    note: str = ""


@dataclass(frozen=True)
class JobView:
    job_id: int
    name: str
    status: str
    node_label: str
    node_icon: str
    percent: float | None           # None = 未开始，不显示百分比
    eta_s: float | None
    elapsed_s: float
    retry_count: int
    duration_s: float | None        # 片源时长
    speed: float | None             # 片源时长 / 已耗时
    timeline: list[NodeView]
    error: str
    style_key: str
    queue_pos: int | None


@dataclass(frozen=True)
class Snapshot:
    ts: float
    counts: dict[str, int]
    total: int
    done: int
    running: int
    waiting: int
    failed: int
    jobs: list[JobView]
    queue_eta_s: float
    eta_throughput_s: float
    model_age_s: float
    disk_free_gb: float | None
    ram_percent: float | None
    db_missing: bool = False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _parse(ts: str) -> datetime | None:
    """解析库里带时区的 ISO8601 字符串。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def human_duration(seconds: float | None) -> str:
    """时长渲染：「2 小时 11 分」/「47 分」/「< 1 分」。"""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return "< 1 分"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h >= 24:
        d, hh = divmod(h, 24)
        return f"{d} 天 {hh} 小时"
    if h:
        return f"{h} 小时 {m} 分" if m else f"{h} 小时"
    return f"{m} 分"


def human_eta(seconds: float | None) -> str:
    """剩余时间渲染：不足 1 分钟时用「即将完成」。"""
    if seconds is None:
        return "-"
    if seconds < 60:
        return "即将完成"
    return human_duration(seconds)


#: 展示排序：进行中 > 待重试 > 失败 > 等待资源 > 排队 > 完成
_DISPLAY_RANK = {"running": 0, "retry": 1, "failed": 2,
                 "resource": 3, "waiting": 4, "done": 5}


def display_order(views: list[JobView]) -> list[JobView]:
    """把作业按"最需要关注"的次序排列（Web 与 TUI 共用）。"""
    return sorted(views, key=lambda v: (_DISPLAY_RANK.get(v.style_key, 9),
                                        v.job_id))


def format_bar(percent: float | None, width: int = 20,
               filled: str = "█", empty: str = "░") -> str:
    if percent is None:
        return empty * width
    pct = min(max(float(percent), 0.0), 100.0)
    n = int(round(pct / 100.0 * width))
    return filled * n + empty * (width - n)


def safe_icon(icon: str, ascii_mode: bool = False) -> str:
    """把 NODE_MAP 的图标降级为终端可安全显示的字形。"""
    if ascii_mode:
        return _ASCII_ICONS.get(_GBK_ICONS.get(icon, icon), icon)
    return _GBK_ICONS.get(icon, icon)


def node_for(status: JobStatus, stage: Stage) -> tuple[str, str]:
    """取某作业当前应展示的节点文案与图标。"""
    if status is JobStatus.RUNNING and stage is not Stage.NONE:
        return NODE_MAP.get(stage.value, ("处理中", "▶"))
    return NODE_MAP.get(status.value, ("未知", "?"))


def style_key(status: JobStatus) -> str:
    if status is JobStatus.RUNNING:
        return "running"
    if status is JobStatus.RETRY_PENDING:
        return "retry"
    if status is JobStatus.DONE:
        return "done"
    if status is JobStatus.FAILED_FINAL:
        return "failed"
    if status in (JobStatus.WAIT_DISK, JobStatus.WAIT_RESOURCE):
        return "resource"
    return "waiting"


def _source_duration(job: Job) -> float | None:
    """从 metadata_json 取片源时长（秒）。"""
    if not job.metadata_json:
        return None
    try:
        import json
        d = json.loads(job.metadata_json)
    except (ValueError, TypeError):
        return None
    for key in ("duration", "duration_s"):
        v = d.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


# --------------------------------------------------------------------------- #
# 阶段耗时模型
# --------------------------------------------------------------------------- #
def _robust_seconds(pairs: list[tuple[float, int]]) -> tuple[float | None, int]:
    """截尾中位数。

    ``pairs`` = (耗时秒, attempts)。``attempts > 2`` 表示该阶段真的重跑过
    （正常流程恰好写入 RUNNING + DONE 两次），此时耗时含重试间隔，样本不可用。
    """
    xs = sorted(d for d, a in pairs if d > 1.0 and a <= 2)
    if not xs:
        return None, 0
    if len(xs) >= 3:
        xs = xs[:-1]                     # 去掉最大离群（重试 / 空闲夹杂）
    return statistics.median(xs), len(xs)


_MODEL_CACHE: dict[str, ProgressModel] = {}
_MODEL_LOCK = threading.Lock()


def _stage_stats(conn: sqlite3.Connection) -> dict[Stage, StageStat]:
    rows = conn.execute(
        "SELECT stage, attempts, "
        "       (julianday(finished_at) - julianday(started_at)) * 86400.0 AS dur_s "
        "FROM job_stages "
        "WHERE result IN ('DONE', 'SKIPPED') "
        "  AND started_at IS NOT NULL AND finished_at IS NOT NULL"
    ).fetchall()
    buckets: dict[Stage, list[tuple[float, int]]] = {}
    for r in rows:
        try:
            stage = Stage(r["stage"])
        except ValueError:
            continue
        buckets.setdefault(stage, []).append(
            (float(r["dur_s"] or 0.0), int(r["attempts"] or 0)))
    out: dict[Stage, StageStat] = {}
    for stage, pairs in buckets.items():
        med, n = _robust_seconds(pairs)
        out[stage] = StageStat(stage=stage, median_s=med, n=n)
    return out


def _job_total_median(conn: sqlite3.Connection) -> float:
    rows = conn.execute(
        "SELECT job_id, SUM((julianday(finished_at) - julianday(started_at))"
        " * 86400.0) AS total_s "
        "FROM job_stages "
        "WHERE result IN ('DONE', 'SKIPPED') "
        "  AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "GROUP BY job_id"
    ).fetchall()
    xs = sorted(float(r["total_s"]) for r in rows
                if r["total_s"] and float(r["total_s"]) > 1.0)
    if not xs:
        return sum(_CONST_SECONDS.values())
    if len(xs) >= 3:
        xs = xs[:-1]
    return statistics.median(xs)


def build_model(db_path: str | Path, ttl: float = 60.0,
                now_ts: float | None = None) -> ProgressModel:
    """构建（或命中缓存的）阶段耗时模型。

    带 TTL 缓存，避免每次刷新都重算导致百分比在边界上抖动。
    """
    now_ts = time.time() if now_ts is None else now_ts
    key = str(db_path)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None and 0 <= now_ts - cached.generated_at < ttl:
            return cached
    stats: dict[Stage, StageStat] = {}
    total = sum(_CONST_SECONDS.values())
    try:
        conn = open_readonly(db_path)
        try:
            stats = _stage_stats(conn)
            total = _job_total_median(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        log.debug("无法读取历史阶段耗时（将走降级模型）: %s", exc)
    model = ProgressModel(stats=stats, job_total_median_s=total,
                          generated_at=now_ts)
    with _MODEL_LOCK:
        _MODEL_CACHE[key] = model
    return model


def stage_seconds(model: ProgressModel, stage: Stage,
                  source_duration_s: float | None) -> tuple[float, str]:
    """某阶段预估耗时，(秒数, 依据)。依据 ∈ history | duration | default。"""
    st = model.stats.get(stage)
    if st is not None and st.median_s and st.median_s > 0:
        return st.median_s, "history"
    if source_duration_s and source_duration_s > 0:
        factor = _FACTOR_PER_SOURCE_SECOND.get(stage, 0.05)
        return max(factor * source_duration_s, 0.1), "duration"
    return _CONST_SECONDS.get(stage, 60.0), "default"


# --------------------------------------------------------------------------- #
# 快照构建
# --------------------------------------------------------------------------- #
def _load_jobs(conn: sqlite3.Connection) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY priority DESC, job_id ASC").fetchall()
    return [Job.from_row(r) for r in rows]


def _load_stages(conn: sqlite3.Connection) -> dict[int, dict[Stage, StageRow]]:
    out: dict[int, dict[Stage, StageRow]] = {}
    for r in conn.execute(
            "SELECT job_id, stage, result, started_at, finished_at, attempts, "
            "       error FROM job_stages"):
        try:
            stage = Stage(r["stage"])
            result = StageResult(r["result"])
        except ValueError:
            continue
        out.setdefault(int(r["job_id"]), {})[stage] = StageRow(
            stage=stage, result=result,
            started_at=r["started_at"] or "",
            finished_at=r["finished_at"] or "",
            attempts=int(r["attempts"] or 0),
            error=r["error"] or "")
    return out


def _timeline(job: Job, stages: dict[Stage, StageRow],
              model: ProgressModel, src_dur: float | None) -> list[NodeView]:
    """把 6 个流水线阶段渲染成时间线节点。"""
    nodes: list[NodeView] = []
    running_stage = job.stage if job.status is JobStatus.RUNNING else Stage.NONE
    for stage in PIPELINE_STAGES:
        row = stages.get(stage)
        label, icon = NODE_MAP.get(stage.value, (stage.value, "○"))
        est_s, basis = stage_seconds(model, stage, src_dur)
        note = f"预估 {human_duration(est_s)}"
        if basis == "history":
            note = f"实测中位 {human_duration(est_s)}"
        state = "pending"
        if row is not None:
            if row.result is StageResult.SKIPPED:
                state = "skipped"
                note = "已跳过"
            elif row.result is StageResult.DONE:
                state = "done"
                started, finished = _parse(row.started_at), _parse(row.finished_at)
                if started and finished:
                    real = (finished - started).total_seconds()
                    note = f"实测 {human_duration(real)}"
            elif row.result is StageResult.FAILED:
                state = "failed"
                note = row.error[:40] or "失败"
            elif stage is running_stage or row.result is StageResult.RUNNING:
                state = "current"
        elif stage is running_stage:
            state = "current"
        if stage is running_stage and state == "pending":
            state = "current"
        nodes.append(NodeView(key=stage.value, label=label, icon=icon,
                              state=state, note=note))
    return nodes


def _job_view(job: Job, stages: dict[Stage, StageRow], model: ProgressModel,
              now_dt: datetime, queue_pos: int | None) -> JobView:
    src_dur = _source_duration(job)
    name = Path(job.source_path).name
    label, icon = node_for(job.status, job.stage)

    plan = [s for s in PIPELINE_STAGES
            if stages.get(s) is None
            or stages[s].result is not StageResult.SKIPPED]
    est = {s: stage_seconds(model, s, src_dur)[0] for s in plan}
    total = sum(est.values()) or 1.0
    done_w = sum(est[s] for s in plan
                 if stages.get(s) is not None
                 and stages[s].result in (StageResult.DONE, StageResult.SKIPPED))

    # 本次尝试的起止时间。`mark_started` 每次尝试都会刷新 started_at 并清空
    # finished_at，所以对重跑过的作业也可靠（旧实现保留首次时间，只能不显示）
    attempt_start = _parse(job.started_at)
    attempt_end = _parse(job.finished_at)

    # 阶段内细化：仅当作业在跑且当前阶段正在执行
    frac = 0.0
    if job.status is JobStatus.RUNNING and job.stage in est:
        row = stages.get(job.stage)
        stage_start = _parse(row.started_at) if row else None
        # `job_stages.started_at` 被 COALESCE 冻结在**首次**尝试上（实测 job12
        # 差 5.5 小时），只有它不早于本次尝试起始时才可采信，否则退回尝试起始
        ref = attempt_start
        if stage_start and attempt_start and stage_start >= attempt_start:
            ref = stage_start
        if ref is not None:
            used = max((now_dt - ref).total_seconds(), 0.0)
            frac = stage_fraction(used / est[job.stage])
    cur_w = est.get(job.stage, 0.0) * frac

    if job.status is JobStatus.DONE:
        percent: float | None = 100.0
        eta: float | None = 0.0
    elif job.status is JobStatus.FAILED_FINAL:
        percent = None
        eta = None
    elif job.status in _NOT_STARTED:
        percent = None
        eta = total
    else:
        percent = min((done_w + cur_w) / total * 100.0, 99.0)
        eta = max(total - done_w - cur_w, 0.0)

    # 「已用时长」：运行中取本次尝试起始，DONE 取本次尝试的起止
    elapsed = 0.0
    if job.status is JobStatus.DONE and attempt_start and attempt_end:
        elapsed = (attempt_end - attempt_start).total_seconds()
    elif job.status is JobStatus.RUNNING and attempt_start:
        elapsed = max((now_dt - attempt_start).total_seconds(), 0.0)

    # 「处理速度」= 片源时长 ÷ 已耗时（倍速）。刚开始跑时 elapsed 极小，
    # 算出来会是几百倍的荒谬值，故满 2 分钟才开始显示。
    speed = None
    if src_dur and elapsed >= 120.0:
        speed = src_dur / elapsed

    return JobView(
        job_id=job.job_id, name=name, status=job.status.value,
        node_label=label, node_icon=icon, percent=percent, eta_s=eta,
        elapsed_s=elapsed, retry_count=job.retry_count,
        duration_s=src_dur, speed=speed,
        timeline=_timeline(job, stages, model, src_dur),
        error=(job.last_error or "")[:160], style_key=style_key(job.status),
        queue_pos=queue_pos)


def _queue_order(jobs: list[Job]) -> list[Job]:
    """与调度器一致的排队次序（RETRY_PENDING > WAIT_DISK > WAITING > DISCOVERED）。"""
    rank = {JobStatus.RETRY_PENDING: 0, JobStatus.WAIT_DISK: 1,
            JobStatus.WAITING: 2, JobStatus.DISCOVERED: 3,
            JobStatus.WAIT_RESOURCE: 4}
    pend = [j for j in jobs if j.status in _NOT_STARTED
            or j.status is JobStatus.RETRY_PENDING]
    return sorted(pend, key=lambda j: (rank.get(j.status, 9),
                                       -j.priority, j.job_id))


def _host_metrics(cfg) -> tuple[float | None, float | None]:
    disk_free = ram = None
    try:
        import psutil
        ram = float(psutil.virtual_memory().percent)
        for key in ("output", "work"):
            p = (cfg.paths or {}).get(key)
            if p and Path(p).exists():
                disk_free = psutil.disk_usage(str(p)).free / 1024 ** 3
                break
    except Exception as exc:  # noqa: BLE001 —— 指标缺失不该影响监控
        log.debug("读取主机指标失败: %s", exc)
    return disk_free, ram


def build_snapshot(cfg, now_ts: float | None = None) -> Snapshot:
    """采集一次完整快照。任何读库失败都降级为空态，绝不抛给界面。"""
    now_ts = time.time() if now_ts is None else now_ts
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    db_path = (cfg.paths or {}).get("database")
    dash_cfg = (cfg.raw or {}).get("dashboard", {}) or {}
    ttl = float(dash_cfg.get("model_ttl_seconds", 60))
    model = build_model(db_path, ttl=ttl, now_ts=now_ts)

    jobs: list[Job] = []
    stages: dict[int, dict[Stage, StageRow]] = {}
    missing = False
    try:
        conn = open_readonly(db_path)
        try:
            jobs = _load_jobs(conn)
            stages = _load_stages(conn)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        missing = True
        log.debug("无法读取作业库 %s: %s", db_path, exc)

    counts: dict[str, int] = {}
    for j in jobs:
        counts[j.status.value] = counts.get(j.status.value, 0) + 1

    order = _queue_order(jobs)
    pos = {j.job_id: i + 1 for i, j in enumerate(order)}

    views = display_order([
        _job_view(j, stages.get(j.job_id, {}), model, now_dt,
                  pos.get(j.job_id) if j.status in _NOT_STARTED
                  else None)
        for j in jobs
    ])

    queue_eta = sum(v.eta_s for v in views if v.eta_s)
    remaining = sum(1 for v in views
                    if v.status not in (JobStatus.DONE.value,
                                        JobStatus.FAILED_FINAL.value))
    disk_free, ram = _host_metrics(cfg)

    return Snapshot(
        ts=now_ts, counts=counts, total=len(jobs),
        done=counts.get(JobStatus.DONE.value, 0),
        running=counts.get(JobStatus.RUNNING.value, 0),
        waiting=sum(counts.get(s.value, 0) for s in _NOT_STARTED)
        + counts.get(JobStatus.RETRY_PENDING.value, 0),
        failed=counts.get(JobStatus.FAILED_FINAL.value, 0),
        jobs=views, queue_eta_s=queue_eta,
        eta_throughput_s=remaining * model.job_total_median_s,
        model_age_s=max(now_ts - model.generated_at, 0.0),
        disk_free_gb=disk_free, ram_percent=ram, db_missing=missing)


def snapshot_to_dict(s: Snapshot) -> dict:
    """JSON 可序列化形式（Web 的 /api/state 与 TUI 的 --json 共用）。"""

    def _node(n: NodeView) -> dict:
        return {"key": n.key, "label": n.label, "icon": n.icon,
                "state": n.state, "note": n.note}

    def _job(v: JobView) -> dict:
        return {
            "job_id": v.job_id, "name": v.name, "status": v.status,
            "node_label": v.node_label, "node_icon": v.node_icon,
            "percent": None if v.percent is None else round(v.percent, 2),
            "eta_s": None if v.eta_s is None else round(v.eta_s, 1),
            "eta_text": human_eta(v.eta_s),
            "elapsed_s": round(v.elapsed_s, 1),
            "retry_count": v.retry_count,
            "duration_s": v.duration_s,
            "duration_text": human_duration(v.duration_s),
            "speed": None if v.speed is None else round(v.speed, 2),
            "timeline": [_node(n) for n in v.timeline],
            "error": v.error, "style_key": v.style_key, "queue_pos": v.queue_pos,
        }

    return {
        "ts": s.ts,
        "counts": s.counts, "total": s.total, "done": s.done,
        "running": s.running, "waiting": s.waiting, "failed": s.failed,
        "queue_eta_s": round(s.queue_eta_s, 1),
        "queue_eta_text": human_duration(s.queue_eta_s),
        "eta_throughput_s": round(s.eta_throughput_s, 1),
        "eta_throughput_text": human_duration(s.eta_throughput_s),
        "model_age_s": round(s.model_age_s, 1),
        "disk_free_gb": None if s.disk_free_gb is None else round(s.disk_free_gb, 1),
        "ram_percent": None if s.ram_percent is None else round(s.ram_percent, 1),
        "db_missing": s.db_missing,
        "jobs": [_job(v) for v in s.jobs],
    }