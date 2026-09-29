"""FFmpeg 适配器：跨平台兼容参数与转码命令拼装。

回归点是线上事故：成片在安卓默认播放器报"格式不支持"，
根因是 pix_fmt 变 4:4:4、HEVC 用了 hev1 tag（详见 config.yaml 的 output 段）。
"""

from __future__ import annotations

from pathlib import Path

from adapters.ffmpeg import (EncoderBackend, FFmpegAdapter, audio_bitrate_for,
                             compat_video_args, muxer_name)


class TestCompatVideoArgs:
    def test_default_is_420(self) -> None:
        assert compat_video_args("h264", "mp4")[0:2] == ["-pix_fmt", "yuv420p"]

    def test_h264_forces_high_and_avc1(self) -> None:
        args = compat_video_args("h264", "mp4")
        assert args[args.index("-profile:v") + 1] == "high"
        assert args[args.index("-tag:v") + 1] == "avc1"

    def test_hevc_uses_hvc1_never_hev1(self) -> None:
        args = compat_video_args("hevc", "mp4")
        assert args[args.index("-profile:v") + 1] == "main"
        assert args[args.index("-tag:v") + 1] == "hvc1"
        assert "hev1" not in args

    def test_tag_skipped_for_non_mp4_container(self) -> None:
        # mkv / webm 没有 codec tag 概念，传了会被 ffmpeg 报错
        assert "-tag:v" not in compat_video_args("hevc", "mkv")

    def test_explicit_pix_fmt_wins(self) -> None:
        assert compat_video_args("h264", "mp4", "yuv422p")[1] == "yuv422p"


class TestBuildTranscodeArgs:
    @staticmethod
    def _adapter() -> FFmpegAdapter:
        return FFmpegAdapter(executable="ffmpeg",
                             encoder_cfg={"crf": 23, "prefer": ["cpu"]})

    def test_h264_transcode_carries_compat_guard(self) -> None:
        args = self._adapter()._build_transcode_args(  # noqa: SLF001
            Path("in.mp4"), Path("out.mp4"), "libx264", EncoderBackend.CPU,
            "aac", "320k", 48000, video_codec="h264")
        assert args[args.index("-pix_fmt") + 1] == "yuv420p"
        assert args[args.index("-tag:v") + 1] == "avc1"
        assert "-movflags" in args and "+faststart" in args

    def test_hevc_transcode_carries_hvc1(self) -> None:
        args = self._adapter()._build_transcode_args(  # noqa: SLF001
            Path("in.mp4"), Path("out.mp4"), "libx265", EncoderBackend.CPU,
            "aac", "320k", 48000, video_codec="hevc")
        assert args[args.index("-tag:v") + 1] == "hvc1"

    def test_vp9_gets_no_avc_tag(self) -> None:
        # vp9 走 webm，兼容表里没有它 → 只补 pix_fmt，不补 profile/tag
        args = self._adapter()._build_transcode_args(  # noqa: SLF001
            Path("in.mp4"), Path("out.webm"), "libvpx-vp9", EncoderBackend.CPU,
            "libopus", "256k", 48000, video_codec="vp9", container="webm")
        assert "-profile:v" not in args
        assert "-tag:v" not in args


class TestHelpers:
    def test_muxer_name(self) -> None:
        assert muxer_name("mkv") == "matroska"
        assert muxer_name("mp4") == "mp4"

    def test_audio_bitrate_clamped_for_opus(self) -> None:
        assert audio_bitrate_for("libopus", "320k") == "256k"
        assert audio_bitrate_for("aac", "320k") == "320k"
