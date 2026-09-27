"""pipeline.progress 的单元测试。

全部注入 `now` / 构造确定性的历史样本，不使用 sleep。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import progress as P
from pipeline.dashboard import _HTML
from pipeline.state_machine import JobStatus, Stage, StageResult


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _seed(path: Path, jobs=(), stages=()) -> None:
    """直接写库，便于构造确定性的时间戳。"""
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO jobs (job_id, source_path, source_size,"
                " status, stage, retry_count, priority, created_at, started_at,"
                " finished_at, last_error, metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", jobs)
            conn.executemany(
                "INSERT OR REPLACE INTO job_stages (job_id, stage, result,"
                " attempts, started_at, finished_at, error) "
                "VALUES (?,?,?,?,?,?,?)", stages)
    finally:
        conn.close()


def _job(job_id: int, name: str, status: JobStatus, stage: Stage,
         *, retry: int = 0, started: str = "", finished: str = "",
         error: str = "", meta: str = "") -> tuple:
    return (job_id, f"C:/in/{name}", 1000, status.value, stage.value,
            retry, 0, _iso(datetime.now(timezone.utc)), started, finished,
            error, meta)


def _stage(job_id: int, stage: Stage, result: StageResult, secs: float,
           attempts: int = 2, base: datetime | None = None) -> tuple:
    base = base or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (job_id, stage.value, result.value, attempts,
            _iso(base), _iso(base + timedelta(seconds=secs)), "")


@pytest.fixture()
def nocache(cfg):
    """关掉模型缓存，让同一个测试内的多次快照互不串味。"""
    cfg.raw["dashboard"] = {"model_ttl_seconds": 0}
    return cfg


# --------------------------------------------------------------------------- #
# 1. 统计口径：截尾中位数（本方案的核心论点）
# --------------------------------------------------------------------------- #
def test_robust_seconds_uses_median_not_mean():
    """REPAIR_AUDIO 的真实样本——均值会被失败重试彻底带偏。"""
    minutes = [364.5, 3.9, 3.3, 2.4]          # job 1 的 364.5 是 DFN 失败重试
    pairs = [(m * 60.0, 2) for m in minutes]

    med, n = P._robust_seconds(pairs)
    assert med is not None
    assert n == 3                              # 4 个样本去掉最大离群后剩 3
    # 去离群后 [2.4, 3.3, 3.9] 的中位数 = 3.3 分钟（即 198 秒）
    assert med == pytest.approx(3.3 * 60.0, abs=1.0)

    mean_s = sum(minutes) / len(minutes) * 60.0
    assert mean_s > 90 * 60                    # 均值 93.5 分钟，放大约 25 倍
    assert med < mean_s / 25                   # 中位数远小于均值 —— 口径必须用中位数


def test_robust_seconds_excludes_retried_samples():
    """attempts>2 表示该阶段真的重跑过，耗时含重试间隔，样本不可用。"""
    pairs = [(3600.0, 4), (60.0, 2), (70.0, 2)]
    med, n = P._robust_seconds(pairs)
    assert n == 2
    assert med == pytest.approx(65.0)


def test_robust_seconds_ignores_noise_and_empty():
    assert P._robust_seconds([])[0] is None
    assert P._robust_seconds([(0.4, 2), (0.9, 2)])[0] is None   # 全部过短


# --------------------------------------------------------------------------- #
# 2. 降级链：history → duration → default
# --------------------------------------------------------------------------- #
def test_stage_seconds_fallback_chain():
    model = P.ProgressModel(stats={}, job_total_median_s=100.0, generated_at=0.0)

    # 无历史 + 有片源时长 → 用系数
    secs, basis = P.stage_seconds(model, Stage.REPAIR_VIDEO, 3000.0)
    assert basis == "duration"
    assert secs == pytest.approx(0.55 * 3000.0)

    # 无历史 + 无片源时长 → 常量
    secs, basis = P.stage_seconds(model, Stage.REPAIR_VIDEO, None)
    assert basis == "default"
    assert secs == P._CONST_SECONDS[Stage.REPAIR_VIDEO]

    # 有历史 → 优先用历史中位数
    model2 = P.ProgressModel(
        stats={Stage.REPAIR_VIDEO: P.StageStat(Stage.REPAIR_VIDEO, 999.0, 3)},
        job_total_median_s=100.0, generated_at=0.0)
    secs, basis = P.stage_seconds(model2, Stage.REPAIR_VIDEO, 3000.0)
    assert (secs, basis) == (999.0, "history")


# --------------------------------------------------------------------------- #
# 3. 快照：空库 / 正常库都不该炸
# --------------------------------------------------------------------------- #
def test_snapshot_empty_db(nocache, db):
    s = P.build_snapshot(nocache)
    assert s.total == 0
    assert s.jobs == []
    assert s.queue_eta_s == 0.0
    assert s.db_missing is False


def test_snapshot_missing_db(tmp_path):
    """库文件不存在时降级为空态，而不是抛异常给界面。"""

    class _Cfg:
        paths = {"database": str(tmp_path / "nope.db"), "output": None,
                 "work": None}
        raw = {"dashboard": {"model_ttl_seconds": 0}}

    s = P.build_snapshot(_Cfg())
    assert s.db_missing is True
    assert s.total == 0


# --------------------------------------------------------------------------- #
# 4. 百分比
# --------------------------------------------------------------------------- #
def test_done_job_is_100_even_when_all_ai_stages_skipped(nocache, db, project_dir):
    """light 档：AI 阶段全部 SKIPPED，分母要剔除，完成时恰好 100。"""
    db_path = project_dir / "pipeline.db"
    stages = [(1, s.value, StageResult.SKIPPED.value, 2,
               _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
               _iso(datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)), "")
              for s in Stage]
    _seed(db_path, jobs=[_job(1, "a.wmv", JobStatus.DONE, Stage.NONE)],
          stages=stages)

    v = P.build_snapshot(nocache).jobs[0]
    assert v.percent == 100.0
    assert v.eta_s == 0.0


def test_stage_fraction_is_capped_below_100(nocache, db, project_dir):
    """阶段内细化封顶 95%：即使 elapsed 远超预估，也不能显示 100%。"""
    db_path = project_dir / "pipeline.db"
    now = datetime.now(timezone.utc)
    _seed(db_path, jobs=[_job(1, "a.wmv", JobStatus.RUNNING, Stage.REPAIR_VIDEO,
                              started=_iso(now - timedelta(minutes=1)))],
          stages=[(1, Stage.REPAIR_VIDEO.value, StageResult.RUNNING.value, 2,
                   _iso(now - timedelta(minutes=1)), None, "")])

    early = P.build_snapshot(nocache, now_ts=now.timestamp()).jobs[0]
    assert early.percent is not None and 0 < early.percent < 100

    # 把阶段起点挪到很久以前 —— 模拟"重试间隔把 elapsed 撑爆"
    _seed(db_path, stages=[(1, Stage.REPAIR_VIDEO.value,
                            StageResult.RUNNING.value, 2,
                            _iso(now - timedelta(hours=100)), None, "")])
    late = P.build_snapshot(nocache, now_ts=now.timestamp()).jobs[0]

    assert late.percent is not None
    assert late.percent < 100.0                 # 绝不能到 100
    assert late.percent > early.percent         # 但确实在推进


def test_not_started_shows_queue_position_not_zero_percent(nocache, db,
                                                           project_dir):
    """等待中的作业不显示百分比（否则 30+ 张卡片全是 0% 会造成卡死的错觉）。"""
    db_path = project_dir / "pipeline.db"
    _seed(db_path, jobs=[
        _job(1, "a.wmv", JobStatus.DISCOVERED, Stage.NONE),
        _job(2, "b.wmv", JobStatus.WAITING, Stage.NONE),
    ])
    s = P.build_snapshot(nocache)
    assert all(v.percent is None for v in s.jobs)
    assert sorted(v.queue_pos for v in s.jobs) == [1, 2]
    assert s.queue_eta_s > 0


def test_retry_pending_keeps_percent_and_eta(nocache, db, project_dir):
    db_path = project_dir / "pipeline.db"
    _seed(db_path, jobs=[_job(1, "a.wmv", JobStatus.RETRY_PENDING,
                              Stage.REPAIR_VIDEO, retry=1,
                              error="RVE 内存不足")],
          stages=[(1, Stage.REPAIR_VIDEO.value, StageResult.FAILED.value, 2,
                   _iso(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                   _iso(datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)),
                   "RVE 内存不足")])
    v = P.build_snapshot(nocache).jobs[0]
    assert v.retry_count == 1
    assert v.percent is not None                # 保留基准百分比
    assert v.eta_s is not None
    assert v.style_key == "retry"
    assert v.error.startswith("RVE")


def test_stale_finished_at_does_not_produce_fake_elapsed(nocache, db,
                                                         project_dir):
    """重跑过的作业 finished_at 残留上次时间，不能据此算出「已用 2 秒」。"""
    db_path = project_dir / "pipeline.db"
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _seed(db_path, jobs=[_job(1, "a.wmv", JobStatus.RUNNING, Stage.REPAIR_VIDEO,
                              started=_iso(t0),
                              finished=_iso(t0 + timedelta(seconds=2)))])
    v = P.build_snapshot(nocache).jobs[0]
    assert v.elapsed_s == 0.0                   # 时间戳不自洽 → 不展示
    assert v.speed is None


# --------------------------------------------------------------------------- #
# 5. 历史统计确实被读进来
# --------------------------------------------------------------------------- #
def test_model_reads_history_and_drops_outlier(nocache, db, project_dir):
    db_path = project_dir / "pipeline.db"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stages = []
    for job_id, secs in ((1, 3600.0 * 6), (2, 1700.0), (3, 1750.0)):
        stages.append(_stage(job_id, Stage.REPAIR_VIDEO, StageResult.DONE,
                             secs, base=base))
    _seed(db_path,
          jobs=[_job(i, f"{i}.wmv", JobStatus.DONE, Stage.NONE)
                for i in (1, 2, 3)],
          stages=stages)

    m = P.build_model(db_path, ttl=0)
    stat = m.stats[Stage.REPAIR_VIDEO]
    assert stat.n == 2                          # 3 个样本去掉最大离群
    assert stat.median_s == pytest.approx(1725.0)


# --------------------------------------------------------------------------- #
# 6. 渲染辅助
# --------------------------------------------------------------------------- #
def test_human_duration_and_eta():
    assert P.human_duration(30) == "< 1 分"
    assert P.human_duration(600) == "10 分"
    assert P.human_duration(3660) == "1 小时 1 分"
    assert P.human_duration(90000) == "1 天 1 小时"
    assert P.human_eta(30) == "即将完成"
    assert P.human_eta(None) == "-"


def test_format_bar_bounds():
    assert P.format_bar(None, 4) == "░░░░"
    assert P.format_bar(-5, 4) == "░░░░"
    assert P.format_bar(150, 4) == "████"
    assert P.format_bar(50, 4) == "██░░"


def test_safe_icon_downgrades_for_gbk_and_ascii():
    # NODE_MAP 里的 ✓ 不在 CP936 中，必须降级
    assert P.safe_icon("✓") == "√"
    assert P.safe_icon("✓", ascii_mode=True) == "[x]"
    assert P.safe_icon("○", ascii_mode=True) == "[ ]"


def test_display_order_puts_running_first(nocache, db, project_dir):
    db_path = project_dir / "pipeline.db"
    _seed(db_path, jobs=[
        _job(1, "done.wmv", JobStatus.DONE, Stage.NONE),
        _job(2, "wait.wmv", JobStatus.WAITING, Stage.NONE),
        _job(3, "run.wmv", JobStatus.RUNNING, Stage.REPAIR_VIDEO),
        _job(4, "retry.wmv", JobStatus.RETRY_PENDING, Stage.REPAIR_VIDEO),
    ])
    order = [v.style_key for v in P.build_snapshot(nocache).jobs]
    assert order[0] == "running"
    assert order[1] == "retry"
    assert order[-1] == "done"


# --------------------------------------------------------------------------- #
# 7. 离线约束回归：看板不得引用任何外部资源
# --------------------------------------------------------------------------- #
def test_dashboard_html_has_no_external_resources():
    from pipeline.dashboard import _INTERVAL_TOKEN
    for bad in ("http://", "https://", "//cdn", "googleapis", "unpkg"):
        assert bad not in _HTML, f"看板 HTML 引用了外部资源: {bad}"
    assert "<meta charset=\"utf-8\">" in _HTML
    assert _INTERVAL_TOKEN in _HTML          # 刷新间隔占位符待运行时替换


def test_snapshot_to_dict_is_json_serializable(nocache, db, project_dir):
    import json
    db_path = project_dir / "pipeline.db"
    _seed(db_path, jobs=[_job(1, "a.wmv", JobStatus.RUNNING,
                              Stage.REPAIR_VIDEO)])
    d = P.snapshot_to_dict(P.build_snapshot(nocache))
    assert json.loads(json.dumps(d, ensure_ascii=False))["total"] == 1
    assert d["jobs"][0]["timeline"][0]["key"] == Stage.REPAIR_VIDEO.value