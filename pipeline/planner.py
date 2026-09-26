"""画质/音质 AI 修复的自动档位规划。

为什么要自动决定
----------------
同一台机器上，1x 压缩伪影修复模型（RealPLKSR）比 2x 超分模型（SPAN）
慢约 **9 倍**。以 68 分钟 / 712×400 / 25fps 的片源实测：

    1x + 2x（完整修复）  ≈ 14.8 小时
    仅 2x（只超分）      ≈  1.5 小时
    音质 DeepFilterNet   ≈  7 分钟（约 10× 实时）

因此「要不要开 1x 压缩修复」必须按 **片源特征 + 时间预算** 自动决定。
若在 config.yaml 里手工写死，批量处理 30+ 个长视频时极易写出一个要跑
好几天、无人值守也没法收尾的配置——这正是本模块要解决的问题。

成本模型
--------
按「单帧成本可分解」标定（本机 RTX 4060 Laptop 8GB + CUDA + tile=0）：

    秒/帧 = upscale_s_per_mp    × 输出百万像素     （2x 超分）
          + decompress_s_per_mp × 输入百万像素     （1x 压缩修复）

对三组实测值的吻合度（源 712×400 / 25fps，50 帧计时）：

    档位       实测      模型
    仅 2x      2.65s     2.65s
    1x + 2x   25.98s    25.98s
    仅 1x     23.25s    24.0s     ← 误差 3%

> 说明：分辨率维度的线性外推是**一阶近似**（只在 712×400 上标定过）。
> 换显卡或换分辨率档位后，可用 `scripts/verify_cuda.py --bench` 复标定，
> 直接改 config 里的 `video_repair.auto.*_s_per_mp`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("pipeline.planner")

#: 默认标定值（秒 / 百万像素 / 帧），可被 config 覆盖
DEFAULT_AUTO: dict = {
    "enabled": True,
    #: 单次 run 允许的「画质 AI 总时长」预算；决定是否启用昂贵的 1x 压缩修复
    "total_budget_hours": 12,
    "upscale_s_per_mp": 0.0465,
    "decompress_s_per_mp": 1.64,
    #: 每个任务固定开销（模型加载等），实测约 6~10s，取 30s 保守值
    "job_overhead_seconds": 30,
    #: 推导 RVE timeout 时的安全系数
    "timeout_safety_factor": 3.0,
    #: timeout 下限（估计失败时用这个兜底）
    "timeout_floor_seconds": 14400,
    #: 判定「片源含压缩伪影、值得修复」的编码
    "artifact_codecs": ["h264", "avc", "wmv3", "vc1", "msmpeg4v3",
                        "mpeg4", "msmpeg4", "mpeg2video"],
    #: 判定「老容器、压缩伪影概率高」的容器名
    "artifact_containers": ["asf", "wmv", "avi"],
    #: DeepFilterNet 相对实时的倍速（实测约 10×），用于音质阶段预估
    "audio_realtime_factor": 10.0,
}


def _megapixels(width: int, height: int) -> float:
    if not width or not height:
        return 0.0
    return (float(width) * float(height)) / 1_000_000.0


@dataclass
class RepairDecision:
    """单个任务在 REPAIR_VIDEO 阶段使用的档位决策。"""

    use_decompress: bool
    est_seconds: float          # 选定档位的预计耗时
    est_full_seconds: float     # 完整档位（1x+2x）的预计耗时
    est_fast_seconds: float     # 快速档位（仅 2x）的预计耗时
    budget_seconds: float       # 本任务可用的时间预算
    timeout_seconds: float      # 据此推导的 RVE timeout
    reason: str
    eligible: bool = False      # 片源是否「值得」做压缩修复
    warnings: list[str] = field(default_factory=list)

    @property
    def profile_name(self) -> str:
        return "1x+2x（完整修复）" if self.use_decompress else "2x（仅超分）"


class RepairPlanner:
    """按队列规模分配预算、按片源特征决定档位。"""

    def __init__(self, vr_cfg: dict, pending_jobs: int = 1,
                 logger: logging.Logger | None = None) -> None:
        self.log = logger or log
        auto = dict(DEFAULT_AUTO)
        auto.update((vr_cfg or {}).get("auto") or {})
        self.cfg = auto
        models = (vr_cfg or {}).get("models") or {}
        self.decompress_model: str = str(models.get("decompress") or "")
        up = models.get("upscale") or {}
        self.upscale_models = {str(k): str(v) for k, v in up.items() if v}

        self.enabled = bool(auto.get("enabled", True))
        #: 用户显式档位：auto | light | standard | full（由 config/--tier 写入）
        self.tier = str((vr_cfg or {}).get("tier") or "auto").lower()
        total_budget = float(auto.get("total_budget_hours", 12)) * 3600.0
        # 队列越大，单文件可用预算越小 → 长批次会自动退回快速档位
        self.budget_seconds = total_budget / max(1, int(pending_jobs))
        self.pending_jobs = max(1, int(pending_jobs))
        #: 「连快速档都超预算」的提示只报一次，避免 30 个文件刷 30 条相同告警
        self._warned_over_budget = False

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        """是否具备压缩修复能力（模型已配置且存在）。"""
        from pathlib import Path
        return bool(self.decompress_model) and Path(self.decompress_model).is_file()

    def eligible(self, info) -> bool:
        """片源是否值得做 1x 压缩伪影修复（按编码/容器判定）。"""
        codec = (info.video_codec or "").lower()
        container = (info.container or "").lower()
        if any(c in codec for c in self.cfg["artifact_codecs"]):
            return True
        return any(c in container for c in self.cfg["artifact_containers"])

    def est_seconds(self, frames: float, in_mp: float, out_mp: float,
                    use_decompress: bool) -> float:
        """按成本模型估算画质 AI 耗时（秒）。"""
        per_frame = self.cfg["upscale_s_per_mp"] * out_mp
        if use_decompress:
            per_frame += self.cfg["decompress_s_per_mp"] * in_mp
        return frames * per_frame + float(self.cfg["job_overhead_seconds"])

    # ------------------------------------------------------------------ #
    def decide(self, info, profile, quiet: bool = False) -> RepairDecision:
        """为单个任务决定档位与 timeout。"""
        frames = max(0.0, float(getattr(info, "duration", 0) or 0)) * \
            (float(getattr(info, "fps", 0) or 0) or 25.0)
        in_mp = _megapixels(int(getattr(info, "width", 0) or 0),
                            int(getattr(info, "height", 0) or 0))
        scale = max(1, int(getattr(profile, "upscale_scale", 1) or 1))
        out_mp = in_mp * scale * scale

        est_full = self.est_seconds(frames, in_mp, out_mp, True)
        est_fast = self.est_seconds(frames, in_mp, out_mp, False)
        budget = self.budget_seconds
        warn: list[str] = []

        if self.tier == "light":
            # 用户显式选 light：不做画质 AI（调用方已关闭 video_repair）
            use, reason = False, "档位=light：不做画质 AI，仅转码"
        elif self.tier == "standard":
            use, reason = False, "档位=中档：仅 2x 超分，不做压缩伪影修复"
        elif self.tier == "full":
            use = self.configured
            reason = ("档位=完全修复：启用 1x 压缩伪影修复 + 2x 超分"
                      if use else
                      "档位=完全修复，但未配置 1x 压缩修复模型 → 退化为仅 2x")
        elif not self.enabled:
            # 手工模式：完全尊重配置——模型填了就启用，填 null 就关闭
            use = self.configured
            reason = ("auto.enabled=false，按配置手工决定："
                      + ("启用 1x+2x" if use else "仅 2x"))
        elif not self.configured:
            use, reason = False, "未配置 1x 压缩修复模型，仅做 2x 超分"
        elif not self.eligible(info):
            use, reason = False, (f"片源编码 {info.video_codec or '?'} / 容器 "
                                  f"{info.container or '?'} 无压缩伪影特征，无需修复")
        elif est_full <= budget:
            use = True
            reason = (f"预算内：完整修复约 {est_full / 3600:.1f}h ≤ "
                      f"{budget / 3600:.1f}h，启用 1x+2x")
        else:
            use = False
            reason = (f"超预算：完整修复约 {est_full / 3600:.1f}h > "
                      f"{budget / 3600:.1f}h，退回 2x（约快 9 倍）")

        if frames <= 0:
            warn.append("缺少时长/帧率信息，耗时按 0 处理；timeout 用配置下限兜底")
        if est_fast > budget and not self._warned_over_budget and self.tier == "auto":
            self._warned_over_budget = True
            warn.append(
                f"即使只做 2x，单文件也约 {est_fast / 3600:.1f}h > 单文件预算 "
                f"{budget / 3600:.2f}h（本批共 {self.pending_jobs} 个文件）。"
                f"预算只用于决定是否启用压缩修复，不会限速；"
                f"如需消除该提示可调大 video_repair.auto.total_budget_hours")

        est = est_full if use else est_fast
        timeout = max(float(self.cfg["timeout_floor_seconds"]),
                      est * float(self.cfg["timeout_safety_factor"]) + 600.0)

        d = RepairDecision(use_decompress=use, est_seconds=est,
                           est_full_seconds=est_full, est_fast_seconds=est_fast,
                           budget_seconds=budget, timeout_seconds=timeout,
                           reason=reason, eligible=self.eligible(info),
                           warnings=warn)
        if not quiet:
            self.log.info("档位决策：%s —— %s（预计 %.2fh，timeout %.1fh）",
                          d.profile_name, reason, est / 3600, timeout / 3600)
            for w in warn:
                self.log.warning("档位决策提示：%s", w)
        return d

    # ------------------------------------------------------------------ #
    def batch_estimate(self, infos: list) -> dict:
        """给整批做预估（供 `main.py estimate` 使用，不修改任何状态）。"""
        tot_f = tot_dur = tot_full = tot_fast = 0.0
        rows = []
        for info, profile in infos:
            dur = max(0.0, float(getattr(info, "duration", 0) or 0))
            frames = dur * (float(getattr(info, "fps", 0) or 0) or 25.0)
            in_mp = _megapixels(int(getattr(info, "width", 0) or 0),
                                int(getattr(info, "height", 0) or 0))
            scale = max(1, int(getattr(profile, "upscale_scale", 1) or 1))
            out_mp = in_mp * scale * scale
            full = self.est_seconds(frames, in_mp, out_mp, True)
            fast = self.est_seconds(frames, in_mp, out_mp, False)
            d = self.decide(info, profile, quiet=True)
            tot_f += frames
            tot_dur += dur
            tot_full += full
            tot_fast += fast
            rows.append({
                "name": getattr(info, "path", "?"),
                "duration_min": dur / 60.0,
                "resolution": f"{getattr(info, 'width', 0)}x{getattr(info, 'height', 0)}",
                "codec": getattr(info, "video_codec", ""),
                "eligible": d.eligible,
                "use_decompress": d.use_decompress,
                "est_full_h": full / 3600.0,
                "est_fast_h": fast / 3600.0,
                "chosen_h": d.est_seconds / 3600.0,
                "reason": d.reason,
            })
        audio_h = (tot_dur / float(self.cfg.get("audio_realtime_factor", 10.0))
                   ) / 3600.0
        return {
            "files": len(rows),
            "total_frames": tot_f,
            "total_duration_hours": tot_dur / 3600.0,
            "est_full_hours": tot_full / 3600.0,
            "est_fast_hours": tot_fast / 3600.0,
            "est_chosen_hours": sum(r["chosen_h"] for r in rows),
            "est_audio_hours": audio_h,
            "budget_per_job_hours": self.budget_seconds / 3600.0,
            "rows": rows,
        }