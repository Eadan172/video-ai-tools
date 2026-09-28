"""工具路径解析：PATH 上没有 ffmpeg/ffprobe 时回退到仓库自带 .tools/ffmpeg。

背景：适配器默认用裸命令名 "ffmpeg"，而 PATH 由启动方式决定 —— 双击 run.bat
会带上 .tools/ffmpeg，直接 `python main.py run` 不会，于是 REPAIR_AUDIO 的第一
步就报「找不到可执行文件: ffmpeg」。这组测试守住兜底逻辑。
"""

from __future__ import annotations

import adapters._toolpath as tp
from adapters.ffmpeg import FFmpegAdapter
from adapters.ffprobe import FFprobeAdapter


class TestResolveFfmpeg:
    def test_path_wins(self, monkeypatch) -> None:
        monkeypatch.setattr(tp.shutil, "which",
                            lambda _n: r"C:\somewhere\ffmpeg.exe")
        assert tp.resolve_ffmpeg("ffmpeg") == r"C:\somewhere\ffmpeg.exe"

    def test_falls_back_to_bundled(self, monkeypatch, tmp_path) -> None:
        fake = tmp_path / "ffmpeg.exe"
        fake.write_bytes(b"")
        monkeypatch.setattr(tp.shutil, "which", lambda _n: None)
        monkeypatch.setattr(tp, "resolve_bundled", lambda _n: str(fake))
        assert tp.resolve_ffmpeg("ffmpeg") == str(fake)

    def test_returns_name_when_nothing_found(self, monkeypatch) -> None:
        monkeypatch.setattr(tp.shutil, "which", lambda _n: None)
        monkeypatch.setattr(tp, "resolve_bundled", lambda _n: None)
        assert tp.resolve_ffmpeg("ffmpeg") == "ffmpeg"

    def test_explicit_path_is_left_alone(self) -> None:
        p = r"E:\some\where\ffmpeg.exe"
        assert tp.resolve_ffmpeg(p) == p

    def test_adapters_use_the_resolver(self, monkeypatch) -> None:
        """适配器默认构造也必须走兜底，否则换个启动方式就整批失败。"""
        monkeypatch.setattr(tp.shutil, "which", lambda _n: None)
        monkeypatch.setattr(tp, "resolve_bundled",
                            lambda n: r"C:\bundled\%s.exe" % n)
        assert FFmpegAdapter().executable == r"C:\bundled\ffmpeg.exe"
        assert FFprobeAdapter().executable == r"C:\bundled\ffprobe.exe"
