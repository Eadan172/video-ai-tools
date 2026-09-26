"""资源感知调度器 —— 项目核心（需求 #16 / #18 / #34 / #35）。

调度原则（稳定性 > 吞吐量）：
1. 视频级并发 = 1：同一时间只有一个视频占用 work/current；
2. 磁盘是第一约束：低于阈值不启动新任务，紧急时触发清理；
3. 断点续跑：以 SQLite 的 job_stages 结果 + 文件实际存在性共同决定
   从哪个阶段继续，绝不盲信数据库；
4. 单个视频失败隔离，不阻塞队列；
5. 编码后端 QSV → NVENC → CPU 自动降级；
6. GPU OOM → 降低超分倍率重试一次。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from adapters import (EncoderBackend, FFmpegAdapter, FFprobeAdapter,
                        RealVideoEnhancerAdapter, build_audio_adapter)
from profiles.selector import Profile, select_profile
from .cleanup import Cleaner, delete_if_exists
from .config import Config
from .database import Database
from .disk_manager import DiskManager, DiskState
from .errors import (DependencyError, DiskSpaceError, EmergencyStopError,
                     GpuOutOfMemoryError, PipelineError, ProbeError)
from .filename import unique_output_path
from .logger import get_job_logger, job_log_path
from .resources import ResourceMonitor
from .state_machine import (Job, JobStatus, MediaInfo, PIPELINE_STAGES, Stage,
                            StageResult)
from .verifier import Verifier

log = logging.getLogger("pipeline.scheduler")


class Scheduler:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        paths = cfg.paths
        self.input_dir = paths["input"]
        self.work_dir = paths["work"]
        self.output_dir = paths["output"]
        self.failed_dir = paths["failed"]
        self.log_dir = paths["logs"]

        self.disk = DiskManager(cfg.disk, watch_path=self.work_dir,
                                work_dir=self.work_dir / "current")
        self.resources = ResourceMonitor(cfg.resources, self.work_dir)
        self.cleaner = Cleaner(self.work_dir, self.input_dir, self.output_dir)
        self.ffprobe = FFprobeAdapter()
        self.ffmpeg = FFmpegAdapter(encoder_cfg=cfg.raw.get("encoder", {}))
        self.video_ai = RealVideoEnhancerAdapter(cfg.video_repair)
        self.audio_ai = build_audio_adapter(cfg.audio_repair)
        self.verifier = Verifier(self.ffprobe, cfg.verify, cfg.output)

        self.max_workspaces = int(
            cfg.scheduler_opts.get("max_active_video_workspaces", 1))
        self.poll_interval = float(
            cfg.scheduler_opts.get("poll_interval_seconds", 10))
        self.max_attempts = int(cfg.retry.get("max_attempts", 2))
        self._stop_requested = False

    # ================================================================== #
    # 主循环
    # ================================================================== #
    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        """无人值守主循环。Ctrl+C 时安全退出，状态已持久化可恢复。"""
        recovered = self.db.recover_interrupted()
        if recovered:
            log.info("恢复 %d 个被中断的任务", recovered)
        log.info("调度器启动：max_workspaces=%d", self.max_workspaces)

        while not self._stop_requested:
            try:
                self.disk.assert_not_halted()
            except EmergencyStopError as exc:
                log.error("%s；触发紧急清理后等待", exc)
                freed = self.cleaner.emergency_cleanup()
                log.info("紧急清理释放 %.2f GB", freed / 1024 ** 3)
                time.sleep(self.poll_interval)
                continue

            job = self._pick_next_job()
            if job is None:
                pending = self._has_pending_work()
                if not pending:
                    log.info("队列已清空，调度器退出")
                    return
                time.sleep(self.poll_interval)
                continue

            try:
                self._process_job(job)
            except (KeyboardInterrupt, SystemExit):
                log.warning("收到中断信号，状态已保存，可安全退出")
                raise
            except PipelineError as exc:
                self._handle_failure(job, exc)
            except Exception as exc:  # noqa: BLE001 —— 单任务异常不阻塞队列
                log.exception("任务 %d 未预期异常", job.job_id)
                self._handle_failure(job, PipelineError(str(exc)))

    def _has_pending_work(self) -> bool:
        counts = self.db.status_counts()
        active = sum(counts.get(s.value, 0) for s in (
            JobStatus.DISCOVERED, JobStatus.WAITING, JobStatus.WAIT_RESOURCE,
            JobStatus.WAIT_DISK, JobStatus.RUNNING, JobStatus.RETRY_PENDING))
        return active > 0

    def _pick_next_job(self) -> Job | None:
        """优先级（需求 #17）：重试 > 等待 > 新任务；受磁盘阈值约束。"""
        state = self.disk.state()
        # 磁盘紧张时只允许继续"已经开始"的任务
        candidates: list[Job] = []
        if state is DiskState.NORMAL:
            candidates = self.db.list_jobs([
                JobStatus.RETRY_PENDING, JobStatus.WAITING,
                JobStatus.WAIT_DISK, JobStatus.DISCOVERED])
        elif state in (DiskState.CONTROLLED, DiskState.CONSERVATIVE,
                       DiskState.EMERGENCY_CLEANUP):
            candidates = [j for j in self.db.list_jobs(
                [JobStatus.RETRY_PENDING, JobStatus.WAIT_DISK])
                if j.stage != Stage.NONE and j.stage in PIPELINE_STAGES]
        else:
            return None

        if not candidates:
            return None
        # 状态优先级（需求 #17）：RETRY_PENDING > WAITING > 新任务
        rank = {JobStatus.RETRY_PENDING: 0, JobStatus.WAIT_DISK: 1,
                JobStatus.WAITING: 2, JobStatus.DISCOVERED: 3}
        candidates.sort(key=lambda j: (rank.get(j.status, 9),
                                       -j.priority, j.job_id))
        job = candidates[0]

        # 单文件空间预算（需求 #12 / #27）
        if job.stage == Stage.NONE or job.status in (JobStatus.DISCOVERED,):
            ok, required, free = self.disk.has_space_for(job.source_size)
            if not ok:
                log.warning("磁盘不足：job %d 需要 %.1fGB，可用 %.1fGB → WAIT_DISK",
                            job.job_id, required, free)
                if job.status in (JobStatus.DISCOVERED,):
                    self.db.update_status(job.job_id, JobStatus.WAITING)
                self.db.update_status(job.job_id, JobStatus.WAIT_DISK)
                return None
        return job

    # ================================================================== #
    # 单任务流水线
    # ================================================================== #
    def _process_job(self, job: Job) -> None:
        jlog = get_job_logger(self.log_dir, job.job_id)
        jlog.info("=== 开始处理 %s ===", job.source_path)
        self.db.mark_started(job.job_id)
        self.db.update_status(job.job_id, JobStatus.RUNNING)
        job = self.db.get_job(job.job_id)  # 刷新

        workspace = self.cleaner.prepare_current()
        self.db.set_workspace(job.job_id, str(workspace))
        log_file = job_log_path(self.log_dir, job.job_id)

        # ---------- VALIDATING：ffprobe + profile ---------- #
        if self.db.get_stage_result(job.job_id, Stage.VALIDATING) not in (
                StageResult.DONE, StageResult.SKIPPED) or not job.metadata_json:
            self.db.set_stage(job.job_id, Stage.VALIDATING)
            self.db.set_stage_result(job.job_id, Stage.VALIDATING,
                                     StageResult.RUNNING)
            info = self.ffprobe.probe(job.source_path)
            if info.anomalies and not (info.has_video or info.has_audio):
                raise ProbeError(f"源文件异常: {'; '.join(info.anomalies)}")
            self.db.set_metadata(job.job_id, json.dumps(
                info.__dict__, ensure_ascii=False))
            profile = self._resolve_profile(job, info)
            self.db.set_profile(job.job_id, profile.name)
            self.db.set_stage_result(job.job_id, Stage.VALIDATING,
                                     StageResult.DONE)
            jlog.info("VALIDATING 完成 profile=%s", profile.name)
        job = self.db.get_job(job.job_id)
        info = MediaInfo(**json.loads(job.metadata_json))
        profile = self._resolve_profile(job, info)

        # ---------- 流水线各阶段（断点续跑：已完成且产物存在 → 跳过） ---------- #
        video_ai = workspace / "video_ai.mp4"
        audio_wav = workspace / "audio.wav"
        audio_clean = workspace / "audio_clean.wav"

        self._stage_repair_video(job, info, profile, video_ai, log_file, jlog)
        self._stage_repair_audio(job, info, audio_wav, audio_clean,
                                 log_file, jlog)
        final_video = self._stage_transcode(job, video_ai, log_file, jlog)
        output_path = self._stage_export(job, final_video, audio_clean,
                                         info, log_file, jlog)
        self._stage_verify(job, output_path, info, jlog)

        # ---------- 完成：清理工作区（需求 I） ---------- #
        self.cleaner.cleanup_current(logger=jlog)
        self.db.set_stage(job.job_id, Stage.NONE)
        self.db.update_status(job.job_id, JobStatus.DONE)
        jlog.info("=== 完成 → %s ===", output_path)

    # ------------------------------------------------------------------ #
    def _resolve_profile(self, job: Job, info: MediaInfo) -> Profile:
        return select_profile(info, self.cfg.raw.get("profiles", {}))

    def _stage_done(self, job: Job, stage: Stage, artifact: Path | None) -> bool:
        """断点续跑判定：DB 记录完成 + 产物文件真实存在。"""
        result = self.db.get_stage_result(job.job_id, stage)
        if result not in (StageResult.DONE, StageResult.SKIPPED):
            return False
        if result is StageResult.SKIPPED:
            return True
        return artifact is None or artifact.exists()

    def _output_dir_for(self, job: Job) -> Path:
        """输出目录镜像输入的相对子目录（input/剧集 → output/剧集）。

        这样不同来源的视频在 output/ 下各自成目录，不会混在一起；
        源文件直接在 input/ 根下时仍输出到 output/ 根下。
        """
        try:
            rel = Path(job.source_path).resolve().parent.relative_to(self.input_dir)
        except (ValueError, OSError):
            return self.output_dir
        if str(rel) in (".", ""):
            return self.output_dir
        target = self.output_dir / rel
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("创建输出子目录失败 %s: %s", target, exc)
            return self.output_dir
        return target

    # ------------------------------------------------------------------ #
    def _stage_repair_video(self, job: Job, info: MediaInfo, profile: Profile,
                            video_ai: Path, log_file: Path, jlog) -> None:
        stage = Stage.REPAIR_VIDEO
        if self._stage_done(job, stage, video_ai):
            jlog.info("%s 已完成，跳过", stage.value)
            return
        self._wait_resources(jlog)
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        vr_cfg = self.cfg.video_repair
        if not vr_cfg.get("enabled", True) or vr_cfg.get("backend") == "none":
            # 不做视频 AI：直接用 FFmpeg 转一份高质量副本作为后续输入
            jlog.info("视频 AI 已禁用，直接转码为中间文件")
            self.ffmpeg.transcode(Path(job.source_path), video_ai,
                                  video_codec=self.cfg.output.get(
                                      "video_codec", "hevc"),
                                  sample_rate=self.cfg.output.get(
                                      "sample_rate", 48000),
                                  log_file=log_file)
            self.db.set_stage_result(job.job_id, stage, StageResult.SKIPPED)
            return

        scale = profile.upscale_scale
        try:
            self.video_ai.enhance(Path(job.source_path), video_ai,
                                  scale=scale, log_file=log_file)
        except GpuOutOfMemoryError:
            if scale > 1:
                jlog.warning("GPU OOM，降低超分倍率 %dx → 1x 重试", scale)
                self.video_ai.enhance(Path(job.source_path), video_ai,
                                      scale=1, log_file=log_file)
            else:
                raise
        if not video_ai.exists():
            raise PipelineError(f"视频 AI 未产出文件: {video_ai}")
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
        jlog.info("%s 完成 → %s (%.2f GB)", stage.value, video_ai.name,
                  video_ai.stat().st_size / 1024 ** 3)

    def _stage_repair_audio(self, job: Job, info: MediaInfo, audio_wav: Path,
                            audio_clean: Path, log_file: Path, jlog) -> None:
        stage = Stage.REPAIR_AUDIO
        if self._stage_done(job, stage, audio_clean):
            jlog.info("%s 已完成，跳过", stage.value)
            return
        self._wait_resources(jlog)
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        ar_cfg = self.cfg.audio_repair
        if not info.has_audio:
            self.db.set_stage_result(job.job_id, stage, StageResult.SKIPPED)
            return
        if not ar_cfg.get("enabled", True) or self.audio_ai is None:
            # 不做音频 AI：直接抽取 WAV 供合流使用
            self.ffmpeg.extract_audio(Path(job.source_path), audio_clean,
                                      sample_rate=int(ar_cfg.get(
                                          "sample_rate", 48000)),
                                      log_file=log_file)
            self.db.set_stage_result(job.job_id, stage, StageResult.SKIPPED)
            return

        self.ffmpeg.extract_audio(Path(job.source_path), audio_wav,
                                  sample_rate=int(ar_cfg.get(
                                      "sample_rate", 48000)),
                                  log_file=log_file)
        self.audio_ai.enhance(audio_wav, audio_clean, log_file=log_file)
        # 磁盘优化（需求 #6）：清洗完成后立即删除原始 WAV
        delete_if_exists(audio_wav, jlog)
        if not audio_clean.exists():
            raise PipelineError(f"音频 AI 未产出文件: {audio_clean}")
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
        jlog.info("%s 完成 → %s", stage.value, audio_clean.name)

    def _stage_transcode(self, job: Job, video_ai: Path, log_file: Path,
                         jlog) -> Path:
        """PREPARE_TRANSCODE（智能跳过判定）+ TRANSCODE。返回最终视频路径。"""
        stage = Stage.PREPARE_TRANSCODE
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        src_video = video_ai if video_ai.exists() else Path(job.source_path)
        if self.verifier.matches_target(str(src_video)):
            jlog.info("已是目标格式 → TRANSCODE = SKIP")
            self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
            self.db.set_stage_result(job.job_id, Stage.TRANSCODE,
                                     StageResult.SKIPPED)
            return src_video
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)

        stage = Stage.TRANSCODE
        transcoded = video_ai.parent / f"transcoded.{self.cfg.output_ext}"
        if self._stage_done(job, stage, transcoded):
            jlog.info("%s 已完成，跳过", stage.value)
            return transcoded
        self._wait_resources(jlog)
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        out_cfg = self.cfg.output
        # adapter 内部自动执行 QSV → NVENC → CPU 降级链（需求 #23）
        self.ffmpeg.transcode(
            src_video, transcoded,
            video_codec=out_cfg.get("video_codec", "hevc"),
            audio_codec=out_cfg.get("audio_codec", "aac"),
            audio_bitrate=out_cfg.get("audio_bitrate", "320k"),
            sample_rate=int(out_cfg.get("sample_rate", 48000)),
            log_file=log_file)
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
        jlog.info("%s 完成 → %s", stage.value, transcoded.name)
        # AI 中间视频使命完成，立即删除释放磁盘
        if video_ai.exists() and video_ai != transcoded:
            delete_if_exists(video_ai, jlog)
        return transcoded

    def _stage_export(self, job: Job, final_video: Path, audio_clean: Path,
                      info: MediaInfo, log_file: Path, jlog) -> Path:
        stage = Stage.EXPORT
        existing_output = Path(job.output_path) if job.output_path else None
        if self._stage_done(job, stage, existing_output):
            jlog.info("%s 已完成，跳过", stage.value)
            return existing_output
        self._wait_resources(jlog)
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        out_cfg = self.cfg.output
        container = out_cfg.get("container", "mp4")
        # 输出镜像输入的相对子目录：input/剧集/x.mp4 → output/剧集/x.mp4
        out_dir = self._output_dir_for(job)
        final_path = unique_output_path(out_dir,
                                        Path(job.source_path).stem,
                                        f".{container}")
        partial = final_path.with_suffix(final_path.suffix + ".partial")

        audio = audio_clean if audio_clean.exists() else None
        self.ffmpeg.mux(final_video, audio, partial,
                        audio_codec=out_cfg.get("audio_codec", "aac"),
                        audio_bitrate=out_cfg.get("audio_bitrate", "320k"),
                        sample_rate=int(out_cfg.get("sample_rate", 48000)),
                        container=container,
                        log_file=log_file)
        # 合流成功 → 立即删除 audio_clean.wav（需求 #6）
        if audio is not None:
            delete_if_exists(audio, jlog)
        self.db.set_output(job.job_id, str(final_path))
        # partial 留给 VERIFY 阶段做 atomic rename
        self._partial_path = partial  # noqa: SLF001  同一对象内传递
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
        return final_path

    def _stage_verify(self, job: Job, output_path: Path, info: MediaInfo,
                      jlog) -> None:
        stage = Stage.VERIFY
        if self._stage_done(job, stage, output_path):
            jlog.info("%s 已完成，跳过", stage.value)
            return
        self.db.set_stage(job.job_id, stage)
        self.db.set_stage_result(job.job_id, stage, StageResult.RUNNING)

        partial = getattr(self, "_partial_path",
                          output_path.with_suffix(output_path.suffix + ".partial"))
        candidate = partial if Path(partial).exists() else output_path
        self.verifier.verify_output(str(candidate), info)
        # Atomic Output（需求 #19）：验证通过才 rename
        if Path(partial).exists():
            Path(partial).replace(output_path)
        self.db.set_stage_result(job.job_id, stage, StageResult.DONE)
        jlog.info("VERIFY 通过 → %s", output_path)

    # ------------------------------------------------------------------ #
    def _wait_resources(self, jlog) -> None:
        """阶段启动前资源检查：RAM 超软限制 / 磁盘紧急 → 等待或抛出。"""
        self.disk.assert_not_halted()
        if self.disk.state() is DiskState.EMERGENCY_CLEANUP:
            freed = self.cleaner.emergency_cleanup(logger=jlog)
            jlog.warning("触发紧急清理，释放 %.2f GB", freed / 1024 ** 3)
        waited = 0
        while not self.resources.ram_allows_new_stage():
            if waited == 0:
                jlog.warning("RAM 超过软限制 %d%%，等待释放",
                             self.resources.ram_soft_limit)
            time.sleep(5)
            waited += 5
            if waited > 600:
                raise DiskSpaceError("RAM 长时间超限，放弃启动新阶段")
            self.disk.assert_not_halted()

    # ------------------------------------------------------------------ #
    def _handle_failure(self, job: Job, exc: Exception) -> None:
        """失败处理（需求 #23）：可重试 → RETRY_PENDING；否则 FAILED_FINAL。"""
        jlog = get_job_logger(self.log_dir, job.job_id)
        jlog.error("任务失败: %s", exc)
        self.db.log_event(job.job_id, "ERROR", str(exc))
        self.db.set_error(job.job_id, str(exc))
        job = self.db.get_job(job.job_id)

        retryable = getattr(exc, "retryable", True)
        if isinstance(exc, DependencyError):
            retryable = False
        if retryable and job.retry_count < self.max_attempts - 1:
            self.db.increment_retry(job.job_id)
            self.db.update_status(job.job_id, JobStatus.RETRY_PENDING,
                                  error=str(exc))
            jlog.warning("进入 RETRY_PENDING (第 %d 次)", job.retry_count + 1)
        else:
            # 失败隔离：把工作区残留移到 failed/<job_id>/，不阻塞队列
            self._quarantine(job, jlog)
            self.db.update_status(job.job_id, JobStatus.FAILED_FINAL,
                                  error=str(exc))
            jlog.error("FAILED_FINAL")

    def _quarantine(self, job: Job, jlog) -> None:
        dst = self.failed_dir / f"job_{job.job_id:04d}"
        try:
            dst.mkdir(parents=True, exist_ok=True)
            current = self.work_dir / "current"
            if current.exists():
                for p in current.iterdir():
                    if p.is_file():
                        p.replace(dst / p.name)
        except OSError as exc:
            jlog.warning("隔离失败文件时出错: %s", exc)
