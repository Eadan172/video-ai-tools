"""自定义异常层次结构。

所有流水线内部的错误都继承自 PipelineError，便于上层统一捕获、
分类记录日志并决定重试策略。
"""

from __future__ import annotations


class PipelineError(Exception):
    """流水线基础异常。"""

    #: 该类错误是否值得重试（源文件损坏等永久性错误应置 False）
    retryable: bool = True


class ConfigError(PipelineError):
    """配置文件缺失或非法。"""

    retryable = False


class DependencyError(PipelineError):
    """外部依赖（FFmpeg / AI 工具等）缺失或不可用。"""

    retryable = False


class ProbeError(PipelineError):
    """ffprobe 探测失败（文件损坏、无法解码等）。"""

    retryable = False  # 源文件损坏 → FAILED_FINAL


class ExternalToolError(PipelineError):
    """外部命令返回非零退出码。"""

    def __init__(self, message: str, returncode: int = -1,
                 stderr_tail: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class GpuOutOfMemoryError(ExternalToolError):
    """GPU 显存不足 —— 需要降低 tile/batch 后重试。"""


class DiskSpaceError(PipelineError):
    """磁盘空间不足，任务进入 WAIT_DISK 而不是失败。"""


class EmergencyStopError(PipelineError):
    """磁盘低于紧急阈值，整条流水线暂停。"""


class ResourceBusyError(PipelineError):
    """RAM/GPU 等资源暂不满足启动条件。"""


class VerificationError(PipelineError):
    """最终输出未通过 ffprobe 完整性校验。"""
