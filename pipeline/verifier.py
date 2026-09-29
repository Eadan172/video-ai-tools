"""最终输出完整性校验（需求 #20）+ 智能跳过转码判断（需求 #9）。"""

from __future__ import annotations

from adapters.ffprobe import FFprobeAdapter
from adapters.ffmpeg import (MOBILE_SAFE_H264_MBS, MOBILE_SAFE_MAX_LEVEL,
                             frame_macroblocks, level_tenths, parse_level)
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
    def compat_problems(self, info: MediaInfo) -> list[str]:
        """跨平台播放兼容性检查（Windows / Android / iOS 默认播放器）。

        这些不是画质偏好，而是"能不能播"：
          * 4:4:4（yuv444p 等）→ 移动端硬解与软解一律不支持，播放器报
            "格式不支持"；上游中间产物一旦是 4:4:4，会一路继承到成片。
          * HEVC 的 Rext profile（Range Extensions）同样是 4:4:4 家族。
          * HEVC 必须用 **hvc1** tag。ffmpeg 默认写 hev1（参数集在码流内），
            大量安卓/iOS 硬件解码器只认 hvc1（参数集在 hvcC box）而直接拒收。

        字段为空（探测不到）时不判失败，避免对来源不明的文件误报。
        """
        if not info.has_video:
            return []
        problems: list[str] = []
        want_pix = str(self.output_cfg.get("pix_fmt", "yuv420p"))
        if info.pix_fmt and info.pix_fmt != want_pix:
            problems.append(
                f"像素格式不兼容移动端: {info.pix_fmt} != {want_pix}"
                "（4:4:4 / Rext 无法解码）")
        codec = (info.video_codec or "").lower()
        if codec in ("hevc", "h265"):
            if info.profile and info.profile.lower() != "main":
                problems.append(
                    f"HEVC profile 不兼容: {info.profile}（需 Main）")
            if info.codec_tag and info.codec_tag != "hvc1":
                problems.append(
                    f"HEVC codec tag 不兼容: {info.codec_tag}（需 hvc1，"
                    "hev1 会被硬件解码器拒收）")
        elif codec == "h264" and info.codec_tag and info.codec_tag != "avc1":
            problems.append(
                f"H.264 codec tag 不兼容: {info.codec_tag}（需 avc1）")

        # Level / 分辨率上限：超过 Level 4.1 的成片在移动端会被**整帧拒解**。
        # 实测事故：2048x1536 = 12288 宏块/帧 > 4.1 的 MaxFS 8192，编码器只能标
        # Level 5.0，安卓平板默认播放器报「格式不支持」；同参数降到 1024x768
        # （3072 宏块 / L3.1）即正常。这是"能不能播"的硬约束，不是画质偏好。
        cap = parse_level(self.output_cfg.get("max_level", MOBILE_SAFE_MAX_LEVEL))
        if codec in ("h264", "avc", "hevc", "h265") and info.level and cap:
            lv = level_tenths(codec, info.level)
            if lv > cap:
                problems.append(
                    f"编码 level 超出移动端上限: L{lv / 10:.1f} > "
                    f"L{cap / 10:.1f}（安卓/iOS 硬解普遍封顶 4.1）")
            elif codec in ("h264", "avc") and info.width and info.height:
                mbs = frame_macroblocks(info.width, info.height)
                if mbs > MOBILE_SAFE_H264_MBS:
                    problems.append(
                        f"分辨率超出 H.264 Level 4.1 上限: "
                        f"{info.width}x{info.height} = {mbs} 宏块/帧 > "
                        f"{MOBILE_SAFE_H264_MBS} 宏块/帧（移动端会拒解；"
                        "请降分辨率或改用 HEVC）")
        return problems

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
        # 跨平台播放兼容性（profile / pix_fmt / codec tag）
        if self.require_video:
            problems.extend(self.compat_problems(info))

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
        # 兼容性也要过：否则会把 AI 中间产物（可能是 yuv444p）直接当成片导出
        compat_ok = not self.compat_problems(info)
        return container_ok and v_ok and a_ok and sr_ok and compat_ok
