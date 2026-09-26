"""最终输出完整性校验（需求 #20）+ 智能跳过转码判断（需求 #9）。"""

from __future__ import annotations

from adapters.ffprobe import FFprobeAdapter
from pipeline.errors import VerificationError
from pipeline.state_machine import MediaInfo


class Verifier:
    def __init__(self, ffprobe: FFprobeAdapter, cfg: dict, output_cfg: dict) -> None:
        self.ffprobe = ffprobe
        self.duration_tolerance = float(cfg.get("duration_tolerance_seconds", 2.0))
        self.require_video = bool(cfg.get("require_video", True))
        self.require_audio = bool(cfg.get("require_audio", True))
        self.min_size_bytes = int(cfg.get("min_size_bytes", 10_000))
        self.output_cfg = output_cfg

    # ------------------------------------------------------------------ #
    def verify_output(self, out_path: str, source: MediaInfo) -> MediaInfo:
        """校验最终文件。任何一项不满足 → VerificationError。"""
        info = self.ffprobe.probe(out_path)
        problems: list[str] = []

        if info.size < self.min_size_bytes:
            problems.append(f"文件过小: {info.size} bytes")
        if info.duration <= 0:
            problems.append("duration <= 0")
        if self.require_video and not info.has_video:
            problems.append("缺少视频流")
        if self.require_audio and not info.has_audio:
            problems.append("缺少音频流")
        if source.duration > 0 and info.duration > 0:
            if abs(info.duration - source.duration) > self.duration_tolerance:
                problems.append(
                    f"时长偏差过大: 源 {source.duration:.2f}s vs "
                    f"输出 {info.duration:.2f}s")
        # codec / sample rate 需符合目标配置
        want_vcodec = self.output_cfg.get("video_codec", "hevc")
        codec_alias = {"hevc": {"hevc", "h265"}, "h264": {"h264", "avc"}}
        if self.require_video and info.video_codec:
            allowed = codec_alias.get(want_vcodec, {want_vcodec})
            if info.video_codec not in allowed:
                problems.append(
                    f"视频编码不符合目标: {info.video_codec} != {want_vcodec}")
        want_sr = int(self.output_cfg.get("sample_rate", 48000))
        if self.require_audio and info.sample_rate and info.sample_rate != want_sr:
            problems.append(f"采样率不符合目标: {info.sample_rate} != {want_sr}")

        if problems:
            raise VerificationError(f"输出校验失败 {out_path}: " + "; ".join(problems))
        return info

    # ------------------------------------------------------------------ #
    def matches_target(self, path: str) -> bool:
        """智能跳过转码：文件已满足目标格式 → True（TRANSCODE = SKIP）。"""
        try:
            info = self.ffprobe.probe(path)
        except Exception:  # noqa: BLE001 —— 探测失败保守处理为"需要转码"
            return False
        want_container = self.output_cfg.get("container", "mp4")
        want_vcodec = self.output_cfg.get("video_codec", "hevc")
        want_acodec = self.output_cfg.get("audio_codec", "aac")
        want_sr = int(self.output_cfg.get("sample_rate", 48000))
        codec_alias = {"hevc": {"hevc", "h265"}, "h264": {"h264", "avc"}}

        container_ok = want_container in (info.container or "").lower()
        v_ok = info.video_codec in codec_alias.get(want_vcodec, {want_vcodec})
        a_ok = (info.audio_codec or "").lower() == want_acodec.lower()
        sr_ok = (not info.has_audio) or info.sample_rate == want_sr
        return container_ok and v_ok and a_ok and sr_ok
