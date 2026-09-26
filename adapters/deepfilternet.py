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
import shutil
from pathlib import Path

from pipeline.errors import DependencyError, ExternalToolError
from pipeline.runner import run_command

from ._toolpath import which_tool

log = logging.getLogger("adapters.audio")

#: 默认命令模板（对齐 DeepFilterNet 0.5.6 的真实参数）。
DEFAULT_ARGS_TEMPLATE = ["{input}", "--output-dir", "{output_dir}"]


def _write_upto(fh, block, budget: int) -> int:
    """往 SoundFile 写一段音频，但不超过 budget 帧（用于精确对齐源帧数）。

    返回实际写出的帧数；budget 用尽后返回 0，调用方继续消费剩余分段即可。
    """
    if budget <= 0:
        return 0
    if len(block) > budget:
        block = block[:budget]
    if len(block):
        fh.write(block)
    return len(block)


class DeepFilterNetAdapter:
    #: 单段最长秒数。越小越省内存，越大段边界越少（段边界靠交叉淡化消除接缝）
    DEFAULT_MAX_SEGMENT = 120.0
    #: 段间交叉淡化时长（秒）
    DEFAULT_CROSSFADE = 0.05
    #: DFN 峰值内存 ≈ 该系数 × float32 数据量。
    #: 实测标定（16GB 机器、空闲约 4GB 时）：
    #:   68min 立体声 48kHz 整文件 → 单次分配 3.15GB（数据量 1.57GB）
    #:   600s 段（数据量 230MB）→ 仍然 OOM
    #:   60s  段（数据量 23MB） → 通过
    #: 说明 DFN/torch 的峰值远高于数据量本身（分配器预留 + STFT 工作集），
    #: 故取 16 这个偏保守的系数；真正兜底靠下面的「OOM 自动对半降段重试」。
    #: 另注：内存紧张时 torch 也可能以 0xC0000005（访问冲突，rc=3221225477）
    #: 的形式崩溃，而非 0xC0000409——本机已在 60s 段上实测到该表现，
    #: 因此两者都视为「资源不足、可降段重试」。
    MEM_FACTOR = 16.0
    #: 允许动用的可用内存比例（留一半给系统与其他阶段）
    MEM_USE_RATIO = 0.5
    #: 单段最短秒数（降到这个值还 OOM 就只能报错）
    MIN_SEGMENT = 30.0

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}
        self.executable: str = self.cfg.get("executable", "deepFilter")
        self.args_template: list = self.cfg.get("args_template",
                                                list(DEFAULT_ARGS_TEMPLATE))
        self.extra_args: list = self.cfg.get("extra_args", [])
        self.timeout: float = float(self.cfg.get("timeout_seconds", 4 * 3600))
        self.max_segment: float = float(self.cfg.get("max_segment_seconds",
                                                     self.DEFAULT_MAX_SEGMENT))
        self.crossfade: float = float(self.cfg.get("crossfade_seconds",
                                                   self.DEFAULT_CROSSFADE))

    def available(self) -> bool:
        return which_tool(self.executable) is not None

    # ------------------------------------------------------------------ #
    def _build_args(self, src_wav: Path, out_dir: Path) -> list[str]:
        mapping = {"input": str(src_wav), "output_dir": str(out_dir),
                   "output": str(src_wav)}
        args = [self.executable]
        args += [str(a).format(**mapping) for a in self.args_template]
        args += [str(a) for a in self.extra_args]
        return args

    def _run_once(self, src_wav: Path, out_dir: Path,
                  log_file: Path | None) -> None:
        """对单个（短）WAV 跑一次 DeepFilterNet。"""
        args = self._build_args(src_wav, out_dir)
        log.info("DeepFilterNet 调用: %s", " ".join(args[1:]))
        run_command(args, timeout=self.timeout, log_file=log_file)

    @staticmethod
    def _find_output(out_dir: Path, stem: str) -> Path | None:
        """在输出目录里找 DFN 产物（<stem><模型后缀>.wav），排除输入自身。"""
        skip = {f"{stem}.wav", f"{stem}_DeepFilterNet3.wav"}
        for pat in (f"{stem}*DeepFilterNet*.wav", f"{stem}*.wav"):
            for c in sorted(out_dir.glob(pat)):
                if c.name != f"{stem}.wav":
                    return c
        # 退化：目录里任何新 wav（除输入）
        for c in sorted(out_dir.glob("*.wav")):
            if c.name not in skip and c.name != f"{stem}.wav":
                return c
        return None

    def _probe(self, path: Path):
        """读 WAV 基本参数；(frames, sr, channels, duration) 或 None。"""
        try:
            import soundfile as sf  # 懒加载，避免未装 AI 时拖垮整个 CLI
            info = sf.info(str(path))
            if not info.frames or not info.samplerate:
                return None
            return (int(info.frames), int(info.samplerate),
                    int(info.channels), info.frames / float(info.samplerate))
        except Exception as exc:  # noqa: BLE001
            log.warning("无法读取音频参数（将按单文件处理）: %s", exc)
            return None

    def _segment_seconds(self, sr: int, channels: int) -> float:
        """按当前可用内存算出单个音频段最长秒数。"""
        try:
            import psutil
            avail = float(psutil.virtual_memory().available)
        except Exception:  # noqa: BLE001
            return self.max_segment
        per_sec = max(1.0, float(sr) * float(channels) * 4.0 * self.MEM_FACTOR)
        fit = (avail * self.MEM_USE_RATIO) / per_sec
        return max(self.MIN_SEGMENT, min(self.max_segment, fit))

    @staticmethod
    def _is_oom(exc: Exception) -> bool:
        """判断异常是否属于内存不足。

        Windows 上内存不足有几种表现：
          * 子进程直接 abort → rc=3221226505（0xC0000409）
          * torch 在提交内存不足时崩溃 → rc=3221225477（0xC0000005）
          * stderr 里出现 "memory allocation of N bytes failed"
        """
        rc = getattr(exc, "returncode", None)
        if rc in (3221226505, -1073740791, 3221225477, -1073741819):
            return True
        text = (str(exc) + " " + str(getattr(exc, "stderr_tail", ""))).lower()
        return ("memory allocation" in text or "out of memory" in text
                or "cannot allocate memory" in text or "c0000409" in text)

    # ------------------------------------------------------------------ #
    def enhance(self, src_wav: Path, dst_wav: Path,
                log_file: Path | None = None) -> None:
        """对 WAV 降噪。

        长音频会被**自动分段**处理：DeepFilterNet 会把整个文件读成浮点数组，
        峰值内存约是数据量的 3 倍——68 分钟立体声 48kHz 需要 ~3.15GB，
        在 16GB 机器上必然 OOM。分段后每段只占几百 MB。

        分段仅在「按可用内存算出的单段上限 < 总时长」时启用；
        短音频仍走原来的一次性路径。
        """
        if not self.available():
            raise DependencyError(
                f"DeepFilterNet 未安装（executable={self.executable!r}）。"
                "请运行 scripts/setup_ai_tools.ps1 安装（pip install deepfilternet），"
                "或在 config.yaml 中配置正确路径。")
        if not Path(src_wav).is_file():
            raise DependencyError(f"音频 AI 输入不存在: {src_wav}")

        out_dir = Path(dst_wav).parent
        out_dir.mkdir(parents=True, exist_ok=True)

        probed = self._probe(Path(src_wav))
        if probed is None:
            self._enhance_single(Path(src_wav), Path(dst_wav), out_dir, log_file)
            return

        frames, sr, channels, duration = probed
        seg = self._segment_seconds(sr, channels)
        if duration <= seg:
            log.info("音频 %.1f 分钟 ≤ 单段上限 %.1f 分钟，一次性处理",
                     duration / 60.0, seg / 60.0)
            self._enhance_single(Path(src_wav), Path(dst_wav), out_dir, log_file)
            return

        need_mb = frames * channels * 4.0 * self.MEM_FACTOR / 1024 ** 2
        log.info("音频 %.1f 分钟超出单段上限 %.1f 分钟（整段约需 %.0f MB 内存）"
                 "→ 自动分段处理", duration / 60.0, seg / 60.0, need_mb)
        self._enhance_segmented(Path(src_wav), Path(dst_wav), out_dir,
                                frames, sr, channels, seg, log_file)

    # ------------------------------------------------------------------ #
    def _enhance_single(self, src_wav: Path, dst_wav: Path, out_dir: Path,
                        log_file: Path | None) -> None:
        """原有路径：整文件跑一次，然后把 DFN 产物重命名到目标名。"""
        self._run_once(src_wav, out_dir, log_file)
        if dst_wav.exists():
            return
        found = self._find_output(out_dir, src_wav.stem)
        if found is None:
            raise DependencyError(
                "DeepFilterNet 运行结束但未在输出目录找到任何 WAV；"
                "请用 `deepFilter --help` 核对 args_template。")
        found.replace(dst_wav)
        log.info("DeepFilterNet 输出重命名: %s → %s", found.name, dst_wav.name)

    def _enhance_segmented(self, src_wav: Path, dst_wav: Path, out_dir: Path,
                           frames: int, sr: int, channels: int, seg: float,
                           log_file: Path | None) -> None:
        """分段处理（带 OOM 自校准）。

        事先用成本模型猜单段时长，但 DFN 的真实峰值依赖 torch 分配器与当前
        系统提交内存，很难算准。因此：只要失败原因是内存不足，就把段长对半
        降低后整体重试，直到跑通或降到 MIN_SEGMENT 为止。
        这样在任何机器/任何后台负载下都能自适应，不需要用户调参。
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                self._try_segmented(src_wav, dst_wav, out_dir, frames, sr,
                                    channels, seg, log_file)
                return
            except ExternalToolError as exc:
                if not self._is_oom(exc) or seg <= self.MIN_SEGMENT:
                    raise
                new_seg = max(self.MIN_SEGMENT, seg / 2.0)
                log.warning("分段处理内存不足（单段 %.0f 秒，第 %d 次尝试）："
                            "%s → 降到 %.0f 秒后重试", seg, attempt,
                            str(exc)[:160], new_seg)
                seg = new_seg
                # 清理上一次的半成品，避免与重试结果混淆
                for junk in (dst_wav,):
                    try:
                        if junk.exists():
                            junk.unlink()
                    except OSError:
                        pass

    def _try_segmented(self, src_wav: Path, dst_wav: Path, out_dir: Path,
                           frames: int, sr: int, channels: int, seg: float,
                           log_file: Path | None) -> None:
        """分段处理再拼接：逐段跑 DFN，交叉淡化拼接后写回目标文件。"""
        import numpy as np
        import soundfile as sf

        tmp = out_dir / "_dfn_segments"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)

        total = frames / float(sr)
        parts: list[Path] = []
        start = 0.0
        idx = 0
        # 先写 .partial 再原子改名：中途失败不会留下一个"看起来像成品"的残文件
        partial = dst_wav.with_name(dst_wav.name + ".partial")
        try:
            with sf.SoundFile(str(src_wav)) as fh:
                while start < total - 1e-6:
                    idx += 1
                    n = int(min(seg, total - start) * sr)
                    if n <= 0:
                        break
                    fh.seek(int(start * sr))
                    block = fh.read(n, dtype="float32", always_2d=True)
                    seg_wav = tmp / f"seg{idx:03d}.wav"
                    sf.write(str(seg_wav), block, sr, subtype="PCM_16")
                    log.info("  段 %d/%d：%.2f~%.2f 分钟", idx,
                             int(total / seg) + 1, start / 60.0,
                             (start + n / float(sr)) / 60.0)
                    self._run_once(seg_wav, tmp, log_file)
                    found = self._find_output(tmp, seg_wav.stem)
                    if found is None:
                        raise DependencyError(
                            f"分段处理第 {idx} 段未产出 WAV，请检查 DFN 参数")
                    parts.append(found)
                    start += n / float(sr)

            # 流式拼接：逐段读入 → 与上一段尾部做交叉淡化 → 立即写出。
            # 内存峰值只有「单段 + 一个重叠窗口」，不随总时长增长。
            # 若像以前那样一次性 np.concatenate 整集，68min 立体声会占用
            # 4~5GB，可能在低内存机器上直接抛 MemoryError——那种失败发生在
            # 本进程内，OOM 自校准（只兜子进程的 ExternalToolError）碰不到。
            ov = max(1, int(self.crossfade * sr))
            written = 0
            pending: np.ndarray | None = None    # 上一段尚未写出的尾部
            with sf.SoundFile(str(partial), mode="w", format="WAV",
                              samplerate=sr, channels=channels,
                              subtype="PCM_16") as out_fh:
                for p in parts:
                    a, _ = sf.read(str(p), dtype="float32", always_2d=True)
                    if a.shape[1] != channels:      # 通道数兜底对齐
                        if a.shape[1] > channels:
                            a = a[:, :channels]
                        else:
                            a = np.tile(
                                a, (1, channels // max(1, a.shape[1]) + 1)
                            )[:, :channels]
                    if pending is None:              # 首段：先留出尾部做重叠
                        if len(a) > ov:
                            written += _write_upto(out_fh, a[:-ov],
                                                   frames - written)
                            pending = a[-ov:]
                        else:
                            pending = a
                        continue
                    k = int(min(ov, len(pending), len(a)))
                    if k > 0:
                        ramp = np.linspace(0.0, 1.0, k, dtype="float32")[:, None]
                        mixed = pending[-k:] * (1.0 - ramp) + a[:k] * ramp
                        written += _write_upto(out_fh, mixed, frames - written)
                    rest = a[k:]
                    if len(rest) > ov:
                        written += _write_upto(out_fh, rest[:-ov],
                                               frames - written)
                        pending = rest[-ov:]
                    else:
                        pending = rest

                if pending is not None and len(pending):
                    written += _write_upto(out_fh, pending, frames - written)

                # 长度对齐到原始帧数：DFN 的延时补偿可能让总帧数少于源，
                # 而校验器要求输出与源时长偏差 ≤ 2s
                if written < frames:
                    out_fh.write(np.zeros((frames - written, channels),
                                          dtype="float32"))
                    written = frames

            partial.replace(dst_wav)
            log.info("分段拼接完成 → %s（%d 段，%.2f 分钟）",
                     dst_wav.name, len(parts), written / float(sr) / 60.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if partial.exists():
                partial.unlink(missing_ok=True)


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
