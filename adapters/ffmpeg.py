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

from ._toolpath import resolve_ffmpeg, which_tool

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


#: 目标视频编码 → (profile, MP4 codec tag)。
#:
#: 这是「跨平台能不能播」的硬约束表，不是画质偏好：
#:   * Windows（Media Foundation）/ Android（MediaCodec）/ iOS（QuickTime）的
#:     默认播放器只保证 H.264 High 与 HEVC Main（8bit 4:2:0）。
#:   * -profile:v 用来堵住 High 4:4:4 Predictive / HEVC Rext 这类移动端解不了的档位。
#:   * tag 只对 mp4/mov 有意义：HEVC 必须是 hvc1（参数集进 hvcC box），
#:     ffmpeg 默认写的 hev1（参数集在码流内）会被大量硬件解码器直接拒收。
#:   * level 故意不写死：让编码器自动取「够用的最低 level」，反而兼容面更广
#:     （写死 4.1 会让高分片源因 level 不足而编码失败）。
#:     —— 但这条只保证了"标注正确"，不保证"设备放得出来"：分辨率一旦超过
#:     Level 4.1 的 MaxFS，产出就必然是不可播的，见下方 MOBILE_SAFE_* 与
#:     pipeline/verifier.py::compat_problems 的兜底判定。
_COMPAT_VIDEO = {
    "h264": ("high", "avc1"),
    "hevc": ("main", "hvc1"),
    "h265": ("main", "hvc1"),
}


#: 移动端安全上限 —— Level 4.1。
#:
#: 为什么是 4.1：安卓/苹果的硬件解码器普遍封顶 Level 4.1；而
#:   1920x1080 = 120x68 = 8160 宏块，刚好压在 H.264 Level 4.1 的 MaxFS 8192
#:   以内 —— 这正是「1080p 是通用可播上限」的根本原因。
#: 实测教训（本仓库真实事故）：2048x1536 = 128x96 = **12288 宏块/帧** > 8192，
#:   编码器只能标 Level 5.0（x264 会自动取"够用的最低 level"，但该分辨率根本
#:   塞不进 4.1），安卓平板默认播放器整帧拒解、报「格式不支持」，而同样参数
#:   降到 1024x768 后正常播放。
#: 注意：**不能**简单地把 -level 4.1 写死 —— 分辨率超过 MaxFS 时写死 4.1 会
#:   让编码直接失败。分辨率本身才是根因，故该约束由 verifier 判定。
MOBILE_SAFE_H264_MBS = 8192          # H.264 Level 4.1 的 MaxFS（宏块/帧）
#: `output.max_level` 的默认值 —— 即上面这个上限对应的 level 写法
MOBILE_SAFE_MAX_LEVEL = "4.1"


def frame_macroblocks(width: int, height: int) -> int:
    """按 16x16 宏块切分，返回每帧宏块数（不足一块按一块计）。"""
    return ((max(0, int(width)) + 15) // 16) * ((max(0, int(height)) + 15) // 16)


def level_tenths(codec: str, level: int) -> int:
    """把 ffprobe 的 level 归一到「十分之一 level」口径，便于跨编码比较。

    H.264 的 level_idc 本身就是 level×10（4.1 → 41）；
    HEVC 的 general_level_idc 是 level×30（4.1 → 123），故需除以 3。
    """
    lv = max(0, int(level or 0))
    if str(codec).lower() in ("hevc", "h265"):
        return lv // 3
    return lv


def parse_level(text: object) -> int:
    """把配置里的 level 写法解析成「十分之一 level」口径。

    "4.1" → 41，"4" → 40，"41" → 41；空/"0" → 0 表示不限制。
    """
    s = str(text or "").strip().rstrip("pP").split()[0] if str(text or "").strip() else ""
    if not s:
        return 0
    try:
        if "." in s:
            major, minor = s.split(".", 1)
            return int(major) * 10 + int(minor[0])
        val = int(float(s))
        return val if val > 10 else val * 10
    except (TypeError, ValueError):
        return 0


def compat_video_args(video_codec: str, container: str,
                      pix_fmt: str = "yuv420p") -> list[str]:
    """返回强制跨平台兼容的视频编码参数（pix_fmt / profile / codec tag）。

    这些参数是「能不能在默认播放器里播」的开关，任何转码路径都应带上，
    否则中间产物的 4:4:4 会被下游编码器原样继承，成片在移动端直接报
    "格式不支持"（详见 config.yaml 的 output 段说明）。
    """
    args = ["-pix_fmt", str(pix_fmt or "yuv420p")]
    profile, tag = _COMPAT_VIDEO.get(str(video_codec).lower(), (None, None))
    if profile:
        args += ["-profile:v", profile]
    if tag and str(container).lower() in _FASTSTART_CONTAINERS:
        args += ["-tag:v", tag]
    return args


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
        # PATH 上没有 ffmpeg 时回退到仓库自带的 .tools/ffmpeg（见 _toolpath）
        self.executable = resolve_ffmpeg(executable)
        self.cfg = encoder_cfg or {}
        self._encoders_cache: set[str] | None = None

    def available(self) -> bool:
        return which_tool(self.executable) is not None

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
                              container: str | None = None,
                              video_codec: str = "hevc",
                              pix_fmt: str = "yuv420p") -> list[str]:
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
        # 跨平台兼容硬约束：8bit 4:2:0 + 正确 profile / codec tag。
        # 不加这一步，上游中间产物的 4:4:4 会被编码器原样继承，成片在
        # 安卓 / iOS 默认播放器上直接报"格式不支持"（见 compat_video_args）。
        if video_codec in ("h264", "hevc", "h265"):
            args += compat_video_args(video_codec, container, pix_fmt)
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
                  container: str | None = None,
                  pix_fmt: str = "yuv420p") -> EncoderBackend:
        """转码到目标格式。

        backend=None 时按配置优先级 QSV → NVENC → CPU 逐个尝试：
        编码器在 `-encoders` 里存在 ≠ 硬件真的可用（如 ffmpeg 编了 libmfx
        但没有 Intel GPU），因此运行失败也要自动降级到下一个后端。

        :param vf: 可选的视频滤镜链（画质修复用）
        :param container: 目标容器；None 时按 dst 扩展名推断
        :param pix_fmt: 目标像素格式（兼容性硬约束，默认 yuv420p）
        """
        from pipeline.errors import ExternalToolError

        if backend is not None:
            encoder = _ENCODER_MAP[(video_codec, backend)]
            run_command(self._build_transcode_args(
                src, dst, encoder, backend, audio_codec, audio_bitrate,
                sample_rate, vf, container, video_codec, pix_fmt),
                timeout=timeout, log_file=log_file)
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
                    vf, container, video_codec, pix_fmt),
                    timeout=timeout, log_file=log_file)
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
