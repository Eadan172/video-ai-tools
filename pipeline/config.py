"""配置加载与校验。

使用 dataclass 承载配置，避免魔法字符串散落各处。
config.yaml 中所有键都有合理默认值，缺失文件时抛出 ConfigError。

路径约定
--------
- `paths` 段下的相对路径 → 相对于 **config.yaml 所在目录**（`Config.root`）。
- `video_repair` / `audio_repair` 中的 AI 工具路径（executable、args_template
  里的脚本、模型文件、ffmpeg_path） → 同样以 `./` 或 `../` 开头的写法
  会被解析到 `Config.root`，从而避免在配置里硬编码绝对路径。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError


# --------------------------------------------------------------------------- #
# 默认值 —— 对应需求文档中的基准硬件：22 核 / 16GB RAM / 50GB 磁盘
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, Any] = {
    "paths": {
        "input": "./input",
        "work": "./work",
        "output": "./output",
        "failed": "./failed",
        "logs": "./logs",
        "database": "./pipeline.db",
    },
    "resources": {
        "cpu_cores": 22,
        "ram_total_gb": 16,
        "ram_soft_limit_percent": 80,
        "ram_hard_limit_percent": 92,
        "max_video_ai_jobs": 1,
        "max_audio_jobs": 1,
        "max_transcode_jobs": 1,
        "monitor_interval_seconds": 15,
    },
    "scheduler": {
        "max_active_video_workspaces": 1,
        "poll_interval_seconds": 10,
    },
    "disk": {
        "total_available_gb": 50,
        "safe_start_gb": 30,
        "pause_new_jobs_gb": 20,
        "cleanup_gb": 15,
        "emergency_stop_gb": 10,
        "safety_margin_gb": 8,
        "video_temp_multiplier": 1.5,
        "audio_temp_multiplier": 0.2,
        "output_multiplier": 1.2,
    },
    "gpu": {
        "ai_gpu": {"type": "nvidia", "index": 0},
        "encode_gpu": {"type": "intel", "index": 1},
    },
    "video_repair": {
        "enabled": True,
        "backend": "real-video-enhancer",   # real-video-enhancer | none
        "infer_backend": "pytorch",         # RVE 推理后端: pytorch | ncnn | tensorrt
        "device": "cuda",
        "precision": "auto",                # auto | float16 | float32
        "gpu_index": 0,
        "model": "auto",
        "denoise": True,
        "deblock": True,
        "upscale": True,
        "scale_mode": "auto",               # auto | 1x | 2x | 4x
        "interpolation": False,
        "tile": 0,                          # 0 = 不分块（显存不足时设为 256/512）
        "crf": 16,                          # RVE 自身编码的 CRF
        "encoder": "libx265",               # RVE 输出编码器预设
        "ffmpeg_path": "ffmpeg",            # RVE 需要的 ffmpeg
        "timeout_seconds": 43200,
        "executable": "realesrgan-video-enhancer",  # 安装后指向 RVE 专用解释器
        "args_template": [
            "-i", "{input}",
            "-o", "{output}",
            ["--extra_restoration_models", "{decompress_model}"],
            ["--upscale_model", "{upscale_model}"],
            "--device", "{device}",
            "--backend", "{infer_backend}",
            "--precision", "{precision}",
            "--pytorch_gpu_id", "{gpu_index}",
            ["--tilesize", "{tile}"],
            ["--ffmpeg_path", "{ffmpeg_path}"],
            "--overwrite",
            "--crf", "{crf}",
            "--video_encoder_preset", "{encoder}",
            "--audio_encoder_preset", "copy_audio",
        ],
        # 模型文件（REAL-Video-Enhancer 的“倍率”由模型决定，而非 --scale）
        "models": {
            "upscale": {
                # "2x": "./tools/models/2x_OpenProteus_Compact_i2_70K.pth",
                # "4x": "./tools/models/4xNomos8k_span_otf_medium.pth",
            },
            "decompress": None,
        },
        "extra_args": [],
    },
    "audio_repair": {
        "enabled": True,
        "backend": "deepfilternet",         # deepfilternet | clearervoice | none
        "executable": "deepFilter",
        "sample_rate": 48000,
        "bitrate": "320k",
        "timeout_seconds": 14400,
        "args_template": ["{input}", "--output-dir", "{output_dir}"],
        "extra_args": [],
    },
    "output": {
        "container": "mp4",
        "video_codec": "hevc",              # h264 | hevc | av1
        "audio_codec": "aac",
        "audio_bitrate": "320k",
        "sample_rate": 48000,
        "keep_subtitles": True,
        "keep_metadata": True,
    },
    "encoder": {
        "prefer": ["qsv", "nvenc", "cpu"],
        "qsv_preset": "medium",
        "cpu_preset": "medium",
        "crf": 23,
    },
    "retry": {
        "max_attempts": 2,
        "backoff_seconds": 30,
    },
    "verify": {
        "duration_tolerance_seconds": 2.0,
        "require_video": True,
        "require_audio": True,
        "min_size_bytes": 10_000,
    },
    "intermediate": {
        "lossless": False,                  # 默认禁止 FFV1+PCM 巨型中间文件
    },
    "input": {
        "extensions": [".mp4", ".avi", ".wmv", ".mkv", ".mov",
                       ".flv", ".webm", ".mpg", ".mpeg", ".3gp"],
        # 只处理指定的输入子目录（相对 input/ 的路径）；空 = 全部处理。
        # 例: ["剧集"] → 只处理 input/剧集/**。可用 CLI 的 --only 覆盖。
        "include": [],
    },
    "profiles": {
        "auto_select": True,
        "overrides": {},                    # 例: {"lesson_001.wmv": "legacy"}
    },
}

# --------------------------------------------------------------------------- #
# 输出容器 → 编码组合
#
# 容器与编码必须兼容（例如 webm 不接受 aac，avi 放 hevc 兼容性差），
# 因此这里让**容器决定编码组合**，而不是让两者独立配置后互相打架。
# `--format` / `output.container` 都会经过 apply_output_format() 归一化。
# --------------------------------------------------------------------------- #
CONTAINER_PROFILES: dict[str, dict[str, str]] = {
    "mp4":  {"video_codec": "hevc", "audio_codec": "aac",  "ext": "mp4",
             "audio_bitrate": "320k"},
    "mkv":  {"video_codec": "hevc", "audio_codec": "aac",  "ext": "mkv",
             "audio_bitrate": "320k"},
    "mov":  {"video_codec": "hevc", "audio_codec": "aac",  "ext": "mov",
             "audio_bitrate": "320k"},
    # opus（libopus）比特率上限为 256k，写 320k 会被 ffmpeg 直接拒绝
    "webm": {"video_codec": "vp9",  "audio_codec": "opus", "ext": "webm",
             "audio_bitrate": "256k"},
    "avi":  {"video_codec": "h264", "audio_codec": "mp3",  "ext": "avi",
             "audio_bitrate": "320k"},
}

#: 需要 `-movflags +faststart` 的容器（其余容器传该参数会报错）
FASTSTART_CONTAINERS = {"mp4", "mov"}


def apply_output_format(cfg: "Config", fmt: str) -> str:
    """按目标容器设置编码组合，返回归一化后的容器名。

    容器名同时也决定输出扩展名，保证 `output/xxx.<ext>` 与实际封装一致。
    """
    key = str(fmt).strip().lower().lstrip(".")
    if key not in CONTAINER_PROFILES:
        raise ConfigError(
            f"不支持的输出格式 {fmt!r}；可选：{', '.join(CONTAINER_PROFILES)}")
    prof = CONTAINER_PROFILES[key]
    out = cfg.raw.setdefault("output", {})
    out["container"] = key
    out["video_codec"] = prof["video_codec"]
    out["audio_codec"] = prof["audio_codec"]
    out["audio_bitrate"] = prof["audio_bitrate"]
    return key


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 dict，override 优先。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _is_relative_path_token(s: str) -> bool:
    """判断字符串是否是“以 ./ 或 ../ 开头的相对路径写法”。

    只认显式写法，避免把 `-i`、`libx265` 这类普通参数误判成路径。
    """
    return (s.startswith("./") or s.startswith("../")
            or s.startswith(".\\") or s.startswith("..\\"))


def _resolve_token(s: Any, root: Path) -> Any:
    if isinstance(s, str) and _is_relative_path_token(s):
        return str((root / s).resolve())
    if isinstance(s, (list, tuple)):
        return [_resolve_token(x, root) for x in s]
    return s


@dataclass
class Config:
    """强类型配置对象。raw 保留完整 dict 供 adapter 读取 extra_args。"""

    raw: dict[str, Any]
    root: Path = field(default_factory=Path.cwd)

    # ---- 常用快捷属性 ---------------------------------------------------- #
    def _section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def paths(self) -> dict[str, Path]:
        return {k: (self.root / v).resolve() for k, v in self._section("paths").items()}

    @property
    def disk(self) -> dict[str, Any]:
        return self._section("disk")

    @property
    def resources(self) -> dict[str, Any]:
        return self._section("resources")

    @property
    def scheduler_opts(self) -> dict[str, Any]:
        return self._section("scheduler")

    @property
    def video_repair(self) -> dict[str, Any]:
        return self._section("video_repair")

    @property
    def audio_repair(self) -> dict[str, Any]:
        return self._section("audio_repair")

    @property
    def output(self) -> dict[str, Any]:
        return self._section("output")

    @property
    def retry(self) -> dict[str, Any]:
        return self._section("retry")

    @property
    def verify(self) -> dict[str, Any]:
        return self._section("verify")

    @property
    def input_opts(self) -> dict[str, Any]:
        return self._section("input")

    @property
    def output_ext(self) -> str:
        """输出容器对应的扩展名（不含点）。"""
        key = str(self.output.get("container", "mp4")).lower()
        return CONTAINER_PROFILES.get(key, {}).get("ext", key)

    def ensure_dirs(self) -> None:
        """创建 input/work/output/failed/logs 目录（不会删除已有内容）。"""
        for key in ("input", "work", "output", "failed", "logs"):
            self.paths[key].mkdir(parents=True, exist_ok=True)

    # ---- AI 工具路径解析 -------------------------------------------------- #
    def resolve_tools(self) -> None:
        """把 AI 工具相关的相对路径（./ 或 ../ 开头）解析为绝对路径。

        就地修改 self.raw，使 adapter 无需感知项目根目录。
        """
        root = self.root
        for section, keys in (
            ("video_repair", ("executable", "ffmpeg_path", "args_template", "extra_args")),
            ("audio_repair", ("executable", "args_template", "extra_args")),
        ):
            sec = self.raw.get(section)
            if not isinstance(sec, dict):
                continue
            for key in keys:
                if key in sec:
                    sec[key] = _resolve_token(sec[key], root)
        # 模型文件
        vsec = self.raw.get("video_repair")
        if isinstance(vsec, dict) and isinstance(vsec.get("models"), dict):
            models = vsec["models"]
            if isinstance(models.get("decompress"), str):
                models["decompress"] = _resolve_token(models["decompress"], root)
            up = models.get("upscale")
            if isinstance(up, dict):
                models["upscale"] = {
                    k: _resolve_token(v, root) for k, v in up.items()
                }


def load_config(path: str | Path = "config.yaml") -> Config:
    """加载 config.yaml 并与默认配置合并。"""
    p = Path(path)
    user: dict[str, Any] = {}
    if p.exists():
        try:
            user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"config.yaml 解析失败: {exc}") from exc
        if not isinstance(user, dict):
            raise ConfigError("config.yaml 顶层必须是 mapping")
    merged = _deep_merge(DEFAULT_CONFIG, user)
    cfg = Config(raw=merged, root=p.resolve().parent)
    cfg.resolve_tools()
    # 容器可能被 config.yaml 直接改过 → 归一化编码组合，避免产出不兼容的封装
    apply_output_format(cfg, cfg.output.get("container", "mp4"))
    return cfg
