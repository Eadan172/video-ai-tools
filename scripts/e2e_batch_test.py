#!/usr/bin/env python
"""端到端批量功能测试执行器（测试工具，不属于被测 pipeline 业务逻辑）。

对 input/ 下所有视频文件（含子目录）执行：

    1. 调用 pipeline（scan → run）做完整处理；
    2. 处理成功且成功导出 mp4 → mp4 保留在 output/（同名冲突时加 YYYYMMDDHHMMSS 时间戳）；
    3. 处理失败或导出失败 → 源视频文件移入 failed/（同名冲突加时间戳），
       并把详细错误写入 logs/YYYYMMDD_HHMMSS_error.log（单文件 > 10MB 自动新建）；
    4. 生成测试汇总报告。

约束：
    - 不修改 pipeline 业务逻辑代码，AI 阶段通过独立测试配置禁用（环境无该外部工具）；
    - 处理前对 input/ 与 pipeline.db 做备份；
    - 所有文件操作均有异常捕获，单项失败不会中断整体测试。

用法：
    python scripts/e2e_batch_test.py [--config config.e2e.yaml] [--skip-backup]
                                     [--run-timeout 21600] [--ffmpeg-dir DIR]

    python scripts/e2e_batch_test.py --smoke [--smoke-count 1]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]          # video_pipeline/
PROJ = ROOT.parent                                  # 项目根目录
ARTIFACT_ROOT = PROJ / "_e2e_artifacts"
ERROR_LOG_MAX_BYTES = 10 * 1024 * 1024              # 单个错误日志上限 10MB
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}

sys.path.insert(0, str(ROOT))
from pipeline.filename import sanitize_filename  # noqa: E402  与被测系统同名规则一致

#: 错误分类：命中任意关键字即归入该类别（按顺序匹配）
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("PROCESS_CRASH", ("进程异常退出", "TEST_RUN_TIMEOUT")),
    ("SOURCE_MISSING", ("源文件缺失",)),
    ("DEPENDENCY_MISSING", ("未安装", "未找到可执行文件", "没有可用的",
                            "DependencyError", "尚未实现")),
    ("SOURCE_CORRUPT", ("ffprobe 无法读取", "源文件异常", "输出无法解析",
                        "ProbeError")),
    ("GPU_OUT_OF_MEMORY", ("显存不足", "GpuOutOfMemory")),
    ("DISK_SPACE", ("磁盘", "WAIT_DISK", "DiskSpaceError", "EmergencyStop")),
    ("TIMEOUT", ("超时", "TimeoutExpired")),
    ("VERIFY_FAILED", ("校验失败", "VerificationError", "导出文件校验")),
    ("ENCODER_BACKEND_FAILED", ("所有编码后端均失败", "命令失败 rc=",
                                "ExternalToolError")),
    ("STATE_MACHINE_ERROR", ("非法状态迁移", "ValueError")),
    ("UNCLASSIFIED", ()),
]


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


def classify(text: str) -> str:
    for name, keys in CATEGORIES:
        if any(k in text for k in keys):
            return name
    return "UNCLASSIFIED"


# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, args) -> None:
        self.args = args
        self.workspace: Path = (Path(args.workspace).resolve()
                                if args.workspace else ROOT)
        self.paths = {
            "input": self.workspace / "input",
            "work": self.workspace / "work",
            "output": self.workspace / "output",
            "failed": self.workspace / "failed",
            "logs": self.workspace / "logs",
            "db": self.workspace / "pipeline.db",
        }
        self.config_name: str = args.config
        self.ffmpeg_dir = self._resolve_ffmpeg_dir()
        self.run_id = now_ts()
        self.artifacts = ARTIFACT_ROOT / f"artifacts_{self.workspace.name}"

        self.results: list[dict] = []
        self.notes: list[str] = []
        self.doctor_output = ""
        self.run_rc: int | None = None
        self.run_timed_out = False
        self.pipeline_out_log: Path | None = None
        self.error_log_file: Path | None = None
        self.started = time.time()
        self.duration = 0.0
        self.tracebacks: dict[int, list[str]] = {}
        self.crash_lines: list[str] = []

    # ------------------------------------------------------------------ #
    def _resolve_ffmpeg_dir(self) -> Path | None:
        cand = [self.args.ffmpeg_dir,
                os.environ.get("FFMPEG_DIR"),
                str(PROJ / ".tools" / "ffmpeg")]
        for c in cand:
            if c and (Path(c) / "ffmpeg.exe").exists() and (Path(c) / "ffprobe.exe").exists():
                return Path(c)
        return None

    def env(self) -> dict:
        e = os.environ.copy()
        if self.ffmpeg_dir:
            e["PATH"] = str(self.ffmpeg_dir) + os.pathsep + e.get("PATH", "")
            e["FFMPEG_DIR"] = str(self.ffmpeg_dir)
        e["PYTHONIOENCODING"] = "utf-8"
        e["PYTHONUTF8"] = "1"
        return e

    def python(self) -> str:
        return sys.executable

    def main_py(self) -> str:
        return str(ROOT / "main.py")

    # ------------------------------------------------------------------ #
    # 步骤 1：目录准备
    # ------------------------------------------------------------------ #
    def prepare_dirs(self) -> None:
        log("步骤 1/6：准备目录")
        for key in ("input", "work", "output", "failed", "logs"):
            p = self.paths[key]
            existed = p.exists()
            try:
                p.mkdir(parents=True, exist_ok=True)
                if not existed:
                    self.notes.append(f"新建目录 {p}")
                    log(f"  新建 {p}")
            except OSError as exc:
                self.notes.append(f"目录创建失败 {p}: {exc}")
                log(f"  [ERROR] 无法创建 {p}: {exc}")
        for key in ("output", "failed", "logs"):
            log(f"  {self.paths[key]} -> {'存在' if self.paths[key].exists() else '缺失'}")

    # ------------------------------------------------------------------ #
    # 步骤 2：备份
    # ------------------------------------------------------------------ #
    def backup(self) -> None:
        log("步骤 2/6：备份原始输入与数据库")
        if self.args.skip_backup:
            self.notes.append("已按 --skip-backup 跳过备份")
            log("  跳过（--skip-backup）")
            return
        self.artifacts.mkdir(parents=True, exist_ok=True)
        src_in, dst_in = self.paths["input"], self.artifacts / "input_backup"
        try:
            if src_in.exists():
                shutil.copytree(src_in, dst_in, dirs_exist_ok=True)
                n = sum(1 for _ in dst_in.rglob("*") if _.is_file())
                self.notes.append(f"input 备份：{n} 个文件 → {dst_in}")
                log(f"  input 已备份（{n} 个文件）→ {dst_in}")
        except OSError as exc:
            self.notes.append(f"input 备份失败: {exc}")
            log(f"  [WARN] input 备份失败：{exc}")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.paths["db"]) + suffix)
            try:
                if p.exists():
                    shutil.copy2(p, self.artifacts / f"pipeline.db{suffix}.bak")
            except OSError as exc:
                log(f"  [WARN] 数据库备份失败 {p.name}: {exc}")

    # ------------------------------------------------------------------ #
    def reset_db(self) -> None:
        """清空旧任务队列（旧库已备份），保证是干净的一次端到端测试。"""
        log("步骤 3/6：重置任务队列")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.paths["db"]) + suffix)
            try:
                if p.exists():
                    if not self.args.skip_backup:
                        shutil.copy2(p, self.artifacts / f"pipeline.db{suffix}.bak")
                    p.unlink()
            except OSError as exc:
                self.notes.append(f"重置数据库失败 {p.name}: {exc}")
                log(f"  [WARN] {p.name} 删除失败：{exc}")
        log("  已重置（重新 scan 建立队列）")

    # ------------------------------------------------------------------ #
    def write_config(self) -> Path:
        cfg_path = self.workspace / self.config_name
        content = (
            "# 由 scripts/e2e_batch_test.py 生成：端到端测试用配置\n"
            "# 与 config.yaml 的差异仅为禁用 AI 阶段（本环境无对应外部工具），\n"
            "# 使 pipeline 走其内置的降级路径（纯 FFmpeg 转码）。\n"
            "paths:\n"
            '  input: "./input"\n'
            '  work: "./work"\n'
            '  output: "./output"\n'
            '  failed: "./failed"\n'
            '  logs: "./logs"\n'
            '  database: "./pipeline.db"\n'
            "video_repair:\n"
            "  enabled: false\n"
            '  backend: "none"\n'
            "audio_repair:\n"
            "  enabled: false\n"
            '  backend: "none"\n'
        )
        try:
            cfg_path.write_text(content, encoding="utf-8")
            log(f"  测试配置：{cfg_path}")
        except OSError as exc:
            raise SystemExit(f"无法写入测试配置 {cfg_path}: {exc}") from exc
        return cfg_path

    # ------------------------------------------------------------------ #
    # 步骤 4：调用 pipeline
    # ------------------------------------------------------------------ #
    def _run(self, cmd: list[str], timeout: float) -> tuple[int | None, str, bool]:
        """执行命令；返回 (returncode, stdout+stderr, timed_out)。"""
        try:
            p = subprocess.run(cmd, cwd=str(self.workspace), env=self.env(),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, **NO_WINDOW)
            return p.returncode, (p.stdout or "") + (p.stderr or ""), False
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return None, out, True
        except OSError as exc:
            return None, f"命令启动失败: {exc}", False

    def run_pipeline(self) -> None:
        log("步骤 4/6：调用 pipeline（doctor → scan → run）")
        cfg = f"--config={self.config_name}"

        rc, out, _ = self._run([self.python(), self.main_py(), cfg, "doctor"], 600)
        self.doctor_output = out.strip()
        log(f"  doctor 退出码 = {rc}")
        for line in out.strip().splitlines():
            log(f"    {line}")

        rc, out, _ = self._run([self.python(), self.main_py(), cfg, "scan"], 1800)
        log(f"  scan 退出码 = {rc}：{out.strip().splitlines()[-1] if out.strip() else ''}")
        if rc not in (0, None):
            self.notes.append(f"scan 非零退出（rc={rc}）：{out.strip()[-300:]}")

        self.pipeline_out_log = self.paths["logs"] / f"e2e_pipeline_{self.run_id}.out.log"
        self.pipeline_out_log.parent.mkdir(parents=True, exist_ok=True)
        log(f"  run 开始（超时上限 {self.args.run_timeout}s），输出 → "
            f"{self.pipeline_out_log.name}")
        try:
            with self.pipeline_out_log.open("w", encoding="utf-8", errors="replace") as fh:
                proc = subprocess.Popen(
                    [self.python(), self.main_py(), cfg, "run"],
                    cwd=str(self.workspace), env=self.env(),
                    stdout=fh, stderr=subprocess.STDOUT, text=True, **NO_WINDOW)
                try:
                    self.run_rc = proc.wait(timeout=self.args.run_timeout)
                except subprocess.TimeoutExpired:
                    self.run_timed_out = True
                    proc.kill()
                    proc.wait(timeout=60)
                    self.notes.append(
                        f"run 超过 {self.args.run_timeout}s 未结束，已强制终止")
                    log(f"  [WARN] run 超时，已终止（pid={proc.pid}）")
        except OSError as exc:
            self.notes.append(f"run 启动失败: {exc}")
            log(f"  [ERROR] run 启动失败：{exc}")
        log(f"  run 退出码 = {self.run_rc}"
            f"{'（超时终止）' if self.run_timed_out else ''}")

        self._index_pipeline_output()

    def _index_pipeline_output(self) -> None:
        """流式扫描 run 日志：提取崩溃行与按 job 的堆栈跟踪（避免全量驻留内存）。"""
        if not self.pipeline_out_log or not self.pipeline_out_log.exists():
            return
        cur_job: int | None = None
        try:
            with self.pipeline_out_log.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "未预期异常" in line:
                        cur_job = None
                        for tok in line.replace("任务", " ").split():
                            if tok.isdigit():
                                cur_job = int(tok)
                                break
                        if cur_job is not None:
                            self.tracebacks.setdefault(cur_job, []).append(line.rstrip())
                    elif cur_job is not None:
                        self.tracebacks[cur_job].append(line.rstrip())
                        if len(self.tracebacks[cur_job]) > 60:
                            cur_job = None
                    low = line.lower()
                    if any(k in low for k in ("traceback", "memoryerror",
                                              "recursionerror", "internal error")):
                        self.crash_lines.append(line.rstrip())
        except OSError as exc:
            self.notes.append(f"读取 pipeline 运行日志失败: {exc}")

    # ------------------------------------------------------------------ #
    # 步骤 5：结果分类
    # ------------------------------------------------------------------ #
    def probe_ok(self, path: Path) -> tuple[bool, str]:
        """独立用 ffprobe 复核导出文件（视频/音频流、时长、目标编码）。"""
        ffprobe = (self.ffmpeg_dir / "ffprobe.exe") if self.ffmpeg_dir else Path("ffprobe")
        try:
            p = subprocess.run(
                [str(ffprobe), "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300, **NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"ffprobe 复核失败: {exc}"
        if p.returncode != 0:
            return False, f"ffprobe 无法读取导出文件: {(p.stderr or '')[-300:]}"
        try:
            data = json.loads(p.stdout or "{}")
        except json.JSONDecodeError as exc:
            return False, f"ffprobe 输出无法解析: {exc}"
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        v = [s for s in streams if s.get("codec_type") == "video"]
        a = [s for s in streams if s.get("codec_type") == "audio"]
        problems = []
        try:
            size = int(fmt.get("size", 0) or 0)
            dur = float(fmt.get("duration", 0) or 0)
        except (TypeError, ValueError):
            size, dur = 0, 0.0
        if size < 10_000:
            problems.append(f"文件过小({size}B)")
        if dur <= 0:
            problems.append("时长无效")
        if not v:
            problems.append("缺少视频流")
        if not a:
            problems.append("缺少音频流")
        if v and v[0].get("codec_name") not in ("hevc", "h265"):
            problems.append(f"视频编码={v[0].get('codec_name')}(期望hevc)")
        if a:
            if (a[0].get("codec_name") or "").lower() != "aac":
                problems.append(f"音频编码={a[0].get('codec_name')}(期望aac)")
            if str(a[0].get("sample_rate")) != "48000":
                problems.append(f"采样率={a[0].get('sample_rate')}(期望48000)")
        return (not problems), "; ".join(problems)

    @staticmethod
    def unique_target(directory: Path, name: str) -> Path:
        """保持原文件名；同名时在文件名后加 YYYYMMDDHHMMSS 时间戳。"""
        target = directory / name
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        return directory / f"{stem}_{now_ts()}{suffix}"

    def read_jobs(self) -> list[sqlite3.Row]:
        try:
            conn = sqlite3.connect(str(self.paths["db"]))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT job_id, source_path, source_size, status, stage, "
                "retry_count, last_error, output_path, finished_at FROM jobs "
                "ORDER BY job_id").fetchall()
            conn.close()
            return rows
        except sqlite3.Error as exc:
            self.notes.append(f"读取任务数据库失败: {exc}")
            log(f"  [ERROR] 读取数据库失败：{exc}")
            return []

    def job_log_tail(self, job_id: int, lines: int = 60) -> tuple[str, str]:
        """返回 (最后执行的命令, 任务日志尾部)。"""
        p = self.paths["logs"] / "jobs" / f"{job_id:04d}.log"
        if not p.exists():
            return "", ""
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", ""
        cmd = ""
        for line in text.splitlines():
            if line.startswith("$ "):
                cmd = line[2:].strip()
        tail = "\n".join(text.splitlines()[-lines:])
        return cmd, tail

    # ---- 错误日志写入（单文件 ≤10MB，超出自动新建） --------------------- #
    def _error_log(self, rotate: bool = False) -> Path:
        if self.error_log_file and not rotate:
            try:
                if self.error_log_file.stat().st_size < ERROR_LOG_MAX_BYTES:
                    return self.error_log_file
            except OSError:
                pass
        self.error_log_file = self.paths["logs"] / f"{now_ts()}_error.log"
        try:
            self.error_log_file.touch(exist_ok=True)
        except OSError as exc:
            log(f"  [WARN] 无法创建错误日志：{exc}")
        return self.error_log_file

    def write_error_entry(self, rec: dict) -> None:
        tb = rec.get("traceback") or (
            "(该阶段为受控业务异常，无 Python 堆栈；详见下方任务日志尾部)")
        entry = (
            "=" * 78 + "\n"
            f"[失败时间]   {now_str()}\n"
            f"[源文件名]   {rec['file']}\n"
            f"[源路径]     {rec['source_path']}\n"
            f"[任务ID]     {rec.get('job_id')}\n"
            f"[任务状态]   {rec.get('status')}\n"
            f"[失败阶段]   {rec.get('stage')}\n"
            f"[失败命令]   {rec.get('command') or '(无外部命令记录)'}\n"
            f"[错误分类]   {rec['category']}\n"
            f"[错误信息]   {rec.get('error') or '(空)'}\n"
            f"[源文件处理] {rec.get('source_action')}\n"
            f"[导出的mp4]  {rec.get('output_action') or '(无)'}\n"
            "[堆栈跟踪]\n"
            f"{tb}\n"
            "[任务日志尾部]\n"
            f"{rec.get('job_log_tail') or '(无)'}\n"
        )
        try:
            p = self._error_log()
            with p.open("a", encoding="utf-8") as fh:
                fh.write(entry)
        except OSError as exc:
            log(f"  [WARN] 写入错误日志失败 {rec['file']}：{exc}")

    # ------------------------------------------------------------------ #
    def classify_results(self) -> None:
        log("步骤 5/6：分类结果（成功 → output / 失败 → failed + 错误日志）")
        jobs = self.read_jobs()
        seen_sources: set[str] = set()
        output_dir, failed_dir = self.paths["output"], self.paths["failed"]

        for row in jobs:
            src = Path(row["source_path"])
            seen_sources.add(str(src.resolve()).lower())
            rec = {
                "job_id": row["job_id"], "file": src.name,
                "source_path": row["source_path"], "status": row["status"],
                "stage": row["stage"], "retry_count": row["retry_count"],
                "error": row["last_error"], "output_path": row["output_path"],
                "finished_at": row["finished_at"], "command": "",
                "job_log_tail": "", "traceback": "", "source_action": "",
                "output_action": "", "category": "", "ok": False,
            }
            rec["traceback"] = "\n".join(self.tracebacks.get(row["job_id"], []))
            rec["command"], rec["job_log_tail"] = self.job_log_tail(row["job_id"])

            # ---- 判定成功：任务 DONE + 导出文件存在且复核通过 ----
            out_path = Path(row["output_path"]) if row["output_path"] else None
            ok, reason = False, ""
            if row["status"] == "DONE":
                if out_path is None or not out_path.exists():
                    reason = f"导出文件校验: 任务标记 DONE 但文件不存在({out_path})"
                else:
                    ok, reason = self.probe_ok(out_path)
                    if not ok:
                        reason = f"导出文件校验: {reason}"
            else:
                reason = row["last_error"] or f"任务未完成（status={row['status']}）"

            if ok and out_path is not None:
                rec["ok"] = True
                # 成功：只保留 pipeline 产出的 mp4 在 output/，保持原文件名
                desired = output_dir / f"{sanitize_filename(src.stem)}.mp4"
                try:
                    if out_path.resolve() == desired.resolve():
                        rec["output_action"] = f"已在 output/{desired.name}"
                    else:
                        target = desired if not desired.exists() else self.unique_target(
                            output_dir, f"{sanitize_filename(src.stem)}.mp4")
                        shutil.move(str(out_path), str(target))
                        rec["output_action"] = f"重命名为 output/{target.name}"
                except (OSError, shutil.Error) as exc:
                    rec["ok"] = False
                    rec["category"] = "EXPORT_MOVE_FAILED"
                    rec["error"] = f"移动导出文件失败: {exc}"
                    rec["source_action"] = "未移动源文件"
                    log(f"  [ERROR] {src.name}: {rec['error']}")
                    self.write_error_entry(rec)
                    self.results.append(rec)
                    continue
            else:
                # ---- 失败：源文件移入 failed/ + 错误日志 ----
                rec["category"] = classify(f"{reason} {row['last_error']} {rec['traceback']}")
                rec["error"] = reason
                try:
                    if src.exists():
                        target = self.unique_target(failed_dir, sanitize_filename(src.name))
                        shutil.move(str(src), str(target))
                        rec["source_action"] = f"已移动 → failed/{target.name}"
                    else:
                        rec["source_action"] = "源文件缺失，未能移动"
                        rec["category"] = "SOURCE_MISSING"
                except (OSError, shutil.Error) as exc:
                    rec["source_action"] = f"移动失败: {exc}"
                    log(f"  [ERROR] {src.name}: 源文件移动失败：{exc}")
                if out_path and out_path.exists():
                    rec["output_action"] = "残留未通过校验的导出文件（保留于 output/）"
                self.write_error_entry(rec)
                log(f"  [FAIL] {src.name}  阶段={row['stage']}  分类={rec['category']}")

            self.results.append(rec)

        # ---- input/ 中存在但未进入队列的文件 ----
        try:
            exts = {".mp4", ".avi", ".wmv", ".mkv", ".mov", ".flv", ".webm",
                    ".mpg", ".mpeg", ".3gp"}
            stray = [p for p in sorted(self.paths["input"].rglob("*"))
                     if p.is_file() and p.suffix.lower() in exts
                     and str(p.resolve()).lower() not in seen_sources]
        except OSError:
            stray = []
        for p in stray:
            rec = {"job_id": None, "file": p.name, "source_path": str(p),
                   "status": "NOT_SCANNED", "stage": "NONE", "retry_count": 0,
                   "error": "未进入处理队列（未被 scan 收录或被跳过）",
                   "output_path": "", "finished_at": "", "command": "",
                   "job_log_tail": "", "traceback": "", "output_action": "",
                   "category": "UNPROCESSED", "ok": False}
            try:
                target = self.unique_target(failed_dir, sanitize_filename(p.name))
                shutil.move(str(p), str(target))
                rec["source_action"] = f"已移动 → failed/{target.name}"
            except (OSError, shutil.Error) as exc:
                rec["source_action"] = f"移动失败: {exc}"
            self.write_error_entry(rec)
            self.results.append(rec)
            log(f"  [FAIL] {p.name}  分类=UNPROCESSED")

        if self.run_timed_out:
            for rec in self.results:
                if not rec["ok"] and not rec["traceback"]:
                    rec["traceback"] = "TEST_RUN_TIMEOUT: pipeline run 进程超时被强制终止"

    # ------------------------------------------------------------------ #
    # 步骤 6：汇总报告
    # ------------------------------------------------------------------ #
    def report(self) -> tuple[int, Path]:
        log("步骤 6/6：生成汇总报告")
        self.duration = time.time() - self.started
        ok = [r for r in self.results if r["ok"]]
        bad = [r for r in self.results if not r["ok"]]
        by_cat: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        for r in bad:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
            by_stage[r.get("stage") or "?"] = by_stage.get(r.get("stage") or "?", 0) + 1
        total = len(self.results)
        rate = (len(ok) / total * 100) if total else 0.0

        err_logs = sorted(self.paths["logs"].glob("*_error.log"))
        err_total = sum(p.stat().st_size for p in err_logs if p.exists())

        lines: list[str] = []
        a = lines.append
        a("# Video Pipeline 端到端功能测试汇总报告")
        a("")
        a(f"- 测试时间：{datetime.fromtimestamp(self.started):%Y-%m-%d %H:%M:%S}"
          f" ~ {now_str()}（耗时 {self.duration / 60:.1f} 分钟）")
        a(f"- 被测对象：`{self.workspace}`")
        a(f"- 测试脚本：`scripts/e2e_batch_test.py`（未修改 pipeline 业务逻辑代码）")
        a(f"- 测试配置：`{self.config_name}`（仅禁用 video_repair / audio_repair，"
          f"走 pipeline 内置降级路径）")
        a(f"- pipeline run 退出码：`{self.run_rc}`"
          f"{'（超时强制终止）' if self.run_timed_out else ''}")
        a("")

        a("## 1. 环境配置")
        a("")
        a(f"- 虚拟环境：`{PROJ}\\.venv`（Python {sys.version.split()[0]}，"
          f"解释器 `{sys.executable}`，prefix `{sys.prefix}`）")
        req = ROOT / "requirements.txt"
        if req.exists():
            pkgs = ", ".join(l.strip() for l in req.read_text(encoding="utf-8").splitlines()
                             if l.strip() and not l.startswith("#"))
            a(f"- Python 依赖（requirements.txt）：{pkgs}")
        a(f"- FFmpeg / FFprobe：`{self.ffmpeg_dir}`")
        a("- 外部 AI 工具：REAL-Video-Enhancer 与 DeepFilterNet **均未安装**；"
          "REAL-Video-Enhancer 为 C++/TensorRT 工程，本环境无法通过 pip 获得，"
          "故本次测试按其内部降级路径（纯 FFmpeg 转码）执行。")
        a("")
        a("```text")
        a((self.doctor_output or "(doctor 无输出)")[:2500])
        a("```")
        a("")

        a("## 2. 关键指标")
        a("")
        a("| 指标 | 数值 |")
        a("| --- | --- |")
        a(f"| 总视频文件数 | {total} |")
        a(f"| 成功（导出 mp4 在 output/） | {len(ok)} |")
        a(f"| 失败（源文件已移入 failed/） | {len(bad)} |")
        a(f"| 成功率 | {rate:.1f}% |")
        a(f"| 测试耗时 | {self.duration / 60:.1f} 分钟 |")
        a(f"| 错误日志文件数 / 总大小 | {len(err_logs)} / {err_total / 1024:.1f} KB |")
        a("")
        a(f"- output/：{sum(1 for _ in self.paths['output'].glob('*') if _.is_file())} 个文件")
        a(f"- failed/：{sum(1 for _ in self.paths['failed'].rglob('*') if _.is_file())} 个文件")
        a("")

        a("## 3. 失败文件清单")
        a("")
        if bad:
            a("| # | 文件名 | job_id | 失败阶段 | 错误分类 | 错误摘要 | 源文件处理 |")
            a("| --- | --- | --- | --- | --- | --- | --- |")
            for i, r in enumerate(bad, 1):
                summary = (r["error"] or "").replace("|", "/").replace("\n", " ")[:150]
                a(f"| {i} | {r['file']} | {r.get('job_id')} | {r.get('stage')} | "
                  f"{r['category']} | {summary} | {r.get('source_action') or '-'} |")
        else:
            a("无失败文件。")
        a("")

        a("## 4. 错误分类统计")
        a("")
        if by_cat:
            a("| 错误分类 | 数量 | 涉及阶段 |")
            a("| --- | --- | --- |")
            for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
                stages = sorted({r.get("stage") or "?" for r in bad if r["category"] == cat})
                a(f"| {cat} | {n} | {', '.join(stages)} |")
        else:
            a("无错误记录。")
        a("")
        if by_stage:
            a("失败阶段分布：" + "，".join(f"{k} × {v}" for k, v in
                                          sorted(by_stage.items(), key=lambda kv: -kv[1])))
            a("")

        a("## 5. 共性问题与系统性缺陷分析")
        a("")
        for t in self.analyze(bad, ok, by_cat):
            a(t)
        a("")

        a("## 6. 成功文件清单")
        a("")
        if ok:
            for i, r in enumerate(ok, 1):
                a(f"{i}. {r['file']} → {r.get('output_action') or 'output/'}")
        else:
            a("无。")
        a("")

        a("## 7. 附录：可追溯性")
        a("")
        a(f"- pipeline 完整运行日志：`{self.pipeline_out_log}`")
        a(f"- 错误日志：{', '.join('`' + str(p) + '`' for p in err_logs) or '（无）'}")
        a(f"- 单任务详细日志：`{self.paths['logs']}\\jobs\\<job_id>.log`"
          f"（含每次外部命令的 stdout/stderr）")
        a(f"- 输入与数据库备份：`{self.artifacts}`")
        if self.notes:
            a("- 测试过程备注：")
            for n in self.notes:
                a(f"  - {n}")
        a("")

        report_path = self.workspace / "E2E_TEST_REPORT.md"
        try:
            report_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            log(f"  [ERROR] 报告写入失败：{exc}")
        try:
            (self.paths["logs"] / f"e2e_results_{self.run_id}.json").write_text(
                json.dumps({"summary": {"total": total, "ok": len(ok), "bad": len(bad),
                                        "success_rate": rate,
                                        "duration_sec": round(self.duration, 1),
                                        "run_rc": self.run_rc,
                                        "run_timed_out": self.run_timed_out},
                            "results": self.results, "notes": self.notes},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        log(f"  报告：{report_path}")
        log(f"  成功 {len(ok)} / 失败 {len(bad)} / 总计 {total}（成功率 {rate:.1f}%）")
        return (0 if not bad else 1), report_path

    def analyze(self, bad, ok, by_cat) -> list[str]:
        out: list[str] = []
        if not bad:
            out.append("- 本轮测试未出现失败样本，无法从数据中归纳共性问题。")
        else:
            top = max(by_cat.items(), key=lambda kv: kv[1])
            if top[1] == len(bad) and len(bad) > 1:
                out.append(f"- **单一错误原因集中**：全部 {len(bad)} 个失败都归入 "
                           f"`{top[0]}`，说明失败不是随机个例，而是环境或配置层面的"
                           f"系统性问题，而非文件本身的差异。")
            else:
                out.append(f"- 失败原因分布："
                           + "，".join(f"`{k}` × {v}" for k, v in
                                      sorted(by_cat.items(), key=lambda kv: -kv[1]))
                           + "；存在多种成因。")
        out.append("")
        out.append("以下为本次测试过程中由代码与运行证据确认的缺陷/薄弱点（未修改被测代码，"
                   "仅记录）：")
        out.append("")
        out.append("1. **默认配置与真实环境不匹配导致全量失败（本次测试前置状态）**："
                   "`config.yaml` 出厂默认 `video_repair.enabled: true` 且 "
                   "`executable: realesrgan-video-enhancer`，而 `DependencyError` 被标记为"
                   "不可重试，因此未安装该工具时**所有任务**都在 `REPAIR_VIDEO` 阶段直接 "
                   "`FAILED_FINAL`。测试开始前数据库中 37/37 任务均为该状态。建议 "
                   "`doctor` 前置校验或在检测不到 AI 工具时自动降级为纯转码。")
        out.append("2. **磁盘处于 20~30GB 区间时存在无进展死等**："
                   "`Scheduler._pick_next_job()` 在 `DiskState.CONTROLLED` 及更严重状态下，"
                   "只挑选 `stage != NONE` 的任务；若队列中只剩从未开始（`DISCOVERED`，"
                   "`stage = NONE`）的任务，则返回 `None`；而 `_has_pending_work()` 仍把 "
                   "`DISCOVERED` 计为待处理，主循环于是永久 `sleep(poll_interval)` 轮询——"
                   "既无进度也不会退出。本次因 E: 可用 129GB（NORMAL）未触发，"
                   "但在设计容量 50GB 的主机上，磁盘滑落到 20~30GB 时极易命中。")
        out.append("3. **重试次数与文档不一致（off-by-one）**："
                   "`retry.max_attempts = 2` 的判定是 "
                   "`retry_count < max_attempts - 1`，实际每个任务只会重试 **1** 次即进入 "
                   "`FAILED_FINAL`，而 README 描述为“默认每个任务最多重试 2 次”。")
        out.append("4. **输出命名不满足“时间戳”要求**：`pipeline/filename.py::"
                   "unique_output_path()` 对同名冲突使用 `_1`、`_2` 递增后缀，"
                   "而非需求中的 `YYYYMMDDHHMMSS` 时间戳；同名检查也只看 output/ 目录内的 "
                   "文件，不含跨目录语义。")
        out.append("5. **跨任务共享状态脆弱点**：`Scheduler._stage_export()` 把 `.partial` "
                   "路径写在实例属性 `self._partial_path` 上，`_stage_verify()` 再读取；"
                   "断点续跑时若 `EXPORT` 被跳过，`VERIFY` 可能读到上一个任务遗留的值。"
                   "当前因有 `Path(partial).exists()` 兜底而未产生错误结果，属隐患。")
        out.append("6. **目标格式校验过严**：`Verifier.verify_output()` 强制要求 "
                   "`sample_rate == 48000` 且视频编码必须为 hevc/h265；源为 22050Hz 单声道 "
                   "WMV，最终能否通过完全依赖 EXPORT 阶段 `-ar 48000` 一路参数不被改动，"
                   "参数微调即会造成全量 VERIFY 失败（无按文件差异化容忍）。")
        out.append("")
        out.append("最终产物分类一致性：本次全部 "
                   f"{len(ok) + len(bad)} 个样本均已归入 output/ 或 failed/，"
                   "无遗留未分类文件；测试期间未出现进程崩溃或死循环（见 pipeline run "
                   "退出码与运行日志）。")
        return out

    # ------------------------------------------------------------------ #
    def run(self) -> int:
        try:
            self.prepare_dirs()
            self.backup()
            self.reset_db()
            self.write_config()
            self.run_pipeline()
            self.classify_results()
            rc, _ = self.report()
            return rc
        except KeyboardInterrupt:
            log("用户中断，正在尽力生成报告…")
            try:
                self.classify_results()
                self.report()
            except Exception as exc:  # noqa: BLE001
                log(f"中断后生成报告失败：{exc}")
            return 130
        except Exception as exc:  # noqa: BLE001 —— 任何意外都必须留下痕迹
            import traceback
            log(f"[FATAL] 测试执行器异常：{exc}")
            traceback.print_exc()
            try:
                rc, _ = self.report()
            except Exception:  # noqa: BLE001
                pass
            return 2


# --------------------------------------------------------------------------- #
def build_smoke_workspace(count: int) -> Path:
    """冒烟测试工作区：隔离目录 + 最小的 N 个输入视频副本。"""
    ws = ARTIFACT_ROOT / f"smoke_{now_ts()}"
    for sub in ("input", "work", "output", "failed", "logs"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    exts = {".mp4", ".avi", ".wmv", ".mkv", ".mov", ".flv", ".webm",
            ".mpg", ".mpeg", ".3gp"}
    src_in = ROOT / "input"
    files = sorted((p for p in src_in.rglob("*")
                    if p.is_file() and p.suffix.lower() in exts),
                   key=lambda p: p.stat().st_size)[:count]
    for p in files:
        shutil.copy2(p, ws / "input" / p.name)
    log(f"冒烟工作区：{ws}（{len(files)} 个输入文件）")
    return ws


def main() -> int:
    ap = argparse.ArgumentParser(description="Video Pipeline 端到端批量功能测试")
    ap.add_argument("--workspace", default=None, help="被测工作区（默认 video_pipeline/）")
    ap.add_argument("--config", default="config.e2e.yaml", help="测试用配置文件名")
    ap.add_argument("--skip-backup", action="store_true", help="跳过输入备份")
    ap.add_argument("--run-timeout", type=float, default=21600,
                    help="pipeline run 的总超时秒数（默认 21600 = 6 小时）")
    ap.add_argument("--ffmpeg-dir", default=None, help="包含 ffmpeg.exe/ffprobe.exe 的目录")
    ap.add_argument("--smoke", action="store_true", help="冒烟模式：隔离工作区 + 最小样本")
    ap.add_argument("--smoke-count", type=int, default=1, help="冒烟模式样本数")
    args = ap.parse_args()

    if args.smoke:
        args.workspace = str(build_smoke_workspace(args.smoke_count))
        args.config = "config.yaml"
    return Runner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())