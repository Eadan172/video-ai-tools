"""处理策略自动选择（需求 #28）。

根据 ffprobe 结果生成 profile：
- light:       >=1080p，质量好 —— 不超分或轻度
- course_720:  720p 左右 —— 2x 超分 + 轻度降噪
- legacy:      <=480p / 老 WMV/AVI —— 去压缩 + 降噪 + 2x

profile 决定超分倍率与是否降噪；可在 config.yaml profiles.overrides 中
按文件名覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.state_machine import MediaInfo


@dataclass
class Profile:
    name: str
    upscale_scale: int      # 1 = 不放大
    denoise: bool
    deblock: bool
    interpolation: bool = False   # 默认永不插帧


PROFILES: dict[str, Profile] = {
    "light": Profile("light", upscale_scale=1, denoise=True, deblock=False),
    "course_720": Profile("course_720", upscale_scale=2, denoise=True, deblock=True),
    "legacy": Profile("legacy", upscale_scale=2, denoise=True, deblock=True),
}


def select_profile(info: MediaInfo, cfg: dict | None = None) -> Profile:
    """按媒体信息自动选择 profile；config 中可按文件名覆盖。"""
    cfg = cfg or {}
    overrides = cfg.get("overrides", {})
    stem = Path(info.path).name
    if stem in overrides:
        return PROFILES.get(overrides[stem], PROFILES["legacy"])

    legacy_containers = {"asf", "wmv", "avi"}  # wmv 的 container_name 常为 asf
    height = info.height
    if height and height >= 1080:
        return PROFILES["light"]
    # 老容器优先于分辨率判断：1024x768 的 .wmv 也应按 WMV 处理
    # （README 的策略表写的就是「legacy: <=480p / WMV / AVI」，与分辨率无关）。
    # 注意：当前 course_720 与 legacy 的参数完全相同，所以这条顺序只影响语义
    # 归类与日志，不改变实际处理结果；但顺序写反会误导排查。
    if info.container in legacy_containers:
        return PROFILES["legacy"]
    if height and height >= 700:
        return PROFILES["course_720"]
    if height and height <= 480:
        return PROFILES["legacy"]
    return PROFILES["course_720"]
