"""CLI 入口（需求 #29）。

    python main.py scan      扫描 input/，建立任务队列
    python main.py run       无人值守运行（Ctrl+C 安全中断，重启续跑）
    python main.py status    队列与资源状态
    python main.py retry     将 FAILED_FINAL 任务重置为 RETRY_PENDING
    python main.py cleanup   手动清理 work/ 临时文件
    python main.py verify    校验 output/ 所有最终文件
    python main.py estimate  预估批量耗时与自动档位（只探测，不处理）
    python main.py tiers     列出可选修复档位（范围/深度/具体操作/耗时）
    python main.py doctor    环境依赖检查
    python main.py monitor   终端实时监控（原地重绘的进度表）
    python main.py dashboard 本地 Web 进度看板（浏览器打开，只读）

全局参数（写在子命令之前）：
    --config <文件>          指定配置文件
    --format <格式>          mp4(默认) / mkv / mov / webm / avi
    --tier <档位>            auto(默认) / light / standard / full
    --only <子目录>          只处理 input/ 下的指定子目录（可重复）
    --input <目录>           覆盖输入目录（estimate 可直接指向任意目录）
    --no-ai                  跳过两个 AI 修复阶段，只做转码导出
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
from pipeline import dashboard, monitor_tui  # noqa: E402
from pipeline.cleanup import Cleaner  # noqa: E402
from pipeline.config import (CONTAINER_PROFILES, REPAIR_TIERS,
                             apply_output_format, apply_repair_tier,
                             load_config)  # noqa: E402
from pipeline.database import Database  # noqa: E402
from pipeline.logger import setup_logging  # noqa: E402
from pipeline.planner import RepairPlanner  # noqa: E402
from pipeline.resources import ResourceMonitor  # noqa: E402
from pipeline.scanner import Scanner  # noqa: E402
from pipeline.scheduler import Scheduler  # noqa: E402
from pipeline.state_machine import JobStatus  # noqa: E402
from pipeline.verifier import Verifier  # noqa: E402
from profiles.selector import select_profile  # noqa: E402


# ---------------------------------------------------------------------- #
def _bootstrap(args) -> tuple:
    cfg = load_config(args.config)
    # CLI 覆盖：--format 决定容器与编码组合；--only 限定处理的输入子目录
    if getattr(args, "format", None):
        apply_output_format(cfg, args.format)
    if getattr(args, "only", None):
        cfg.raw.setdefault("input", {})["include"] = list(args.only)
    if getattr(args, "input", None):
        cfg.raw.setdefault("paths", {})["input"] = args.input
    if getattr(args, "tier", None):
        apply_repair_tier(cfg, args.tier)
    if getattr(args, "no_ai", False):
        # 兜底开关，优先级高于 --tier：两个 AI 阶段全关，只做转码导出
        cfg.raw.setdefault("video_repair", {})["enabled"] = False
        cfg.raw.setdefault("audio_repair", {})["enabled"] = False
        cfg.raw.setdefault("video_repair", {})["tier"] = "light"
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
        want = str(cfg.output.get("video_codec", "h264"))
        qsv = "READY" if f"{want}_qsv" in encoders else "NOT AVAILABLE"
    except Exception:  # noqa: BLE001
        qsv = "UNKNOWN (ffmpeg 不可用)"
    print(f"Intel QSV        : {qsv}")
    print(f"Output Format    : .{cfg.output_ext}  "
          f"({cfg.output.get('video_codec')} + {cfg.output.get('audio_codec')})"
          f"  pix_fmt={cfg.output.get('pix_fmt', 'yuv420p')}")
    tier = str(cfg.video_repair.get("tier") or "auto")
    print(f"Repair Tier      : {tier}  "
          f"({REPAIR_TIERS.get(tier, {}).get('summary', '')})")
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


def cmd_estimate(args) -> int:
    """批量预估：只探测、不处理，报告耗时与自动档位结论。

    规划 30+ 个长视频的批量之前先跑这个，避免一头扎进要跑几天的任务。
    """
    cfg = load_config(args.config)
    if getattr(args, "format", None):
        apply_output_format(cfg, args.format)
    if getattr(args, "only", None):
        cfg.raw.setdefault("input", {})["include"] = list(args.only)
    if getattr(args, "input", None):
        cfg.raw.setdefault("paths", {})["input"] = args.input
    if getattr(args, "tier", None):
        apply_repair_tier(cfg, args.tier)

    ffprobe = FFprobeAdapter()
    # discover_files() 不碰数据库，这里传 None 即可
    scanner = Scanner(cfg.paths["input"], cfg.raw["input"]["extensions"], None,
                      cfg.raw["input"].get("include"))
    files = scanner.discover_files()
    if not files:
        print("input/ 下没有找到待处理的视频文件")
        return 0

    infos = []
    skipped = 0
    for p in files:
        try:
            info = ffprobe.probe(str(p))
        except Exception as exc:  # noqa: BLE001 —— 探测失败仅跳过，不影响预估
            print(f"[跳过] {p.name}: {exc}")
            skipped += 1
            continue
        infos.append((info, select_profile(info, cfg.raw.get("profiles", {}))))
    if not infos:
        print("没有可预估的文件")
        return 0

    planner = RepairPlanner(cfg.video_repair, pending_jobs=len(infos))
    est = planner.batch_estimate(infos)
    auto = (cfg.video_repair.get("auto") or {})
    budget_total = float(auto.get("total_budget_hours", 12))

    in_dir = cfg.paths["input"]
    tier_now = str(cfg.video_repair.get("tier") or "auto")
    print("批量预估（只探测，不处理任何文件）")
    print("=" * 62)
    print(f"修复档位          : {tier_now}"
          f"（{REPAIR_TIERS.get(tier_now, {}).get('label', '')}）")
    print(f"文件数            : {est['files']}")
    print(f"总时长            : {est['total_duration_hours']:.1f} 小时")
    print(f"单文件时间预算    : {est['budget_per_job_hours']:.2f} 小时"
          f"（总预算 {budget_total:.1f}h ÷ {est['files']}）")
    print(f"画质 AI 完整档     : {est['est_full_hours']:.1f} 小时（1x+2x）")
    print(f"画质 AI 快速档     : {est['est_fast_hours']:.1f} 小时（仅 2x）")
    print(f"音质 AI（预估）    : {est['est_audio_hours']:.1f} 小时")
    print("-" * 62)

    chosen = est["est_chosen_hours"]
    n_decomp = sum(1 for r in est["rows"] if r["use_decompress"])
    if not planner.enabled:
        print("自动档位          : 已关闭（auto.enabled=false，按配置执行）")
    elif not planner.configured:
        print("自动档位          : 仅 2x —— 未配置 1x 压缩修复模型")
    else:
        print(f"自动档位          : {'1x+2x（完整修复）' if n_decomp else '2x（仅超分）'}"
              f"  {n_decomp}/{est['files']} 个文件启用压缩修复")
        if est["rows"]:
            print(f"  理由：{est['rows'][0]['reason']}")
    print(f"预计画质 AI 总耗时 : {chosen:.1f} 小时"
          f"（外加音质约 {est['est_audio_hours']:.1f}h 与转码/合流）")
    worst = max(est["rows"], key=lambda r: r["chosen_h"])
    if worst["chosen_h"] > est["budget_per_job_hours"]:
        print(f"  [注意] 最慢的单文件约 {worst['chosen_h']:.1f}h，已超单文件预算 "
              f"{est['budget_per_job_hours']:.2f}h。预算只用于决定是否启用压缩修复，"
              f"不会限速；如需更宽松可调大 video_repair.auto.total_budget_hours")
    if skipped:
        print(f"  [注意] {skipped} 个文件探测失败已跳过")
    print("-" * 62)
    print("单文件明细：")
    print(f"  {'文件':<34}{'时长':>8}{'分辨率':>11}{'编码':>8}{'档位':>8}"
          f"{'完整档':>9}{'快速档':>9}")
    for r in est["rows"]:
        name = str(r["name"]).replace(str(in_dir) + "\\", "").replace(
            str(in_dir) + "/", "")
        if len(name) > 32:
            name = "…" + name[-31:]
        print(f"  {name:<34}{r['duration_min']:>7.1f}m{r['resolution']:>11}"
              f"{r['codec']:>8}{'1x+2x' if r['use_decompress'] else '2x':>8}"
              f"{r['est_full_h']:>8.1f}h{r['est_fast_h']:>8.1f}h")
    print()
    print("提示：这里是估算，不是限额；实际耗时受分辨率、码率与后台负载影响。")
    print("      档位由 pipeline/planner.py 按实测标定自动决定，无需手改配置。")
    return 0


def cmd_tiers(args) -> int:
    """列出可选的修复档位：修复范围 / 修复深度 / 具体操作 / 实测耗时。"""
    print("修复档位一览 —— 用 --tier <名称> 选择，或双击 run.bat 弹出菜单")
    print("=" * 72)
    for i, (key, t) in enumerate(
            [(k, REPAIR_TIERS[k]) for k in ("light", "standard", "full")], 1):
        print(f"[{i}] {key:<9}{t['label']}")
        print(f"    修复范围：{t['scope']}")
        print(f"    修复深度：{t['depth']}")
        print(f"    具体操作：{t['steps']}")
        print(f"    实测耗时：{t['est_hours_per_68min']}（68 分钟 / 712×400 片源）")
        print()
    t = REPAIR_TIERS["auto"]
    print(f"[0] auto     {t['label']}（默认）")
    print(f"    修复范围：{t['scope']}")
    print(f"    修复深度：{t['depth']}")
    print(f"    具体操作：{t['steps']}")
    print("=" * 72)
    print("用法示例：")
    print("  python main.py --tier standard scan   # 再 python main.py --tier standard run")
    print("  python main.py --tier full --only 剧集 estimate    # 先看要跑多久")
    print("  run.bat                                           # 双击，弹菜单选档位")
    print()
    print("说明：档位只决定「做不做 AI、做到哪一步」；输出格式另由 --format 决定。")
    return 0


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
            want = str(cfg.output.get("video_codec", "h264"))
            check("NVIDIA NVENC", f"{want}_nvenc" in encoders)
            check("Intel QSV", f"{want}_qsv" in encoders)
            check("CPU 软编", f"lib{'x265' if want == 'hevc' else 'x264'}"
                  in encoders)
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
    parser.add_argument("--input", metavar="目录",
                        help="覆盖输入目录（默认取 config 的 paths.input）；"
                             "estimate 可直接指向任意目录")
    parser.add_argument("--tier", choices=sorted(REPAIR_TIERS),
                        help="修复档位：auto(默认，自动) / light(仅转码) / "
                             "standard(2x AI 超分+音质) / full(1x+2x+音质)")
    parser.add_argument("--no-ai", action="store_true",
                        help="跳过两个 AI 修复阶段，只做转码导出（等价 --tier light）")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("scan", "扫描 input/ 建立任务队列"),
        ("run", "无人值守运行"),
        ("status", "查看队列与资源状态"),
        ("retry", "重置失败任务"),
        ("cleanup", "清理临时文件"),
        ("verify", "校验 output/ 最终文件"),
        ("estimate", "预估批量耗时与自动档位（只探测，不处理）"),
        ("tiers", "列出可选修复档位（范围/深度/具体操作/耗时）"),
        ("doctor", "环境依赖检查"),
    ]:
        sub.add_parser(name, help=help_text)
    # 监控类子命令带自己的参数，单独注册（handler 经 set_defaults 注入）
    monitor_tui.add_cli(sub)
    dashboard.add_cli(sub)
    args = parser.parse_args()

    handlers = {
        "scan": cmd_scan, "run": cmd_run, "status": cmd_status,
        "retry": cmd_retry, "cleanup": cmd_cleanup,
        "verify": cmd_verify, "estimate": cmd_estimate, "tiers": cmd_tiers,
        "doctor": cmd_doctor,
    }
    own = getattr(args, "_handler", None)      # monitor / dashboard 自带 handler
    if own is not None:
        return own(args)
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
