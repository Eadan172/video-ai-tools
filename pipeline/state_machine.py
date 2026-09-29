"""任务状态机与数据模型。

状态机（见需求 #2）：

    DISCOVERED
    → VALIDATING
    → WAIT_RESOURCE
    → REPAIR_VIDEO
    → REPAIR_AUDIO
    → PREPARE_TRANSCODE
    → TRANSCODE
    → EXPORT
    → VERIFY
    → DONE

任何阶段失败 → RETRY_PENDING → 重试 → FAILED_FINAL
某阶段已满足条件 → 该阶段记为 SKIPPED，直接进入下一阶段
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStatus(str, enum.Enum):
    """任务的宏观状态（存 jobs.status）。"""

    DISCOVERED = "DISCOVERED"           # 扫描入库，尚未校验
    WAITING = "WAITING"                 # 校验通过，排队等待资源
    WAIT_RESOURCE = "WAIT_RESOURCE"     # 资源不足，等待磁盘/RAM/GPU
    WAIT_DISK = "WAIT_DISK"             # 磁盘不足（单独列出便于 status 统计）
    RUNNING = "RUNNING"                 # 正在执行某个 stage
    RETRY_PENDING = "RETRY_PENDING"     # 阶段失败，等待重试
    DONE = "DONE"
    FAILED_FINAL = "FAILED_FINAL"


class Stage(str, enum.Enum):
    """任务的微观阶段（存 jobs.stage）。"""

    NONE = "NONE"
    VALIDATING = "VALIDATING"
    REPAIR_VIDEO = "REPAIR_VIDEO"
    REPAIR_AUDIO = "REPAIR_AUDIO"
    PREPARE_TRANSCODE = "PREPARE_TRANSCODE"
    TRANSCODE = "TRANSCODE"
    EXPORT = "EXPORT"
    VERIFY = "VERIFY"


#: 阶段执行顺序（VALIDATING 之后的流水线阶段）
PIPELINE_STAGES: list[Stage] = [
    Stage.REPAIR_VIDEO,
    Stage.REPAIR_AUDIO,
    Stage.PREPARE_TRANSCODE,
    Stage.TRANSCODE,
    Stage.EXPORT,
    Stage.VERIFY,
]


class StageResult(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


#: 允许的状态迁移（防御性校验，防止把 DONE 任务拉回 RUNNING 等错误）
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.DISCOVERED: {JobStatus.WAITING, JobStatus.FAILED_FINAL,
                           JobStatus.RETRY_PENDING, JobStatus.RUNNING},
    JobStatus.WAITING: {JobStatus.RUNNING, JobStatus.WAIT_RESOURCE,
                        JobStatus.WAIT_DISK, JobStatus.FAILED_FINAL},
    JobStatus.WAIT_RESOURCE: {JobStatus.WAITING, JobStatus.RUNNING,
                              JobStatus.WAIT_DISK},
    JobStatus.WAIT_DISK: {JobStatus.WAITING, JobStatus.RUNNING},
    JobStatus.RUNNING: {JobStatus.DONE, JobStatus.RETRY_PENDING,
                        JobStatus.FAILED_FINAL, JobStatus.WAIT_DISK,
                        JobStatus.WAITING},
    JobStatus.RETRY_PENDING: {JobStatus.WAITING, JobStatus.RUNNING,
                              JobStatus.FAILED_FINAL},
    JobStatus.DONE: set(),
    JobStatus.FAILED_FINAL: {JobStatus.RETRY_PENDING},  # 手动 retry 命令
}


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, set())


@dataclass
class Job:
    """jobs 表的一行。"""

    job_id: int
    source_path: str
    source_size: int
    status: JobStatus
    stage: Stage
    retry_count: int = 0
    priority: int = 0
    profile: str = "auto"
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    last_error: str = ""
    gpu: str = ""
    output_path: str = ""
    workspace_path: str = ""
    metadata_json: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        d = dict(row)
        d["status"] = JobStatus(d["status"])
        d["stage"] = Stage(d["stage"])
        return cls(**d)


@dataclass
class MediaInfo:
    """ffprobe 探测结果（阶段一的产物，序列化进 jobs.metadata_json）。"""

    path: str
    container: str = ""
    duration: float = 0.0
    bitrate: int = 0
    size: int = 0
    video_codec: str = ""
    # 跨平台播放兼容性三要素（缺一则移动端默认播放器可能拒播）：
    #   profile   —— 堵住 H.264 High 4:4:4 Predictive / HEVC Rext
    #   pix_fmt   —— 必须是 yuv420p（8bit 4:2:0）
    #   codec_tag —— HEVC 需 hvc1（ffmpeg 默认的 hev1 会被硬件解码器拒收）
    profile: str = ""
    pix_fmt: str = ""
    codec_tag: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    stream_count: int = 0
    subtitle_count: int = 0
    has_video: bool = False
    has_audio: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
