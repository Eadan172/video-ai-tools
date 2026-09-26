"""FFmpeg 适配器 + 硬件编码器检测（需求 #8 / #9 / #10）。

编码后端优先级：Intel QSV → NVIDIA NVENC → CPU（libx265/libx264）。
运行时通过 `ffmpeg -encoders` 实际检测，绝不假设 QSV/NVENC 存在。
"""

from __future__ import annotations

import enum
import logging
import shutil
from pathlib import Path

from pipeline.errors import DependencyError
from pipeline.runner import run_command

log = logging.getLogger("adapters.ffmpeg")


class EncoderBackend(enum.Enum):
    QSV = "qsv"
    NVENC = "nvenc"
    CPU = "cpu"


#: (目标 codec, 后端) -> ffmpeg encoder 名
#: 表中存在但本机 ffmpeg 未编译的编码器会被 select_encoder / transcode 自动跳过
_ENCODER_MAP = {
    ("hevc", EncoderBackend.QSV): "hevc_qsv",
    ("hevc", EncoderBackend.NVENC): "hevc_nvenc",
    ("hevc", EncoderBackend.CPU): "libx265",
    ("h264", EncoderBackend.QSV): "h264_qsv",
    ("h264", EncoderBackend.NVENC): "h264_nvenc",
    ("h264", EncoderBackend.CPU): "libx264",
    ("av1", EncoderBackend.QSV): "av1_qsv",
    ("av1", EncoderBackend.CPU): "libsvtav1",
    ("vp9", EncoderBackend.QSV): "vp9_qsv",
    ("vp9", EncoderBackend.CPU): "libvpx-vp9",
}

#: 需要 `-movflags +faststart` 的容器（faststart 只对 mp4/mov 系有效）
_FASTSTART_CONTAINERS = {"mp4", "mov", "m4v"}

#: 容器名 → ffmpeg muxer 名（`-f` 的取值；部分容器名与 muxer 名不一致）
_MUXER_NAMES = {
    "mkv": "matroska",      # mkv 不是合法 muxer 名，必须写 matroska
    "mp4": "mp4",
    "mov": "mov",
    "webm": "webm",
    "avi": "avi",
}

#: 目标音频编码 → ffmpeg 编码器名
#: ffmpeg 内置的 `opus` 是实验性编码器，直接 `-c:a opus` 会报
#: "experimental codecs are not enabled"，必须用 libopus。
_AUDIO_ENCODERS = {"opus": "libopus"}


#: 各音频编码器的比特率上限（kbps）；超出会被 ffmpeg 直接拒绝
_BITRATE_LIMIT_KBPS = {"libopus": 256}


def muxer_name(container: str) -> str:
    """把用户可见的容器名翻译成 ffmpeg 的 muxer 名。"""
    key = str(container).lower()
    return _MUXER_NAMES.get(key, key)


def audio_encoder_name(audio_codec: str) -> str:
    """把目标音频编码翻译成可用的 ffmpeg 编码器名。"""
    key = str(audio_codec).lower()
    return _AUDIO_ENCODERS.get(key, key)


def _bitrate_kbps(bitrate: str) -> int:
    """把 '320k' / '320000' 之类的写法解析为 kbps。"""
    s = str(bitrate).strip().lower().rstrip("b")
    try:
        return int(float(s[:-1])) if s.endswith("k") else int(float(s) / 1000)
    except ValueError:
        return 0


def audio_bitrate_for(encoder: str, bitrate: str) -> str:
    """按编码器上限收敛比特率（如 libopus 最高 256k，写 320k 会报错）。"""
    limit = _BITRATE_LIMIT_KBPS.get(str(encoder).lower())
    kbps = _bitrate_kbps(bitrate)
    if limit and kbps > limit:
        return f"{limit}k"
    return bitrate


