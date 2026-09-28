"""子进程执行封装。

安全约束（需求 #32）：
- 禁止使用 shell=True；
- 所有外部命令必须有 timeout；
- return code 必须检查；
- stdout/stderr 完整保存到日志文件，不进入 Python 内存全量缓存。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import (ExternalToolError, GpuOutOfMemoryError,
                     SystemMemoryError)

log = logging.getLogger("pipeline.runner")

#: **显存**不足的特征。必须比系统内存更具体、且先判：显存不够可以靠降超分倍率
#: 当场解决；系统内存不够只能让出队列稍后再来。
_VRAM_OOM_HINTS = ("cuda out of memory", "cuda oom", "cudnn_status_alloc_failed",
                   "vkerrormemorymapfailed", "gpu out of memory",
                   "hip out of memory")

#: Windows 上"系统内存不足"的两种崩法（与 adapters/deepfilternet.py::_is_oom 同源）：
#:   0xC0000005 (3221225477) 访问冲突 —— torch 在提交内存失败时以访问冲突崩溃
#:   0xC0000409 (3221226505) fast-fail —— 分配失败后直接 abort
#: 判别这两类退出码很关键：否则内存崩溃会被当成普通工具错误，在同一个文件上
#: 连续重试 3 次（每次十几分钟才崩），整条队列跟着停顿。
_OOM_EXIT_CODES = (3221225477, -1073741819, 3221226505, -1073740791)

#: **系统内存**不足的特征。包含 OpenCV 的 "Insufficient memory / Failed to
#: allocate N bytes"：实测 RVE 读帧线程就是这样崩的（只申请 2.3MB 也失败），
#: 与显存无关。若把它当成显存 OOM 去降超分倍率重试，会在同样缺内存的环境里
#: 反复失败（实测挂了 1 小时 40 分钟没写一个字节）。
_RAM_OOM_HINTS = ("memoryerror", "insufficient memory", "memory allocation",
                  "cannot allocate memory", "failed to allocate",
                  "out of memory")


@dataclass
class RunResult:
    args: list[str]
    returncode: int
    duration_seconds: float
    stdout: str
    stderr: str


def run_command(args: Sequence[str],
                timeout: float = 7200,
                log_file: Path | None = None,
                check: bool = True,
                env: dict | None = None) -> RunResult:
    """执行外部命令（列表参数，绝不走 shell）。

    :param args: 命令与参数列表，如 ["ffmpeg", "-i", in, out]
    :param timeout: 秒；超时视为失败
    :param log_file: 若提供，stdout/stderr 追加写入该文件
    :param check: True 时非零退出码抛 ExternalToolError（显存不足抛
                  GpuOutOfMemoryError，系统内存不足抛 SystemMemoryError）
    """
    argv = [str(a) for a in args]
    log.info("RUN %s", " ".join(argv))
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,  # 安全红线：永不使用 shell=True
        )
    except FileNotFoundError as exc:
        raise ExternalToolError(f"找不到可执行文件: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExternalToolError(
            f"命令超时({timeout}s): {argv[0]}", returncode=-9) from exc
    duration = time.monotonic() - start

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"rc={proc.returncode} ({duration:.1f}s) =====\n")
            fh.write("$ " + " ".join(argv) + "\n")
            fh.write(proc.stdout or "")
            fh.write(proc.stderr or "")

    result = RunResult(argv, proc.returncode, duration,
                       proc.stdout or "", proc.stderr or "")
    log.info("EXIT rc=%d %.1fs %s", proc.returncode, duration, argv[0])

    if check and proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        low = tail.lower()
        if any(h in low for h in _VRAM_OOM_HINTS):
            raise GpuOutOfMemoryError(
                f"GPU 显存不足: {argv[0]}", proc.returncode, tail)
        if (proc.returncode in _OOM_EXIT_CODES
                or any(h in low for h in _RAM_OOM_HINTS)):
            raise SystemMemoryError(
                f"系统内存不足（rc={proc.returncode}）: {argv[0]}",
                proc.returncode, tail)
        raise ExternalToolError(
            f"命令失败 rc={proc.returncode}: {argv[0]}", proc.returncode, tail)
    return result
