"""音频修复适配器（需求 #6 / #7）。

支持后端：
- deepfilternet  （默认，CPU 运行，适合长视频批量降噪）
- clearervoice   （预留扩展接口，默认不启用）
- none           （跳过音频修复）

**已对齐 DeepFilterNet 0.5.6 的真实 CLI**（控制台脚本 `deepFilter`，
入口 `df.enhance:run`）。其行为要点：

    deepFilter <noisy_audio_files...> [-o/--output-dir DIR] [--pf]
               [-a/--atten-lim dB] [--no-suffix] [-m/--model-base-dir NAME|PATH]

- 输出文件写入 `--output-dir`，命名为 `<输入stem><模型名后缀>.wav`
  （后缀形如 `_DeepFilterNet3`）；`--no-suffix` 可去掉后缀。
- 模型首次运行时**自动从 GitHub 下载**到用户缓存目录，无需手工准备。
- 内部会按模型采样率(48k)处理后再重采样回输入的采样率。

由于流水线的输入是 FFmpeg 抽出的 WAV（48000Hz），输出仍是 48000Hz，
因此直接满足 config 中 `audio_repair.sample_rate: 48000` 的目标。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pipeline.errors import DependencyError
from pipeline.runner import run_command

from ._toolpath import which_tool

log = logging.getLogger("adapters.audio")

#: 默认命令模板（对齐 DeepFilterNet 0.5.6 的真实参数）。
DEFAULT_ARGS_TEMPLATE = ["{input}", "--output-dir", "{output_dir}"]


class DeepFilterNetAdapter:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}
        self.executable: str = self.cfg.get("executable", "deepFilter")
        self.args_template: list = self.cfg.get("args_template",
                                                list(DEFAULT_ARGS_TEMPLATE))
        self.extra_args: list = self.cfg.get("extra_args", [])
        self.timeout: float = float(self.cfg.get("timeout_seconds", 4 * 3600))

    def available(self) -> bool:
        return which_tool(self.executable) is not None

    def enhance(self, src_wav: Path, dst_wav: Path,
                log_file: Path | None = None) -> None:
        """对 WAV 降噪。DeepFilterNet 输出到目录，完成后重命名到目标路径。"""
        if not self.available():
            raise DependencyError(
                f"DeepFilterNet 未安装（executable={self.executable!r}）。"
                "请运行 scripts/setup_ai_tools.ps1 安装（pip install deepfilternet），"
                "或在 config.yaml 中配置正确路径。")
        if not Path(src_wav).is_file():
            raise DependencyError(f"音频 AI 输入不存在: {src_wav}")

        out_dir = Path(dst_wav).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        mapping = {"input": str(src_wav), "output_dir": str(out_dir),
                   "output": str(dst_wav)}
        args = [self.executable]
        args += [str(a).format(**mapping) for a in self.args_template]
        args += [str(a) for a in self.extra_args]
        log.info("DeepFilterNet 调用: %s", " ".join(args[1:]))
        run_command(args, timeout=self.timeout, log_file=log_file)

        if Path(dst_wav).exists():
            return
        # DeepFilterNet 默认输出 <out_dir>/<stem>_DeepFilterNet3.wav 之类，
        # 若目标文件未生成则在输出目录中按优先级查找并重命名。
        stem = Path(src_wav).stem
        src_name = Path(src_wav).name
        patterns = [f"{stem}*DeepFilterNet*.wav", f"{stem}*.wav", "*.wav"]
        for pat in patterns:
            candidates = [c for c in sorted(out_dir.glob(pat)) if c.name != src_name]
            if candidates:
                candidates[0].replace(dst_wav)
                log.info("DeepFilterNet 输出重命名: %s → %s",
                         candidates[0].name, Path(dst_wav).name)
                return
        raise DependencyError(
            "DeepFilterNet 运行结束但未在输出目录找到任何 WAV；"
            "请用 `deepFilter --help` 核对 args_template。")


class ClearerVoiceAdapter:
    """预留接口（需求 #7）：默认不启用，待后续按官方 CLI 补全。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    def available(self) -> bool:
        return False

    def enhance(self, src_wav: Path, dst_wav: Path,
                log_file: Path | None = None) -> None:
        raise DependencyError(
            "ClearerVoice-Studio 后端尚未实现，请将 audio_repair.backend "
            "设为 deepfilternet 或 none")


def build_audio_adapter(cfg: dict):
    """按配置创建音频修复后端。"""
    backend = cfg.get("backend", "deepfilternet")
    if backend == "deepfilternet":
        return DeepFilterNetAdapter(cfg)
    if backend == "clearervoice":
        return ClearerVoiceAdapter(cfg)
    if backend == "none":
        return None
    raise DependencyError(f"未知音频后端: {backend}")
