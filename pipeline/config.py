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
        # 修复档位：auto | light | standard | full（见下方 REPAIR_TIERS）
        #   auto     交给 planner 按片源特征 + 时间预算自动选（默认）
        #   light    仅转码，不做 AI
        #   standard 2x AI 超分 + 音质降噪
        #   full     1x 压缩伪影修复 + 2x 超分 + 音质降噪
        "tier": "auto",
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
        # ---- 自动档位：按片源特征 + 时间预算自动决定是否启用 1x 压缩修复 ----
        # 实测 1x 压缩修复模型（RealPLKSR）比 2x 超分模型慢约 9 倍，
        # 手工写死会在批量处理 30+ 长视频时做出跑几天的配置。
        # 详见 pipeline/planner.py 的说明与标定数据。
        "auto": {
            "enabled": True,
            #: 单次 run 允许的「画质 AI 总时长」预算，按队列长度分摊到每个文件
            "total_budget_hours": 12,
            #: 单帧成本标定（秒 / 百万像素 / 帧），换机器/换显卡可用
            #: scripts/verify_cuda.py --bench 复标定后覆盖
            "upscale_s_per_mp": 0.0465,
            "decompress_s_per_mp": 1.64,
            "job_overhead_seconds": 30,
            "timeout_safety_factor": 3.0,
            "timeout_floor_seconds": 14400,
            "artifact_codecs": ["h264", "avc", "wmv3", "vc1", "msmpeg4v3",
                                "mpeg4", "msmpeg4", "mpeg2video"],
            "artifact_containers": ["asf", "wmv", "avi"],
            "audio_realtime_factor": 10.0,
        },
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
        # 交付目标定为 Windows / Android / iOS 三端默认播放器的**交集**：
        #   H.264 High + yuv420p + avc1 三端均原生支持，不依赖任何额外解码器。
        #   改回 hevc 也能跑，但必须是 Main + hvc1；profile / tag / pix_fmt 由
        #   adapters/ffmpeg.py::compat_video_args() 自动配套，不需要手工同步。
        "video_codec": "h264",              # h264 | hevc | av1
        # 必须 4:2:0 8bit：4:4:4（yuv444p / HEVC Rext）移动端一律无法解码
        "pix_fmt": "yuv420p",
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
    # 进度监控（只读旁观者；见 pipeline/progress.py）
    "dashboard": {
        "port": 8765,
        "interval_seconds": 3,              # Web 看板刷新间隔
        "tui_interval_seconds": 5,          # 终端 TUI 刷新间隔
        "model_ttl_seconds": 60,            # 阶段耗时模型缓存，防百分比抖动
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
    # mp4 是默认交付格式，直接用 H.264：Win / Android / iOS 默认播放器通吃。
    # （原先写 hevc，实测在安卓平板上因 hev1 + 4:4:4 报"格式不支持"）
    "mp4":  {"video_codec": "h264", "audio_codec": "aac",  "ext": "mp4",
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

# --------------------------------------------------------------------------- #
# 三档修复模块
#
# 档位只决定「做不做 AI / 做到哪一步」，不改变容器与编码（那由 --format 决定）。
# 实测耗时以 68min / 712×400 片源为基准（本机 RTX 4060 Laptop）。
# --------------------------------------------------------------------------- #
REPAIR_TIERS: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "自动",
        "video_ai": None,            # None = 交给 planner 按片源与预算自动决定
        "audio_ai": True,
        "summary": "按片源特征与时间预算自动在「中档 / 完全修复」之间选择",
        "est_hours_per_68min": "取决于片源与队列",
        "scope": "由 planner 判定（片源编码/容器 + 时间预算）",
        "depth": "自动：中档或完全修复",
        "steps": "scan 后按队列长度分摊预算 → 逐任务决定是否启用 1x 压缩修复",
    },
    "light": {
        "label": "light（仅转码）",
        "video_ai": False,
        "audio_ai": False,
        "summary": "不做任何 AI 修复，只做容器/编码规范化",
        "est_hours_per_68min": "约 5 分钟",
        "scope": "全部输入文件；只处理容器与编码，不碰画面/声音内容",
        "depth": "表面：仅重新封装与编码规范化，画质音质与源一致",
        "steps": "FFmpeg 转码 → 目标编码 + AAC 48kHz（QSV→NVENC→CPU 降级）→ 合流 → 校验",
    },
    "standard": {
        "label": "中档（2x AI 超分 + 音质降噪）",
        "video_ai": True,            # True = 画质 AI 开启，但不做 1x 压缩修复
        "audio_ai": True,
        "summary": "分辨率重建 + 语音降噪，不做压缩伪影修复",
        "est_hours_per_68min": "约 1.5 小时",
        "scope": "视频画面（分辨率/锐度）+ 音轨（噪声）；不做压缩伪影修复",
        "depth": "深度：2x 分辨率重建 + 时域/频域降噪，不重建编码损失的细节",
        "steps": "REAL-Video-Enhancer 2x 超分（SPAN 模型，CUDA）→ FFmpeg 抽 48kHz WAV → "
                 "DeepFilterNet 语音降噪 → FFmpeg 转码 → 合流 → 校验",
    },
    "full": {
        "label": "完全修复（1x 压缩伪影修复 + 2x 超分 + 音质降噪）",
        "video_ai": True,
        "audio_ai": True,
        "summary": "压缩伪影修复 + 分辨率重建 + 语音降噪，质量最好但最慢",
        "est_hours_per_68min": "约 14.8 小时",
        "scope": "视频画面（压缩伪影 + 分辨率/锐度）+ 音轨（噪声），三者全开",
        "depth": "完整重建：先修 H.264/WMV 的块效应与振铃，再重建分辨率，最后降噪",
        "steps": "REAL-Video-Enhancer 1x 去压缩伪影（RealPLKSR）→ 2x 超分（SPAN，CUDA）→ "
                 "FFmpeg 抽 48kHz WAV → DeepFilterNet 语音降噪 → FFmpeg 转码 → 合流 → 校验",
    },
}


def apply_repair_tier(cfg: "Config", tier: str) -> str:
    """按用户指定的修复档位覆写配置，返回归一化后的档位名。

    - light    → 关闭视频 AI 与音频 AI（只转码）
    - standard → 开启两个 AI，但不启用 1x 压缩伪影修复（仅 2x 超分）
    - full     → 开启两个 AI，并启用 1x 压缩伪影修复
    - auto     → 不覆写，交由 pipeline/planner.py 自动决定
    """
    key = str(tier).strip().lower()
    if key in ("", "auto"):
        cfg.raw.setdefault("video_repair", {})["tier"] = "auto"
        return "auto"
    if key not in REPAIR_TIERS:
        raise ConfigError(
            f"未知修复档位 {tier!r}；可选：{', '.join(REPAIR_TIERS)}")
    spec = REPAIR_TIERS[key]
    vr = cfg.raw.setdefault("video_repair", {})
    ar = cfg.raw.setdefault("audio_repair", {})
    if spec["video_ai"] is None:
        vr["enabled"] = True
    else:
        vr["enabled"] = bool(spec["video_ai"])
    ar["enabled"] = bool(spec["audio_ai"])
    vr["tier"] = key
    return key


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