class FFmpegAdapter:
    def __init__(self, executable: str = "ffmpeg",
                 encoder_cfg: dict | None = None) -> None:
        self.executable = executable
        self.cfg = encoder_cfg or {}
        self._encoders_cache: set[str] | None = None

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    # ------------------------------------------------------------------ #
    # 编码器检测
    # ------------------------------------------------------------------ #
    def list_encoders(self) -> set[str]:
        if self._encoders_cache is not None:
            return self._encoders_cache
        if not self.available():
            raise DependencyError("ffmpeg 未安装或不在 PATH")
        result = run_command([self.executable, "-hide_banner", "-encoders"],
                             timeout=60, check=True)
        encoders: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith(("V", "A", "S")):
                encoders.add(parts[1])
        self._encoders_cache = encoders
        return encoders

    def select_encoder(self, video_codec: str) -> tuple[EncoderBackend, str]:
        """按配置优先级选择可用编码器。全部不可用 → DependencyError。"""
        encoders = self.list_encoders()
        prefer = self.cfg.get("prefer", ["qsv", "nvenc", "cpu"])
        for name in prefer:
            backend = EncoderBackend(name)
            enc = _ENCODER_MAP.get((video_codec, backend))
            if enc and enc in encoders:
                log.info("编码器选择: %s (%s)", enc, backend.value)
                return backend, enc
        raise DependencyError(
            f"没有可用的 {video_codec} 编码器（尝试过: {prefer}）")

    # ------------------------------------------------------------------ #
    # 基本操作
    # ------------------------------------------------------------------ #
    def extract_audio(self, src: Path, dst: Path, sample_rate: int = 48000,
                      timeout: float = 7200, log_file: Path | None = None) -> None:
        """抽取音轨为 PCM WAV（DeepFilterNet 的输入）。"""
        run_command([self.executable, "-y", "-i", str(src),
                     "-vn", "-acodec", "pcm_s16le",
                     "-ar", str(sample_rate), str(dst)],
                    timeout=timeout, log_file=log_file)

    def _build_transcode_args(self, src: Path, dst: Path, encoder: str,
                              backend: EncoderBackend, audio_codec: str,
                              audio_bitrate: str, sample_rate: int,
                              vf: str | None = None,
                              container: str | None = None) -> list[str]:
        crf = str(self.cfg.get("crf", 23))
        container = (container or Path(dst).suffix.lstrip(".") or "mp4").lower()
        args = [self.executable, "-y", "-i", str(src)]
        if vf:
            # 画质修复滤镜链（去块/降噪/放大/锐化）在编码前串联
            args += ["-vf", vf]
        if backend is EncoderBackend.QSV:
            preset = self.cfg.get("qsv_preset", "medium")
            args += ["-c:v", encoder, "-preset", preset, "-global_quality", crf]
        elif backend is EncoderBackend.NVENC:
            args += ["-c:v", encoder, "-preset", "p5", "-cq", crf]
        else:
            preset = self.cfg.get("cpu_preset", "medium")
            args += ["-c:v", encoder]
            if encoder == "libvpx-vp9":
                # libvpx 的恒定质量模式（-crf 需与 -b:v 0 搭配）
                args += ["-crf", crf, "-b:v", "0"]
            else:
                args += ["-preset", preset, "-crf", crf]
        enc_a = audio_encoder_name(audio_codec)
        args += ["-c:a", enc_a,
                 "-b:a", audio_bitrate_for(enc_a, audio_bitrate),
                 "-ar", str(sample_rate)]
        if container in _FASTSTART_CONTAINERS:
            args += ["-movflags", "+faststart"]
        args += [str(dst)]
        return args

    def transcode(self, src: Path, dst: Path, video_codec: str = "hevc",
                  audio_codec: str = "aac", audio_bitrate: str = "320k",
                  sample_rate: int = 48000, backend: EncoderBackend | None = None,
                  timeout: float = 14400, log_file: Path | None = None,
                  vf: str | None = None,
                  container: str | None = None) -> EncoderBackend:
        """转码到目标格式。

        backend=None 时按配置优先级 QSV → NVENC → CPU 逐个尝试：
        编码器在 `-encoders` 里存在 ≠ 硬件真的可用（如 ffmpeg 编了 libmfx
        但没有 Intel GPU），因此运行失败也要自动降级到下一个后端。

        :param vf: 可选的视频滤镜链（画质修复用）
        :param container: 目标容器；None 时按 dst 扩展名推断
        """
        from pipeline.errors import ExternalToolError

        if backend is not None:
            encoder = _ENCODER_MAP[(video_codec, backend)]
            run_command(self._build_transcode_args(
                src, dst, encoder, backend, audio_codec, audio_bitrate,
                sample_rate, vf, container), timeout=timeout, log_file=log_file)
            return backend

        encoders = self.list_encoders()
        prefer = self.cfg.get("prefer", ["qsv", "nvenc", "cpu"])
        candidates: list[tuple[EncoderBackend, str]] = []
        for name in prefer:
            b = EncoderBackend(name)
            enc = _ENCODER_MAP.get((video_codec, b))
            if enc and enc in encoders:
                candidates.append((b, enc))
        if not candidates:
            raise DependencyError(
                f"没有可用的 {video_codec} 编码器（尝试过: {prefer}）")

        last_exc: Exception | None = None
        for b, enc in candidates:
            try:
                log.info("尝试编码后端 %s (%s)", enc, b.value)
                run_command(self._build_transcode_args(
                    src, dst, enc, b, audio_codec, audio_bitrate, sample_rate,
                    vf, container), timeout=timeout, log_file=log_file)
                return b
            except ExternalToolError as exc:
                log.warning("编码后端 %s 失败，降级尝试下一个: %s", enc, exc)
                last_exc = exc
                if dst.exists():
                    dst.unlink()  # 删除失败残留
        raise DependencyError(f"所有编码后端均失败: {last_exc}")

    def mux(self, video: Path, audio: Path | None, dst: Path,
            audio_codec: str = "aac", audio_bitrate: str = "320k",
            sample_rate: int = 48000, container: str = "mp4",
            timeout: float = 7200, log_file: Path | None = None) -> None:
        """最终合流：视频流一律 -c:v copy，绝不二次编码（需求 #10）。

        dst 可能是 .partial 后缀（atomic output），无法从扩展名推断格式，
        因此显式传 -f <container>。
        """
        args = [self.executable, "-y", "-i", str(video)]
        if audio is not None:
            enc_a = audio_encoder_name(audio_codec)
            args += ["-i", str(audio),
                     "-map", "0:v:0", "-map", "1:a:0",
                     "-c:v", "copy",
                     "-c:a", enc_a,
                     "-b:a", audio_bitrate_for(enc_a, audio_bitrate),
                     "-ar", str(sample_rate), "-shortest"]
        else:
            args += ["-map", "0", "-c", "copy"]
        if container.lower() in _FASTSTART_CONTAINERS:
            # faststart 只对 mp4/mov 系有效，mkv/webm/avi 传了会直接报错
            args += ["-movflags", "+faststart"]
        args += ["-f", muxer_name(container), str(dst)]
        run_command(args, timeout=timeout, log_file=log_file)

    def remux_lossless_intermediate(self, src: Path, dst: Path,
                                    timeout: float = 7200,
                                    log_file: Path | None = None) -> None:
        """仅在输入无法被 AI 工具直接读取时按需生成 FFV1+PCM 中间文件。

        默认禁止使用（intermediate.lossless=false 时调度器不会走到这里）。
        """
        run_command([self.executable, "-y", "-i", str(src),
                     "-c:v", "ffv1", "-level", "3",
                     "-c:a", "pcm_s16le", str(dst)],
                    timeout=timeout, log_file=log_file)
