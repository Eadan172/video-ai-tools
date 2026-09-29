"""FFprobe 适配器（需求 #4 / #24）。

把 ffprobe 命令行封装在 adapter 层，业务代码只面对 MediaInfo。
"""

from __future__ import annotations

import json

from pipeline.errors import ProbeError
from pipeline.runner import run_command
from pipeline.state_machine import MediaInfo

from ._toolpath import resolve_ffmpeg, which_tool


class FFprobeAdapter:
    def __init__(self, executable: str = "ffprobe") -> None:
        # PATH 上没有 ffprobe 时回退到仓库自带的 .tools/ffmpeg（见 _toolpath）
        self.executable = resolve_ffmpeg(executable)

    def available(self) -> bool:
        return which_tool(self.executable) is not None

    def probe(self, path: str, timeout: float = 120) -> MediaInfo:
        """探测媒体文件。无法解析 → ProbeError（视为源文件损坏）。"""
        result = run_command(
            [self.executable, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            timeout=timeout, check=False)
        if result.returncode != 0:
            raise ProbeError(f"ffprobe 无法读取 {path}: {result.stderr[-500:]}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"ffprobe 输出无法解析: {path}") from exc
        return self._parse(path, data)

    @staticmethod
    def _parse(path: str, data: dict) -> MediaInfo:
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        info = MediaInfo(path=path)
        info.container = fmt.get("format_name", "")
        info.size = int(fmt.get("size", 0) or 0)
        info.bitrate = int(fmt.get("bit_rate", 0) or 0)
        try:
            info.duration = float(fmt.get("duration", 0) or 0)
        except (TypeError, ValueError):
            info.anomalies.append("duration 缺失")

        vstreams = [s for s in streams if s.get("codec_type") == "video"]
        astreams = [s for s in streams if s.get("codec_type") == "audio"]
        sstreams = [s for s in streams if s.get("codec_type") == "subtitle"]
        info.stream_count = len(streams)
        info.subtitle_count = len(sstreams)
        info.has_video = bool(vstreams)
        info.has_audio = bool(astreams)

        if vstreams:
            v = vstreams[0]
            info.video_codec = v.get("codec_name", "")
            # 兼容性三要素：用于判断成片能否在 Win/Android/iOS 默认播放器里播
            info.profile = v.get("profile", "") or ""
            info.pix_fmt = v.get("pix_fmt", "") or ""
            info.codec_tag = v.get("codec_tag_string", "") or ""
            info.width = int(v.get("width", 0) or 0)
            info.height = int(v.get("height", 0) or 0)
            fps_raw = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
            try:
                num, den = fps_raw.split("/")
                info.fps = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                info.fps = 0.0
        else:
            info.anomalies.append("无视频流")

        if astreams:
            a = astreams[0]
            info.audio_codec = a.get("codec_name", "")
            try:
                info.sample_rate = int(a.get("sample_rate", 0) or 0)
            except (TypeError, ValueError):
                info.sample_rate = 0
            info.channels = int(a.get("channels", 0) or 0)

        info.metadata = fmt.get("tags", {}) or {}
        if not info.has_video and not info.has_audio:
            info.anomalies.append("既无视频流也无音频流")
        return info
