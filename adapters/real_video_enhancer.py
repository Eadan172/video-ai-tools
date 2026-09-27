"""REAL-Video-Enhancer 视频 AI 修复适配器（需求 #5）。

已对齐 **REAL-Video-Enhancer 2.4.1** 的真实后端 CLI（`backend/rve-backend.py`）。

关键设计说明
------------
1. RVE 的 Windows 发行版是 **PyQt GUI**，无法无人值守调用；但其推理后端
   `rve-backend.py` 是**标准 argparse CLI**，本适配器直接驱动该 CLI，
   从而完全绕开 GUI（这是本方案能落地的核心）。
2. `executable` 通常填 RVE 专用解释器（如 `.venv-rve/Scripts/python.exe`），
   `args_template` 第一项填 `rve-backend.py` 的绝对路径。
3. RVE 的“放大倍率”由**模型文件**决定，而非 `--scale` 数值；因此
   `{upscale_model}` 由 config 中 `models.upscale["<scale>"]` 映射而来。
4. 模板项可以是**字符串**或**字符串列表**：列表表示一个「可选参数组」，
   当组内任一占位符解析为空字符串时，**整组被丢弃**——用于“未配置 4x 模型
   则不传 `--upscale_model`”这类场景，避免产生非法参数。
5. 工具不存在时流水线仍可启动：`doctor` 给 WARN，调度器抛 DependencyError。
6. 默认不插帧；RTX 4060 为唯一 AI 视频 GPU，并发 = 1（由 scheduler 保证）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pipeline.cleanup import unlink_with_retry
from pipeline.errors import DependencyError, ResourceBusyError
from pipeline.runner import run_command

from ._toolpath import which_tool

log = logging.getLogger("adapters.rve")

#: 默认命令模板（对齐 RVE 2.4.1 rve-backend.py 的真实参数名）。
#: 已被 `args_template` 覆盖时以 config 为准。
DEFAULT_ARGS_TEMPLATE: list = [
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
]

#: args_template 中允许出现的占位符（便于配置校验/报错提示）
KNOWN_PLACEHOLDERS = {
    "input", "output", "scale", "upscale_model", "decompress_model",
    "device", "infer_backend", "precision", "gpu_index", "tile",
    "ffmpeg_path", "crf", "encoder",
}


def _which(exe: str) -> str | None:
    """兼容旧调用点的别名，实际逻辑见 adapters._toolpath。"""
    return which_tool(exe)


class RealVideoEnhancerAdapter:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}
        self.executable: str = self.cfg.get("executable", "realesrgan-video-enhancer")
        self.device: str = self.cfg.get("device", "cuda")
        # 注意：cfg["backend"] 是“适配器后端”（real-video-enhancer|none），
        # RVE 自身的推理后端另用 infer_backend，避免语义冲突。
        self.infer_backend: str = self.cfg.get("infer_backend", "pytorch")
        self.precision: str = self.cfg.get("precision", "auto")
        self.gpu_index: str = str(self.cfg.get("gpu_index", 0))
        self.tile: str = str(self.cfg.get("tile", 0))
        self.crf: str = str(self.cfg.get("crf", 16))
        self.encoder: str = self.cfg.get("encoder", "libx265")
        self.ffmpeg_path: str = self.cfg.get("ffmpeg_path", "ffmpeg")
        self.args_template: list = self.cfg.get("args_template",
                                                list(DEFAULT_ARGS_TEMPLATE))
        self.extra_args: list = self.cfg.get("extra_args", [])
        self.timeout: float = float(self.cfg.get("timeout_seconds", 12 * 3600))

        models = self.cfg.get("models", {}) or {}
        upscale = models.get("upscale", {}) or {}
        # 允许 key 写成 int 或 str（YAML 里 2x 可能被解析成数字）
        self.upscale_models: dict[str, str] = {
            str(k): str(v) for k, v in upscale.items() if v
        }
        self.decompress_model: str = str(models.get("decompress") or "")

    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        return _which(self.executable) is not None

    def resolve_upscale_model(self, scale: int) -> str:
        """把放大倍率解析为模型文件路径。scale<=1 表示不超分（返回空串）。"""
        if scale is None or int(scale) <= 1:
            return ""
        key = f"{int(scale)}x"
        path = self.upscale_models.get(key) or self.upscale_models.get(str(int(scale)))
        if not path:
            return ""
        if not Path(path).is_file():
            log.warning("配置的 %s 超分模型不存在，将跳过超分: %s", key, path)
            return ""
        return path

    # ------------------------------------------------------------------ #
    def _build_args(self, src: Path, dst: Path, scale: int) -> list[str]:
        mapping = {
            "input": str(src),
            "output": str(dst),
            "scale": str(scale),
            "upscale_model": self.resolve_upscale_model(scale),
            "decompress_model": self.decompress_model
            if (self.cfg.get("deblock", True) and self.decompress_model) else "",
            "device": self.device,
            "infer_backend": self.infer_backend,
            "precision": self.precision,
            "gpu_index": self.gpu_index,
            "tile": self.tile,
            "ffmpeg_path": self.ffmpeg_path,
            "crf": self.crf,
            "encoder": self.encoder,
        }
        args: list[str] = [self.executable]
        for item in self.args_template:
            if isinstance(item, (list, tuple)):
                # 可选参数组：任一占位符为空 → 整组跳过
                rendered = [str(a).format(**mapping) for a in item]
                if any(tok == "" for tok in rendered):
                    continue
                args += rendered
            else:
                args.append(str(item).format(**mapping))
        args += [str(a) for a in self.extra_args]
        return args

    # ------------------------------------------------------------------ #
    def enhance(self, src: Path, dst: Path, scale: int = 2,
                log_file: Path | None = None) -> None:
        """视频 AI 修复：去压缩伪影 + 降噪 +（可选）超分。"""
        if not self.available():
            raise DependencyError(
                f"REAL-Video-Enhancer 未安装（executable={self.executable!r}）。"
                "请运行 scripts/setup_ai_tools.ps1 安装，或先将 "
                "video_repair.enabled 设为 false。")
        if not Path(src).is_file():
            raise DependencyError(f"视频 AI 输入不存在: {src}")

        scale = max(1, int(scale))
        if scale > 1 and not self.resolve_upscale_model(scale):
            log.warning("未配置 %dx 超分模型，本次仅做压缩修复/降噪（不放大）", scale)

        args = self._build_args(Path(src), Path(dst), scale)
        log.info("RVE 调用: %s", " ".join(args[1:]))
        if dst.exists():
            # RVE 自身也校验 --overwrite，这里先清干净。
            #
            # 注意：上一次 RVE 崩溃/退出时，它的 ffmpeg 子进程可能还活着几秒
            # 并持有该文件；此时直接 unlink 会抛 WinError 32，而且残留文件会
            # 让**后续每一个任务**都在这一行连环失败（实测一次卡死 34 个任务）。
            # 故等待句柄释放后重试；仍失败则抛可重试错误，让调度器稍后再来。
            if not unlink_with_retry(dst, log):
                raise ResourceBusyError(
                    f"中间文件 {dst} 仍被其他进程占用，无法清理；"
                    "请确认没有遗留的 RVE / ffmpeg 进程后重试")
        run_command(args, timeout=self.timeout, log_file=log_file)
        if not Path(dst).exists():
            raise DependencyError(
                f"RVE 运行结束但未产出 {dst}；请检查其 --help 与模型路径配置")
