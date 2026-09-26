"""多镜像、可续传的 PyTorch CUDA 轮子下载器。

为什么不用 `pip install --index-url <镜像>`：
    download.pytorch.org 是 PEP 503 索引，而国内多数「pytorch-wheels」镜像是
    普通 HTTP 目录列表，pip 的 --find-links / --index-url 都解析不了
    （报 "No matching distribution found"）。因此这里直接按 URL 抓取 .whl，
    抓完再用 `pip install --no-deps <本地whl>` 安装。

特性：
  * 多镜像**按实测速度自动排序**，也可用 --mirror 指定；
  * HTTP Range 断点续传，中断后重跑接着下；
  * 完成时比对 Content-Length 校验完整性；
  * 全部落在指定目录，不污染 C 盘（pip 默认会把大 wheel 先下到 %TEMP%）。

用法：
    python scripts/fetch_cuda_wheels.py --dest D:/pip-wheels
    python scripts/fetch_cuda_wheels.py --dest D:/pip-wheels --cuda-index cu126
    python scripts/fetch_cuda_wheels.py --probe-only
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: 镜像前缀模板，{cu} 会被替换为 --cuda-index（如 cu128）
MIRRORS: list[tuple[str, str]] = [
    ("上海交大", "https://mirror.sjtu.edu.cn/pytorch-wheels/{cu}/"),
    ("官方",     "https://download.pytorch.org/whl/{cu}/"),
    ("阿里云",   "https://mirrors.aliyun.com/pytorch-wheels/{cu}/"),
]

#: 需要下载的轮子：{torch}/{tv} 为版本号，{py} 为 Python tag
WHEEL_TEMPLATES: list[tuple[str, str]] = [
    ("torch {torch}+{cu}",       "torch-{torch}%2B{cu}-{py}-{py}-win_amd64.whl"),
    ("torchvision {tv}+{cu}",    "torchvision-{tv}%2B{cu}-{py}-{py}-win_amd64.whl"),
]

CHUNK = 1 << 20  # 1 MiB


def _head(url: str, timeout: float = 20) -> int | None:
    """返回远端文件大小；失败返回 None。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            n = int(r.headers.get("Content-Length", 0))
            return n or None
    except Exception:  # noqa: BLE001
        return None


def probe(url: str, bytes_to_read: int = 6 << 20, timeout: float = 15) -> float:
    """实测某镜像的下载速度（MB/s）；失败返回 0。"""
    try:
        t0 = time.time()
        req = urllib.request.Request(
            url, headers={"Range": f"bytes=0-{bytes_to_read - 1}"})
        got = 0
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                b = r.read(1 << 16)
                if not b:
                    break
                got += len(b)
        dt = time.time() - t0
        return got / 1e6 / dt if dt > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def pick_mirror(probe_path: str, mirror_filter: str | None = None
                ) -> tuple[str, str, float]:
    """按速度挑选镜像，返回 (名称, 前缀, 速度)。"""
    best: tuple[str, str, float] | None = None
    for name, tpl in MIRRORS:
        if mirror_filter and mirror_filter not in name:
            continue
        prefix = tpl
        speed = probe(prefix + probe_path)
        if speed > 0:
            print(f"    探测 {name:<8} {speed:6.2f} MB/s")
            if best is None or speed > best[2]:
                best = (name, prefix, speed)
        else:
            print(f"    探测 {name:<8} 不可用")
    if best is None:
        raise SystemExit("所有镜像均不可用，请检查网络或代理设置")
    return best


def download(url: str, dest: Path, label: str) -> Path:
    """带断点续传的下载；返回本地路径。"""
    total = _head(url)
    if total is None:
        raise RuntimeError(f"无法获取远端大小: {url}")

    done = dest.stat().st_size if dest.exists() else 0
    if done == total:
        print(f"  [跳过] {label} 已完整 ({total / 1e9:.2f} GB)")
        return dest
    if done > total:
        print(f"  [重下] {label} 本地文件异常（{done} > {total}）")
        dest.unlink()
        done = 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if done else "wb"
    headers = {"Range": f"bytes={done}-"} if done else {}
    req = urllib.request.Request(url, headers=headers)

    t0 = time.time()
    last = t0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, mode) as f:
        # 服务端不支持 Range 时会返回 200，需从 0 重写
        if done and r.status != 206:
            print("  [注意] 服务端不支持断点续传，从头下载")
            done = 0
            f.close()
            f = open(dest, "wb")
        got = done
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            now = time.time()
            if now - last >= 2:
                last = now
                pct = got / total * 100
                rate = (got - done) / max(now - t0, 1e-6) / 1e6
                eta = (total - got) / max(rate * 1e6, 1)
                bar_len = 26
                filled = int(bar_len * got / total)
                bar = "#" * filled + "." * (bar_len - filled)
                print(f"\r  {label:<22} [{bar}] {pct:5.1f}%  "
                      f"{got / 1e9:.2f}/{total / 1e9:.2f} GB  "
                      f"{rate:5.2f} MB/s  ETA {eta / 60:4.1f} min",
                      end="", flush=True)
    print()

    final = dest.stat().st_size
    if final != total:
        raise RuntimeError(f"下载不完整: {final} != {total}，请重跑本脚本续传")
    print(f"  [完成] {label} -> {dest}  ({final / 1e9:.2f} GB)")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 PyTorch CUDA 轮子")
    ap.add_argument("--dest", default="D:/pip-wheels",
                    help="轮子存放目录（默认 D:/pip-wheels，避免占用 C 盘）")
    ap.add_argument("--cuda-index", default="cu128",
                    help="CUDA 构建标签：cu128 / cu126 / cu118（默认 cu128）")
    ap.add_argument("--torch-ver", default="2.7.0", help="torch 版本")
    ap.add_argument("--tv-ver", default="0.22.0", help="torchvision 版本")
    ap.add_argument("--py-tag", default="cp311", help="Python tag，如 cp311")
    ap.add_argument("--mirror", default=None,
                    help="强制指定镜像关键字，如 '上海交大' / '官方'")
    ap.add_argument("--probe-only", action="store_true",
                    help="只探测各镜像速度，不下载")
    args = ap.parse_args()

    socket.setdefaulttimeout(60)
    dest = Path(args.dest)
    ctx = {"cu": args.cuda_index, "torch": args.torch_ver,
           "tv": args.tv_ver, "py": args.py_tag}

    print("=" * 70)
    print("PyTorch CUDA 轮子下载")
    print("=" * 70)
    print(f"  目标目录 : {dest.resolve()}")
    print(f"  CUDA 构建: {args.cuda_index}")
    print(f"  版本     : torch {args.torch_ver} / torchvision {args.tv_ver}"
          f" / {args.py_tag}")

    probe_path = WHEEL_TEMPLATES[0][1].format(**ctx)
    print("\n[1/2] 探测镜像速度")
    name, prefix, speed = pick_mirror(probe_path, args.mirror)
    print(f"  → 选用: {name} ({speed:.2f} MB/s)")

    if args.probe_only:
        return 0

    print("\n[2/2] 下载")
    paths = []
    for label_tpl, fname_tpl in WHEEL_TEMPLATES:
        fname = fname_tpl.format(**ctx)
        url = prefix + fname
        local = dest / urllib.parse.unquote(fname)
        paths.append(download(url, local, label_tpl.format(**ctx)))

    print("\n全部完成。安装命令：")
    print("  ./.venv-rve/Scripts/python.exe -m pip install --no-deps " +
          " ".join(f'"{p}"' for p in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
