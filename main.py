"""CLI 入口（需求 #29）。

    python main.py scan      扫描 input/，建立任务队列
    python main.py run       无人值守运行（Ctrl+C 安全中断，重启续跑）
    python main.py status    队列与资源状态
    python main.py retry     将 FAILED_FINAL 任务重置为 RETRY_PENDING
    python main.py cleanup   手动清理 work/ 临时文件
    python main.py verify    校验 output/ 所有最终文件
    python main.py doctor    环境依赖检查
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# 允许 `python main.py` 在项目根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters import FFmpegAdapter, FFprobeAdapter, RealVideoEnhancerAdapter  # noqa: E402
from adapters.deepfilternet import DeepFilterNetAdapter  # noqa: E402
from pipeline.cleanup import Cleaner  # noqa: E402
from pipeline.config import (CONTAINER_PROFILES, apply_output_format,
                             load_config)  # noqa: E402
from pipeline.database import Database  # noqa: E402
from pipeline.logger import setup_logging  # noqa: E402
from pipeline.resources import ResourceMonitor  # noqa: E402
from pipeline.scanner import Scanner  # noqa: E402
from pipeline.scheduler import Scheduler  # noqa: E402
from pipeline.state_machine import JobStatus  # noqa: E402
from pipeline.verifier import Verifier  # noqa: E402


# ---------------------------------------------------------------------- #
def _bootstrap(args) -> tuple:
    cfg = load_config(args.config)
    # CLI 覆盖：--format 决定容器与编码组合；--only 限定处理的输入子目录
    if getattr(args, "format", None):
        apply_output_format(cfg, args.format)
    if getattr(args, "only", None):
        cfg.raw.setdefault("input", {})["include"] = list(args.only)
    if getattr(args, "no_ai", False):
        # AI 组件缺失时的兜底：跳过两个 AI 阶段，只做转码导出，
        # 避免每个任务都在 REPAIR_VIDEO 直接 FAILED_FINAL
        cfg.raw.setdefault("video_repair", {})["enabled"] = False
        cfg.raw.setdefault("audio_repair", {})["enabled"] = False
    cfg.ensure_dirs()
    setup_logging(cfg.paths["logs"])
    db = Database(cfg.paths["database"])
    return cfg, db


def cmd_scan(args) -> int:
    cfg, db = _bootstrap(args)
    scanner = Scanner(cfg.paths["input"],
                      cfg.raw["input"]["extensions"], db,
                      cfg.raw["input"].get("include"))
    created = scanner.scan()
    counts = db.status_counts()
    print(f"扫描完成：新建任务 {len(created)} 个，队列总计 "
          f"{sum(counts.values())} 个")
    return 0


def cmd_run(args) -> int:
    cfg, db = _bootstrap(args)
    scheduler = Scheduler(cfg, db)

    def _sigint(_sig, _frame):
        print("\n收到中断信号：正在安全退出（状态已保存，可断点续跑）...")
        scheduler.request_stop()

    signal.signal(signal.SIGINT, _sigint)
    scheduler.run()
    return 0


def cmd_status(args) -> int:
    cfg, db = _bootstrap(args)
    counts = db.status_counts()
    res = ResourceMonitor(cfg.resources, cfg.paths["work"])
    snap = res.snapshot()
    ffmpeg = FFmpegAdapter(encoder_cfg=cfg.raw.get("encoder", {}))

    print("Video Pipeline")
    print("===============================")
    total = sum(counts.values())
    print(f"Total Jobs       : {total}")
    for label, keys in [
        ("Done", [JobStatus.DONE]),
        ("Running", [JobStatus.RUNNING]),
        ("Waiting", [JobStatus.WAITING, JobStatus.DISCOVERED]),
        ("Retry Pending", [JobStatus.RETRY_PENDING]),
        ("Failed Final", [JobStatus.FAILED_FINAL]),
        ("Wait Disk", [JobStatus.WAIT_DISK]),
    ]:
        print(f"{label:<17}: {sum(counts.get(k.value, 0) for k in keys)}")
    print()
    print(f"Disk Free        : {snap.disk_free_gb:.1f} GB")
    print(f"RAM Usage        : {snap.ram_percent:.0f}%")
    if snap.gpu_vram_total_mb:
        print(f"RTX VRAM         : {snap.gpu_vram_used_mb / 1024:.1f} / "
              f"{snap.gpu_vram_total_mb / 1024:.0f} GB")
    else:
        print("RTX VRAM         : 未检测到（无 NVIDIA GPU 或 nvidia-smi）")
    try:
        encoders = ffmpeg.list_encoders()
        qsv = "READY" if "hevc_qsv" in encoders else "NOT AVAILABLE"
    except Exception:  # noqa: BLE001
        qsv = "UNKNOWN (ffmpeg 不可用)"
    print(f"Intel QSV        : {qsv}")
    print(f"Output Format    : .{cfg.output_ext}  "
          f"({cfg.output.get('video_codec')} + {cfg.output.get('audio_codec')})")
    if cfg.raw.get("input", {}).get("include"):
        print(f"Input Scope      : {', '.join(cfg.raw['input']['include'])}")

    running = db.list_jobs([JobStatus.RUNNING])
    if running:
        job = running[0]
        print()
        print(f"Current Job      : {Path(job.source_path).name}")
        print(f"Stage            : {job.stage.value}")
        print(f"GPU              : {cfg.raw['gpu']['ai_gpu']['type']}")
    return 0


def cmd_retry(args) -> int:
    cfg, db = _bootstrap(args)
    failed = db.list_jobs([JobStatus.FAILED_FINAL])
    if not failed:
        print("没有 FAILED_FINAL 任务")
        return 0
    for job in failed:
        db._conn.execute(  # 重置重试计数，给用户手动 retry 一个全新机会
            "UPDATE jobs SET retry_count=0 WHERE job_id=?", (job.job_id,))
        db._conn.commit()
        db.update_status(job.job_id, JobStatus.RETRY_PENDING,
                         error="手动重试")
        print(f"job {job.job_id} ({Path(job.source_path).name}) → RETRY_PENDING")
    return 0


def cmd_cleanup(args) -> int:
    cfg, db = _bootstrap(args)
    cleaner = Cleaner(cfg.paths["work"], cfg.paths["input"],
                      cfg.paths["output"])
    freed = cleaner.emergency_cleanup()
    cleaner.cleanup_current()
    print(f"清理完成，释放 {freed / 1024 ** 3:.2f} GB"
          f"（input/ 与 output/ 未被触碰）")
    return 0


def cmd_verify(args) -> int:
    cfg, db = _bootstrap(args)
    verifier = Verifier(FFprobeAdapter(), cfg.verify, cfg.output)
    out_dir = cfg.paths["output"]
    # 输出目录会镜像 input 的子目录结构 → 递归查找；且只校验媒体文件，
    # 避免把 .gitkeep 之类的占位文件当作待校验对象误报失败。
    exts = {e.lower() for e in cfg.raw["input"]["extensions"]}
    files = [p for p in sorted(out_dir.rglob("*"))
             if p.is_file() and p.suffix.lower() in exts]
    ok = bad = 0
    for p in files:
        try:
            from pipeline.state_machine import MediaInfo
            verifier.verify_output(str(p), MediaInfo(path=str(p)))
            print(f"[OK]   {p.relative_to(out_dir)}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {p.relative_to(out_dir)}: {exc}")
            bad += 1
    print(f"\n校验完成: {ok} 通过, {bad} 失败")
    return 1 if bad else 0


def cmd_doctor(args) -> int:
    """环境依赖检查（需求 #29 / #33）。缺依赖不崩溃，输出清晰报告。"""
    cfg = load_config(args.config)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, ok, note))

    check("Python >= 3.11", sys.version_info >= (3, 11),
          sys.version.split()[0])

    ffmpeg = FFmpegAdapter(encoder_cfg=cfg.raw.get("encoder", {}))
    ffprobe = FFprobeAdapter()
    check("FFmpeg", ffmpeg.available(), shutil.which("ffmpeg") or "")
    check("FFprobe", ffprobe.available(), shutil.which("ffprobe") or "")

    rve = RealVideoEnhancerAdapter(cfg.video_repair)
    dfn = DeepFilterNetAdapter(cfg.audio_repair)
    check("REAL-Video-Enhancer", rve.available(), rve.executable)
    check("DeepFilterNet", dfn.available(), dfn.executable)

    if ffmpeg.available():
        try:
            encoders = ffmpeg.list_encoders()
            check("NVIDIA NVENC", "hevc_nvenc" in encoders)
            check("Intel QSV", "hevc_qsv" in encoders)
            check("CPU libx265", "libx265" in encoders)
        except Exception as exc:  # noqa: BLE001
            check("编码器检测", False, str(exc))

    res = ResourceMonitor(cfg.resources, Path("."))
    snap = res.snapshot()
    vram_ok = snap.gpu_vram_total_mb is not None and snap.gpu_vram_total_mb >= 7000
    check("NVIDIA GPU (>=8GB VRAM)", vram_ok,
          f"{snap.gpu_vram_total_mb / 1024:.0f} GB"
          if snap.gpu_vram_total_mb else "未检测到")

    # ---- CUDA 版 torch 与 video_repair.device 是否匹配（最易踩的错配） ---- #
    if rve.available():
        want_cuda = str(cfg.video_repair.get("device", "")).lower() == "cuda"
        try:
            proc = subprocess.run(
                [rve.executable, "-c",
                 "import torch;print(torch.__version__);"
                 "print(torch.version.cuda);print(torch.cuda.is_available())"],
                capture_output=True, text=True, timeout=300)
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            ver = lines[0] if lines else "?"
            cuda_ver = lines[1] if len(lines) > 1 else "None"
            avail = len(lines) > 2 and lines[2] == "True"
            note = f"torch {ver}"
            note += f", CUDA {cuda_ver}" if cuda_ver and cuda_ver != "None" else "（CPU 版）"
            if want_cuda:
                check("CUDA 版 torch（device=cuda）", avail,
                      note if avail else
                      note + " ← 需装 CUDA 版: setup_ai_tools.ps1 -Cuda")
            else:
                check("PyTorch 运行自检（device=cpu）", True, note)
        except Exception as exc:  # noqa: BLE001
            check("PyTorch 运行自检", False, str(exc)[:120])

    check(f"RAM >= {cfg.resources.get('ram_total_gb', 16)}GB",
          snap.ram_free_gb + snap.ram_percent >= 0,  # RAM 存在即可
          f"空闲 {snap.ram_free_gb:.1f} GB")
    free_gb = shutil.disk_usage(Path(".").resolve()).free / 1024 ** 3
    check("磁盘可用空间", free_gb >= cfg.disk.get("emergency_stop_gb", 10),
          f"{free_gb:.1f} GB")

    missing: list[str] = []
    for name, ok, note in results:
        tag = "[OK]  " if ok else "[WARN]"
        suffix = f"  ({note})" if note else ""
        print(f"{tag} {name}{suffix}")
        if not ok:
            missing.append(name)

    if missing:
        print("\n缺少依赖/检查未通过：")
        for i, name in enumerate(missing, 1):
            print(f"  {i}. {name}")
        print("\n配置路径：config.yaml（可调整 executable 路径或禁用对应功能）")
        print("提示：缺少 AI 工具时流水线仍可运行——将 video_repair.enabled "
              "/ audio_repair.enabled 设为 false 即可退化为纯转码模式。")
        return 1
    print("\n所有依赖就绪")
    return 0


# ---------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="video-pipeline",
        description="单机无人值守批量视频处理系统（修复 → 转格式 → 导出）")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("--format", choices=sorted(CONTAINER_PROFILES),
                        help="输出文件格式（默认 mp4；容器决定编码组合）")
    parser.add_argument("--only", action="append", metavar="子目录",
                        help="只处理 input/ 下的指定子目录（可重复，如 --only 剧集）")
    parser.add_argument("--no-ai", action="store_true",
                        help="跳过两个 AI 修复阶段，只做转码导出（AI 组件缺失时的兜底）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("scan", "扫描 input/ 建立任务队列"),
        ("run", "无人值守运行"),
        ("status", "查看队列与资源状态"),
        ("retry", "重置失败任务"),
        ("cleanup", "清理临时文件"),
        ("verify", "校验 output/ 最终文件"),
        ("doctor", "环境依赖检查"),
    ]:
        sub.add_parser(name, help=help_text)
    args = parser.parse_args()

    handlers = {
        "scan": cmd_scan, "run": cmd_run, "status": cmd_status,
        "retry": cmd_retry, "cleanup": cmd_cleanup,
        "verify": cmd_verify, "doctor": cmd_doctor,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
