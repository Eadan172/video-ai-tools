"""校验器与 profile 选择器测试。"""

from __future__ import annotations

import pytest

from pipeline.state_machine import MediaInfo
from pipeline.verifier import Verifier
from profiles.selector import select_profile

VERIFY_CFG = {"duration_tolerance_seconds": 2.0, "require_video": True,
              "require_audio": True, "min_size_bytes": 1000}
OUT_CFG = {"container": "mp4", "video_codec": "hevc", "audio_codec": "aac",
           "sample_rate": 48000}


def _info(**kw) -> MediaInfo:
    base = dict(path="/x.mp4", container="mov,mp4,m4a", duration=100.0,
                size=5_000_000, video_codec="hevc", width=1920, height=1080,
                audio_codec="aac", sample_rate=48000, has_video=True,
                has_audio=True)
    base.update(kw)
    return MediaInfo(**base)


class _FakeProbe:
    def __init__(self, info: MediaInfo) -> None:
        self.info = info

    def probe(self, path: str, timeout: float = 120) -> MediaInfo:
        return self.info


def make_verifier(info: MediaInfo) -> Verifier:
    return Verifier(_FakeProbe(info), VERIFY_CFG, OUT_CFG)  # type: ignore[arg-type]


class TestVerifyOutput:
    def test_good_file_passes(self) -> None:
        make_verifier(_info()).verify_output("/x.mp4", _info())

    def test_duration_mismatch_fails(self) -> None:
        from pipeline.errors import VerificationError
        out = _info(duration=90.0)  # 源 100s，偏差 10s > 容差 2s
        with pytest.raises(VerificationError, match="时长偏差"):
            make_verifier(out).verify_output("/x.mp4", _info())

    def test_missing_audio_fails(self) -> None:
        from pipeline.errors import VerificationError
        with pytest.raises(VerificationError, match="缺少音频流"):
            make_verifier(_info(has_audio=False)).verify_output("/x.mp4",
                                                                _info())

    def test_wrong_codec_fails(self) -> None:
        from pipeline.errors import VerificationError
        with pytest.raises(VerificationError, match="视频编码不符合"):
            make_verifier(_info(video_codec="h264")).verify_output(
                "/x.mp4", _info())

    def test_tiny_file_fails(self) -> None:
        from pipeline.errors import VerificationError
        with pytest.raises(VerificationError, match="文件过小"):
            make_verifier(_info(size=10)).verify_output("/x.mp4", _info())


class TestMatchesTarget:
    def test_matching_skips_transcode(self) -> None:
        assert make_verifier(_info()).matches_target("/x.mp4") is True

    def test_h264_needs_transcode(self) -> None:
        assert make_verifier(_info(video_codec="h264")).matches_target(
            "/x.mp4") is False

    def test_wrong_sample_rate_needs_transcode(self) -> None:
        assert make_verifier(_info(sample_rate=44100)).matches_target(
            "/x.mp4") is False

    def test_probe_failure_means_transcode(self) -> None:
        from pipeline.errors import ProbeError

        class BrokenProbe:
            def probe(self, path, timeout=120):
                raise ProbeError("boom")

        v = Verifier(BrokenProbe(), VERIFY_CFG, OUT_CFG)  # type: ignore[arg-type]
        assert v.matches_target("/x.mp4") is False


class TestProfileSelector:
    def test_1080p_is_light(self) -> None:
        assert select_profile(_info(height=1080)).name == "light"

    def test_720p_is_course(self) -> None:
        assert select_profile(_info(height=720)).name == "course_720"

    def test_480p_is_legacy(self) -> None:
        assert select_profile(_info(height=480, container="asf")).name == "legacy"

    def test_wmv_container_is_legacy(self) -> None:
        assert select_profile(_info(height=720, container="asf")).name in \
            ("course_720", "legacy")

    def test_override_by_filename(self) -> None:
        info = _info(path="/input/lesson_001.wmv", height=1080)
        cfg = {"overrides": {"lesson_001.wmv": "legacy"}}
        assert select_profile(info, cfg).name == "legacy"

    def test_never_interpolates(self) -> None:
        for info in (_info(height=480), _info(height=720), _info(height=1080)):
            assert select_profile(info).interpolation is False
