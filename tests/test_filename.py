"""文件名清洗测试（需求 #21）。"""

from __future__ import annotations

from pathlib import Path

from pipeline.filename import sanitize_filename, unique_output_path


class TestSanitize:
    def test_chinese_preserved(self) -> None:
        # 全角字符（：等）在 Windows 合法，必须保留
        assert sanitize_filename("第01讲：课程介绍 (1080P)") == \
            "第01讲：课程介绍 (1080P)"

    def test_halfwidth_colon_replaced(self) -> None:
        assert sanitize_filename("第01讲: 课程介绍") == "第01讲_ 课程介绍"

    def test_invalid_chars_removed(self) -> None:
        for ch in '<>:"/\\|?*':
            assert ch not in sanitize_filename(f"a{ch}b")

    def test_trailing_dots_and_spaces(self) -> None:
        assert sanitize_filename("name. ") == "name"
        assert sanitize_filename("  name  ") == "name"

    def test_empty_becomes_unnamed(self) -> None:
        assert sanitize_filename("...") == "unnamed"
        assert sanitize_filename("") == "unnamed"

    def test_reserved_device_names(self) -> None:
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("nul") == "_nul"

    def test_max_length(self) -> None:
        assert len(sanitize_filename("x" * 500)) <= 120

    def test_control_chars(self) -> None:
        assert "\x00" not in sanitize_filename("a\x00b\x1f")


class TestUniqueOutputPath:
    def test_no_conflict(self, tmp_path: Path) -> None:
        p = unique_output_path(tmp_path, "lesson_001", ".mp4")
        assert p.name == "lesson_001.mp4"

    def test_conflict_appends_counter(self, tmp_path: Path) -> None:
        (tmp_path / "lesson_001.mp4").touch()
        p1 = unique_output_path(tmp_path, "lesson_001", ".mp4")
        assert p1.name == "lesson_001_1.mp4"
        p1.touch()
        p2 = unique_output_path(tmp_path, "lesson_001", ".mp4")
        assert p2.name == "lesson_001_2.mp4"

    def test_chinese_stem(self, tmp_path: Path) -> None:
        p = unique_output_path(tmp_path, "第01讲：课程介绍", ".mp4")
        assert "第01讲" in p.name and ":" not in p.name
